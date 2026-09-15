"""Connection-local settings shared by the benchmark and its client preflight."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from pg_perf_bench.errors import ConfigurationError
from pg_perf_bench.executors.process import run_local_process
from pg_perf_bench.initialization import profile_search_path


async def close_diagnostic_connection(db):
    """Do not leave read-only mode/timeouts on a backend returned to a session pool."""

    async def cleanup():
        try:
            if not db.is_closed():
                await db.execute(
                    'RESET default_transaction_read_only; '
                    'RESET statement_timeout; RESET lock_timeout',
                    timeout=10,
                )
        finally:
            await db.close()

    task = asyncio.create_task(cleanup())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def workload_environment(db_conf, plan=None):
    environment = os.environ.copy()
    for name, key in (
        ('PGHOST', 'host'),
        ('PGPORT', 'port'),
        ('PGUSER', 'user'),
        ('PGDATABASE', 'database'),
    ):
        environment[name] = str(db_conf.get(key, ''))
    if db_conf.get('password'):
        environment['PGPASSWORD'] = str(db_conf['password'])
    if plan is not None:
        # PGOPTIONS uses PostgreSQL option escaping, not shell quoting.
        path = profile_search_path(plan)
        escaped = ''.join('\\' + c if c.isspace() or c == '\\' else c for c in path)
        environment['PGOPTIONS'] = (
            environment.get('PGOPTIONS', '') + ' -c search_path=' + escaped
        ).strip()
    return environment


async def check_workload_session(db_conf, plan, *, psql_path, pgbench_path, timeout):
    """Probe libpq startup options before resetting a pre-created database.

    Unlike asyncpg, pgbench cannot run session setup SQL outside its workload.
    psql uses the same libpq environment and must resolve the requested schema list.
    parse_ident handles both quoted identifiers and pooler-normalized spellings,
    even when profile schemas do not exist yet.
    """
    sql = r"""
SELECT json_agg((pg_catalog.parse_ident(m[1]))[1])
FROM pg_catalog.regexp_matches(
    pg_catalog.current_setting('search_path'),
    '("(?:[^"]|"")*"|[^,[:space:]]+)[[:space:]]*(,|$)', 'g'
) AS m;
"""
    result = await run_local_process(
        [psql_path, '-X', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-c', sql],
        env=workload_environment(db_conf, plan),
        timeout=timeout,
        secrets=(db_conf.get('password'),),
    )
    try:
        actual = json.loads(result.stdout)
    except ValueError as exc:
        raise ConfigurationError('Cannot verify workload search_path before reset') from exc
    expected = [*plan.schemas, 'public']
    if actual != expected:
        raise ConfigurationError(
            f'Workload connection search_path was rewritten: expected {expected!r}, '
            f'got {actual!r}. No schemas were reset. With Odyssey, enable '
            'smart_search_path_enquoting=yes or use a direct primary endpoint. '
            'Session pooling must preserve libpq search_path startup options.'
        )
    # pgbench reuses fixed prepared-statement names across processes. A session
    # pool must clean them on disconnect, including between client-count points.
    with TemporaryDirectory(prefix='pg-perf-session-') as directory:
        script = Path(directory) / 'probe.sql'
        script.write_text('SELECT 1;\n', encoding='utf-8')
        for _ in range(2):
            probe = await run_local_process(
                [pgbench_path, '-n', '-M', 'prepared', '-c', '1', '-t', '1', '-f', str(script)],
                env={**workload_environment(db_conf, plan), 'LC_ALL': 'C'},
                timeout=timeout,
                check=False,
                secrets=(db_conf.get('password'),),
            )
            if probe.returncode != 0 or 'error:' in probe.stderr.lower():
                raise ConfigurationError(
                    'pgbench prepared-statement preflight failed before schema reset. '
                    'Session pooling must clean prepared statements between clients; '
                    'with Odyssey, enable pool_discard=yes or use a direct primary endpoint. '
                    f'Details: {probe.as_dict(secrets=(db_conf.get("password"),))["stderr"]}'
                )
