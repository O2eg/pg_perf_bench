"""Profile-independent, bounded data loading and parallel index construction.

Profiles supply SQL and dependency metadata. This module owns batching,
connections, transactions, concurrency, progress, and initialization phases.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg

from pg_perf_bench.errors import ConfigurationError


@dataclass(frozen=True)
class LoadTask:
    name: str
    sql: str
    # A batched statement receives inclusive bigint bounds as $1 and $2.
    # None denotes one bounded statement, for example a small lookup table.
    count: int | None = None
    depends_on: tuple[str, ...] = ()
    max_rows_per_key: int = 1


@dataclass(frozen=True)
class LoadPlan:
    schemas: tuple[str, ...]
    schema_sql: str
    data: tuple[LoadTask, ...]
    indexes: tuple[LoadTask, ...]
    constraints: tuple[LoadTask, ...] = ()
    prepare_sql: str = ''
    after_data_sql: str = ''
    finalize_sql: str = ''


@dataclass(frozen=True)
class LoadOptions:
    workers: int = 4
    batch_rows: int = 100_000
    table_mode: str = 'unlogged'
    fsync: str = 'off'
    synchronous_commit: str = 'keep'
    timeout: float = 300.0

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> LoadOptions:
        return cls(
            workers=int(config.get('init_workers', 4)),
            batch_rows=int(config.get('init_batch_rows', 100_000)),
            table_mode=config.get('init_table_mode', 'unlogged'),
            fsync=config.get('init_fsync', 'off'),
            synchronous_commit=config.get('init_synchronous_commit', 'keep'),
            timeout=float(config.get('command_timeout', 300.0)),
        )


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def qualified_name(schema: str, name: str) -> str:
    return quote_identifier(schema) + '.' + quote_identifier(name)


def profile_search_path(plan: LoadPlan) -> str:
    return ', '.join(quote_identifier(schema) for schema in plan.schemas) + ', public'


def connection_kwargs(db_conf: dict[str, Any], options: LoadOptions) -> dict[str, Any]:
    kwargs = {key: value for key, value in db_conf.items() if key != 'connect_timeout'}
    kwargs['timeout'] = float(db_conf.get('connect_timeout', 5))
    kwargs['command_timeout'] = options.timeout
    settings = {**kwargs.get('server_settings', {}), 'application_name': 'pg_perf_bench:init'}
    kwargs['server_settings'] = settings
    return kwargs


@asynccontextmanager
async def loader_connection(db_conf, options: LoadOptions, *, search_path=None):
    """Set SQL session options explicitly and restore them before returning to a pool."""
    db = await asyncpg.connect(**connection_kwargs(db_conf, options))
    original = {}

    async def cleanup():
        try:
            if not db.is_closed():
                for name, value in original.items():
                    await db.execute('SELECT pg_catalog.set_config($1, $2, false)', name, value)
        finally:
            await db.close()

    try:
        settings = {}
        if options.synchronous_commit != 'keep':
            settings['synchronous_commit'] = options.synchronous_commit
        if search_path is not None:
            settings['search_path'] = search_path
        for name, value in settings.items():
            original[name] = await db.fetchval('SELECT pg_catalog.current_setting($1)', name)
            await db.execute('SELECT pg_catalog.set_config($1, $2, false)', name, value)
            actual = await db.fetchval('SELECT pg_catalog.current_setting($1)', name)
            if actual != value:
                raise ConfigurationError(f'Loader session did not apply {name}={value!r}')
        yield db
    finally:
        task = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise


def read_sql_tasks(path: Path) -> tuple[LoadTask, ...]:
    """Read explicitly separated SQL tasks; never split arbitrary SQL on semicolons."""
    return tuple(LoadTask(**item) for item in json.loads(path.read_text(encoding='utf-8')))


def load_plan(root: Path, entrypoint: str, scale: float) -> LoadPlan:
    path = (root / entrypoint).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ConfigurationError('initialization entrypoint must be a file inside the profile')
    spec = importlib.util.spec_from_file_location('_pg_perf_bench_profile_loader', path)
    if spec is None or spec.loader is None:
        raise ConfigurationError(f'Cannot import initialization entrypoint: {entrypoint}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    factory = getattr(module, 'build_load_plan', None)
    if not callable(factory):
        raise ConfigurationError(f'{entrypoint} must export build_load_plan(scale)')
    plan = factory(scale)
    validate_plan(plan)
    return plan


def validate_plan(plan: LoadPlan) -> None:
    if not isinstance(plan, LoadPlan) or not plan.schemas or not plan.schema_sql:
        raise ConfigurationError('Initialization must return a LoadPlan with schemas and DDL')
    if any(
        not schema or schema.startswith('pg_') or schema == 'information_schema'
        for schema in plan.schemas
    ):
        raise ConfigurationError('Initialization schemas must be user schemas')
    for tasks in (plan.data, plan.indexes, plan.constraints):
        names = {task.name for task in tasks}
        if len(names) != len(tasks):
            raise ConfigurationError('Initialization task names must be unique within a phase')
        done: set[str] = set()
        for task in tasks:
            if not task.name or not task.sql.strip() or task.max_rows_per_key < 1:
                raise ConfigurationError('Invalid initialization task')
            if task.count is not None and (
                type(task.count) is not int or not 0 <= task.count <= 2**63 - 1
            ):
                raise ConfigurationError('Initialization task count must be a nonnegative integer')
            if not set(task.depends_on) <= names:
                raise ConfigurationError(
                    f'Unknown dependencies for initialization task {task.name}'
                )
        while len(done) < len(tasks):
            ready = {task.name for task in tasks if set(task.depends_on) <= done} - done
            if not ready:
                raise ConfigurationError('Initialization task dependencies contain a cycle')
            done.update(ready)


@dataclass
class _TaskState:
    task: LoadTask
    next_key: int = 1
    outstanding: int = 0
    submitted: bool = False
    completed: bool = False
    rows: int = 0
    batches: int = 0
    seconds: float = 0.0
    last_progress: float = field(default_factory=time.monotonic)


async def run_tasks(
    logger,
    tasks: tuple[LoadTask, ...],
    db_conf: dict[str, Any],
    options: LoadOptions,
    *,
    phase: str,
    search_path: str,
) -> list[dict[str, Any]]:
    """Schedule at most workers jobs; never materialize millions of batch futures."""
    if not tasks:
        return []
    if options.workers < 1 or options.batch_rows < 1:
        raise ConfigurationError('Initialization workers and batch size must be positive')
    logger.info(
        'Initialization %s: starting %s tasks with up to %s workers.',
        phase,
        len(tasks),
        options.workers,
    )
    states = [_TaskState(task) for task in tasks]
    done: set[str] = set()
    changed = asyncio.Condition()
    cursor = 0

    async def take():
        nonlocal cursor
        async with changed:
            while len(done) < len(states):
                previous_done = len(done)
                for offset in range(len(states)):
                    index = (cursor + offset) % len(states)
                    state = states[index]
                    task = state.task
                    if state.completed or not set(task.depends_on) <= done:
                        continue
                    if task.count is None:
                        if state.submitted:
                            continue
                        state.submitted = True
                        params = ()
                    elif state.next_key <= task.count:
                        first = state.next_key
                        last = min(
                            task.count,
                            first + max(1, options.batch_rows // task.max_rows_per_key) - 1,
                        )
                        state.next_key = last + 1
                        params = (first, last)
                    else:
                        if state.outstanding == 0:
                            state.completed = True
                            done.add(task.name)
                            changed.notify_all()
                        continue
                    state.outstanding += 1
                    cursor = (index + 1) % len(states)
                    return state, params
                if len(done) < len(states) and len(done) == previous_done:
                    await changed.wait()
            return None

    async def worker():
        async with loader_connection(db_conf, options, search_path=search_path) as db:
            while (job := await take()) is not None:
                state, params = job
                started = time.monotonic()
                # One transaction per batch/index, independent of every other worker.
                try:
                    async with db.transaction():
                        status = await db.execute(state.task.sql, *params)
                except Exception:
                    logger.error(
                        'Initialization %s failed: task=%s, bounds=%s.',
                        phase,
                        state.task.name,
                        params or 'single job',
                    )
                    raise
                elapsed = time.monotonic() - started
                affected = (
                    int(status.rsplit(' ', 1)[-1]) if status.startswith(('INSERT ', 'COPY ')) else 0
                )
                async with changed:
                    state.outstanding -= 1
                    state.batches += 1
                    state.rows += affected
                    state.seconds += elapsed
                    if state.outstanding == 0 and (
                        state.task.count is None or state.next_key > state.task.count
                    ):
                        state.completed = True
                        done.add(state.task.name)
                    if state.completed or time.monotonic() - state.last_progress >= 2:
                        logger.info(
                            'Initialization %s: %s; %s rows committed; %s batches; %s.',
                            phase,
                            state.task.name,
                            state.rows,
                            state.batches,
                            'complete' if state.completed else 'running',
                        )
                        state.last_progress = time.monotonic()
                    changed.notify_all()

    workers = [asyncio.create_task(worker()) for _ in range(options.workers)]
    try:
        await asyncio.gather(*workers)
    finally:
        for task in workers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
    return [
        {
            'name': state.task.name,
            'rows': state.rows,
            'batches': state.batches,
            'worker_seconds': state.seconds,
        }
        for state in states
    ]


async def prepare_database(
    logger, plan: LoadPlan, db_conf: dict[str, Any], options: LoadOptions
) -> dict[str, Any]:
    """Execute the common phases; the caller owns temporary server settings."""
    validate_plan(plan)
    result: dict[str, Any] = {'started_at': datetime.now(timezone.utc).isoformat(), 'phases': []}
    search_path = profile_search_path(plan)
    result['schemas'] = list(plan.schemas)
    result['search_path'] = search_path
    async with loader_connection(db_conf, options, search_path=search_path) as db:

        async def statement(phase, sql):
            if not sql.strip():
                return
            logger.info('Initialization phase: %s.', phase)
            started = time.monotonic()
            await db.execute(sql)
            result['phases'].append({'name': phase, 'elapsed_seconds': time.monotonic() - started})

        await statement('schema', plan.schema_sql)
        await db.execute('SELECT set_config($1, $2, false)', 'search_path', search_path)
        tables = await db.fetch(
            'SELECT n.nspname AS schema, c.relname AS name FROM pg_class c '
            'JOIN pg_namespace n ON n.oid=c.relnamespace '
            "WHERE n.nspname=ANY($1::text[]) AND c.relkind='r' ORDER BY c.oid",
            list(plan.schemas),
        )
        indexes = await db.fetchval(
            'SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid '
            'JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=ANY($1::text[])',
            list(plan.schemas),
        )
        foreign_keys = await db.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE contype='f' "
            'AND connamespace IN (SELECT oid FROM pg_namespace WHERE nspname=ANY($1::text[]))',
            list(plan.schemas),
        )
        if indexes or foreign_keys:
            raise ConfigurationError(
                'Fast initialization schema must defer all indexes and foreign keys '
                'until after data loading'
            )
        for table in tables:
            await statement(
                options.table_mode + ':' + table['name'],
                f'ALTER TABLE ONLY {qualified_name(table["schema"], table["name"])} '
                + ('SET UNLOGGED' if options.table_mode == 'unlogged' else 'SET LOGGED'),
            )
        await statement('prepare', plan.prepare_sql)
        started = time.monotonic()
        evidence = await run_tasks(
            logger, plan.data, db_conf, options, phase='data', search_path=search_path
        )
        result['phases'].append(
            {'name': 'data', 'elapsed_seconds': time.monotonic() - started, 'tasks': evidence}
        )
        await statement('after_data', plan.after_data_sql)
        # Convert one stored table at a time to bound temporary rewrite space.
        if options.table_mode == 'unlogged':
            for table in tables:
                await statement(
                    'logged:' + table['name'],
                    f'ALTER TABLE ONLY {qualified_name(table["schema"], table["name"])} SET LOGGED',
                )
        for phase, tasks in (('indexes', plan.indexes), ('constraints', plan.constraints)):
            started = time.monotonic()
            # FK validation can lock several related tables in opposite orders.
            phase_options = replace(options, workers=1) if phase == 'constraints' else options
            evidence = await run_tasks(
                logger, tasks, db_conf, phase_options, phase=phase, search_path=search_path
            )
            result['phases'].append(
                {'name': phase, 'elapsed_seconds': time.monotonic() - started, 'tasks': evidence}
            )
        await statement('finalize', plan.finalize_sql)
        # VACUUM must be outside the implicit transaction of a multi-statement string.
        tables = await db.fetch(
            'SELECT n.nspname AS schema, c.relname AS name FROM pg_class c '
            'JOIN pg_namespace n ON n.oid=c.relnamespace '
            "WHERE n.nspname=ANY($1::text[]) AND c.relkind IN ('r', 'm') ORDER BY c.oid",
            list(plan.schemas),
        )
        for table in tables:
            await statement(
                'vacuum:' + table['name'],
                f'VACUUM (FREEZE, ANALYZE) {qualified_name(table["schema"], table["name"])}',
            )
        # Include storage-free partitioned parents, but never analyze unrelated schemas.
        relations = await db.fetch(
            'SELECT n.nspname AS schema, c.relname AS name FROM pg_class c '
            'JOIN pg_namespace n ON n.oid=c.relnamespace '
            "WHERE n.nspname=ANY($1::text[]) AND c.relkind IN ('r', 'm', 'p') ORDER BY c.oid",
            list(plan.schemas),
        )
        if relations:
            await statement(
                'analyze',
                '; '.join('ANALYZE ' + qualified_name(r['schema'], r['name']) for r in relations),
            )
    result['finished_at'] = datetime.now(timezone.utc).isoformat()
    return result


def build_initialization_section(benchmark_runs, options):
    """Separate measured loading counters from DDL and maintenance timings."""
    labels = {
        'schema': 'Create schema',
        'unlogged': 'Set tables UNLOGGED',
        'prepare': 'Prepare generator',
        'data': 'Load data',
        'after_data': 'Post-load processing',
        'logged': 'Set tables LOGGED',
        'indexes': 'Build indexes',
        'constraints': 'Apply constraints',
        'finalize': 'Finalize dataset',
        'vacuum': 'Vacuum and freeze',
        'analyze': 'Analyze relations',
        'restore_settings_and_sync': 'Restore durability settings',
    }
    reports = {}
    for run in benchmark_runs:
        evidence = run.get('initialization')
        if evidence is None:
            if run.get('initialization_performed') is False:
                index = run['iteration']['index']
                source = (
                    'dataset prepared before this run'
                    if run.get('init_policy') == 'skip'
                    else 'dataset initialized in iteration 1'
                )
                reports[f'iteration_{index}_reuse'] = {
                    'header': f'Iteration {index}: initialization skipped',
                    'item_type': 'plain_text',
                    'state': 'expanded',
                    'data': f'Reusing {source}. No reset, loading or VACUUM ANALYZE. '
                    'Cache state and previous workload changes are retained.',
                }
            continue
        index = run['iteration']['index']
        groups = {}
        loads = []
        operations = []
        for phase in evidence['phases']:
            kind = phase['name'].split(':', 1)[0]
            group = groups.setdefault(kind, {'seconds': 0.0, 'phases': [], 'tasks': []})
            group['seconds'] += phase['elapsed_seconds']
            group['phases'].append(phase)
            group['tasks'].extend(phase.get('tasks', []))
            if kind == 'data':
                for task in phase.get('tasks', []):
                    loads.append(
                        [
                            task['name'],
                            round(task['worker_seconds'], 3),
                            f'Rows inserted: {task["rows"]:,}; batches: {task["batches"]:,}',
                        ]
                    )
            elif kind in ('indexes', 'constraints'):
                # Aggregate timings already appear in the summary. Listing them
                # again beside individual tasks invites double-counting.
                action = 'Build index' if kind == 'indexes' else 'Apply constraint'
                for task in phase.get('tasks', []):
                    operations.append(
                        [
                            action,
                            round(task['worker_seconds'], 3),
                            f'{task["name"]}; completed executions: {task["batches"]:,}. '
                            'Time includes execution and commit.',
                        ]
                    )
            else:
                relation = phase['name'].partition(':')[2]
                description = {
                    'schema': 'Create profile schemas and supporting objects: '
                    + ', '.join(evidence.get('schemas', [])),
                    'unlogged': f'Set {relation} to UNLOGGED before bulk loading.',
                    'logged': (
                        f'Set {relation} to LOGGED before bulk loading.'
                        if options['table_mode'] == 'logged'
                        else f'Restore WAL logging for {relation} after loading.'
                    ),
                    'vacuum': f'VACUUM (FREEZE, ANALYZE) {relation}: freeze rows and '
                    'refresh visibility and planner statistics.',
                    'prepare': 'Execute profile setup SQL for data generation.',
                    'after_data': 'Execute profile post-load SQL, including declared '
                    'data corrections; affected rows are not recorded.',
                    'finalize': 'Execute profile finalization SQL, such as materialized '
                    'view refresh and workload bounds setup.',
                    'analyze': 'ANALYZE profile relations, including partitioned parents.',
                    'restore_settings_and_sync': 'Restore pre-load durability settings; '
                    'checkpoint and sync only if fsync changed.',
                }.get(kind, f'Execute custom initialization step {phase["name"]}.')
                operations.append(
                    [
                        labels.get(kind, kind),
                        round(phase['elapsed_seconds'], 3),
                        description,
                    ]
                )

        summary = []
        for kind, group in groups.items():
            tasks = group['tasks']
            if kind == 'data':
                inserted = sum(task['rows'] for task in tasks)
                batches = sum(task['batches'] for task in tasks)
                work = f'{inserted:,} rows inserted; {len(tasks):,} load tasks; {batches:,} batches'
            elif kind in ('indexes', 'constraints'):
                work = f'Tasks: {len(tasks):,}; executions: {sum(t["batches"] for t in tasks):,}'
            elif kind in ('unlogged', 'logged', 'vacuum'):
                work = f'{len(group["phases"]):,} relations'
            else:
                work = {
                    'schema': 'Create tables and supporting objects',
                    'prepare': 'Install generator helpers',
                    'after_data': 'Apply profile post-processing; row counts not collected',
                    'finalize': 'Run profile finalization SQL',
                    'analyze': 'Refresh planner statistics',
                    'restore_settings_and_sync': 'Restore settings; sync storage if fsync changed',
                }.get(kind, 'Execute profile SQL; row counts not collected')
            summary.append([labels.get(kind, kind), round(group['seconds'], 3), work])
        barrier = evidence['replication_barrier']
        summary.append(
            [
                'Wait for replica replay',
                round(barrier['elapsed_seconds'], 3),
                f'{barrier["replicas"]} replicas caught up to {barrier["target_lsn"]}',
            ]
        )
        reports[f'iteration_{index}'] = {
            'header': f'Iteration {index}: initialization summary',
            'description': f'Up to {options["workers"]} load workers; '
            f'batch limit: {options["batch_rows"]:,} rows. '
            f'Tables during load: {options["table_mode"]}; fsync policy: {options["fsync"]}; '
            f'synchronous_commit policy: {options["synchronous_commit"]}; '
            f'fsync after initialization: {evidence["fsync_after"]}. '
            'Times are measured elapsed seconds per stage; per-relation operations are grouped. '
            'Parallel worker times are not added to stage elapsed time.',
            'item_type': 'table',
            'state': 'expanded',
            'collection_status': 'ok',
            'theader': ['Stage', 'Elapsed (s)', 'Work performed'],
            'data': summary,
        }
        reports[f'iteration_{index}_loading'] = {
            'header': f'Iteration {index}: data loading by task',
            'description': 'Inserted rows come from PostgreSQL INSERT/COPY completion counts. '
            'They are not final table sizes: post-load processing may update or delete rows. '
            'Batches count successful loader executions, including single-statement tasks. '
            'Worker time is cumulative execution time including commit, not wall-clock time. '
            'Task names identify loader jobs, not necessarily physical tables; inserting into '
            'a partitioned parent routes rows to its partitions without separate loader jobs.',
            'item_type': 'table',
            'state': 'collapsed',
            'collection_status': 'ok',
            'theader': ['Load task', 'Worker time (s)', 'Details'],
            'data': loads,
        }
        reports[f'iteration_{index}_operations'] = {
            'header': f'Iteration {index}: SQL and maintenance details',
            'description': 'Individual operations, without the stage totals already shown above. '
            'Each row describes the work actually timed. Index tasks can run in parallel, '
            'so adding these times does not give total initialization elapsed time.',
            'item_type': 'table',
            'state': 'collapsed',
            'collection_status': 'ok',
            'theader': ['Operation', 'Time (s)', 'Description'],
            'data': operations,
        }
    return {'header': 'Database initialization', 'state': 'expanded', 'reports': reports}
