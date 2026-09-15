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
    synchronous_commit: str = 'off'
    timeout: float = 300.0

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> LoadOptions:
        return cls(
            workers=int(config.get('init_workers', 4)),
            batch_rows=int(config.get('init_batch_rows', 100_000)),
            table_mode=config.get('init_table_mode', 'unlogged'),
            fsync=config.get('init_fsync', 'off'),
            synchronous_commit=config.get('init_synchronous_commit', 'off'),
            timeout=float(config.get('command_timeout', 300.0)),
        )


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def qualified_name(schema: str, name: str) -> str:
    return quote_identifier(schema) + '.' + quote_identifier(name)


def connection_kwargs(db_conf: dict[str, Any], options: LoadOptions) -> dict[str, Any]:
    kwargs = {key: value for key, value in db_conf.items() if key != 'connect_timeout'}
    kwargs['timeout'] = float(db_conf.get('connect_timeout', 5))
    kwargs['command_timeout'] = options.timeout
    settings = {**kwargs.get('server_settings', {}), 'application_name': 'pg_perf_bench:init'}
    if options.synchronous_commit != 'keep':
        settings['synchronous_commit'] = options.synchronous_commit
    kwargs['server_settings'] = settings
    return kwargs


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
        db = await asyncpg.connect(**connection_kwargs(db_conf, options))
        try:
            await db.execute('SELECT set_config($1, $2, false)', 'search_path', search_path)
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
        finally:
            await db.close()

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
    search_path = ', '.join(quote_identifier(schema) for schema in plan.schemas) + ', public'
    db = await asyncpg.connect(**connection_kwargs(db_conf, options))
    try:

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
        await statement('analyze', 'ANALYZE')
    finally:
        await db.close()
    result['finished_at'] = datetime.now(timezone.utc).isoformat()
    return result


def build_initialization_section(benchmark_runs, options):
    reports = {}
    for run in benchmark_runs:
        evidence = run.get('initialization')
        if evidence is None:
            continue
        index = run['iteration']['index']
        rows = []
        for phase in evidence['phases']:
            tasks = phase.get('tasks', [])
            rows.append(
                [
                    phase['name'],
                    round(phase['elapsed_seconds'], 3),
                    sum(task['rows'] for task in tasks),
                    sum(task['batches'] for task in tasks),
                ]
            )
        barrier = evidence['replication_barrier']
        rows.append(['replica_replay_barrier', round(barrier['elapsed_seconds'], 3), None, None])
        reports[f'iteration_{index}'] = {
            'header': f'Iteration {index}: initialization phases',
            'description': f'Workers: {options["workers"]}; batch rows: {options["batch_rows"]}; '
            f'tables during load: {options["table_mode"]}; fsync during load: {options["fsync"]}; '
            f'loader synchronous_commit: {options["synchronous_commit"]}. '
            f'Final fsync: {evidence["fsync_after"]}; replicas caught up: {barrier["replicas"]}; '
            f'replay target: {barrier["target_lsn"]}. '
            'Rows and batches describe data jobs; elapsed times are wall-clock seconds.',
            'item_type': 'table',
            'state': 'expanded',
            'collection_status': 'ok',
            'theader': ['phase', 'elapsed_seconds', 'rows', 'jobs'],
            'data': rows,
        }
    return {'header': 'Database initialization', 'state': 'expanded', 'reports': reports}
