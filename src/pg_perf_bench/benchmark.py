import asyncio
import os
import platform
import re
import sys
import time
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg

from pg_perf_bench import __version__
from pg_perf_bench.client_tools import (
    SUPPORTED_SERVER_MAJORS,
    select_local_clients,
    server_major_from_version_num,
)
from pg_perf_bench.connections import get_connection
from pg_perf_bench.const import (
    BENCHMARK_TEMPLATE_JSON_PATH,
    ConnectionType,
    WorkMode,
    get_datetime_report,
    get_default_report_name,
)
from pg_perf_bench.contracts import ARTIFACT_SCHEMA_VERSION, canonical_hash, file_hash
from pg_perf_bench.db_operations import (
    DBTasks,
    collect_db_logs,
    get_conn_type_tasks,
    run_command_result,
)
from pg_perf_bench.db_operations.patroni import PatroniController
from pg_perf_bench.errors import CollectionError
from pg_perf_bench.executors.process import ProcessResult
from pg_perf_bench.initialization import (
    LoadOptions,
    LoadPlan,
    build_initialization_section,
    load_plan,
)
from pg_perf_bench.initialization_settings import InitializationSettings, initialize_database
from pg_perf_bench.log import display_user_configuration
from pg_perf_bench.managed import (
    MANAGED_NO_DATA,
    add_managed_report_metadata,
    mark_managed_unavailable,
    read_managed_pg_info,
)
from pg_perf_bench.pgbench_metrics import LEGACY_METRIC_KEYS, parse_pgbench_metrics
from pg_perf_bench.report.commands import fill_info_report
from pg_perf_bench.report.processing import get_report_structure
from pg_perf_bench.storage import (
    build_storage_section,
    collect_storage_snapshot,
    vacuum_analyze,
)
from pg_perf_bench.system_metrics import (
    build_system_metrics_section,
    collect_system_metrics,
    infer_pgbench_duration,
)
from pg_perf_bench.workloads import build_workload_evidence


class BenchmarkRunner:
    """
    A stateless utility class that encapsulates all steps for running
    PostgreSQL performance benchmarks and collecting metrics.
    All methods are static since they share no internal state.
    """

    @staticmethod
    def maximum_tps(benchmark_runs: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Return the complete winning point from a client/load sweep."""
        candidates = [
            run
            for run in benchmark_runs
            if isinstance(run.get('metrics'), dict)
            and isinstance(run['metrics'].get('tps'), (int, float))
            and not isinstance(run['metrics'].get('tps'), bool)
        ]
        if not candidates:
            return None
        best = max(candidates, key=lambda run: float(run['metrics']['tps']))
        return {
            'tps': best['metrics']['tps'],
            'iteration': deepcopy(best.get('iteration')),
            'metrics': deepcopy(best['metrics']),
        }

    @staticmethod
    def environment_evidence(report: dict[str, Any]) -> dict[str, Any]:
        """Build stable dimension hashes without volatile usage counters."""
        managed_info = report.get('managed_pg_info')
        if managed_info:
            load_generator = (report.get('postgresql_compatibility') or {}).get('load_generator')
            identity = {
                'managed_pg_info_hash': managed_info['hash'],
                'load_generator': load_generator,
            }
            return {
                'schema_version': 'pg_perf_bench/environment-evidence-v1',
                'identity_hash': canonical_hash(identity),
                'dimensions': {
                    'managed_instance': {'hash': managed_info['hash'], 'items': ['managed_pg_info']}
                },
                'load_generator_hash': canonical_hash(load_generator),
                'system_metrics_collection_scope': 'unavailable_managed_postgresql',
            }
        system_reports = report['sections']['system']['reports']

        def stable_value(item_name: str) -> Any:
            value = deepcopy(system_reports[item_name].get('data'))
            if item_name == 'ip_br_addr' and isinstance(value, str):
                return re.sub(r'@if\d+', '@if*', value)
            return value

        dimension_items = {
            'kernel_os': (
                'uname_a',
                'etc_os_release',
                'sysctl_vm',
                'sysctl_net_ipv4_tcp',
                'sysctl_net_ipv4_udp',
            ),
            # ``lshw_processor`` includes the instantaneous CPU clock.  It is
            # useful raw evidence, but it is not hardware identity: frequency
            # changes naturally with load and power management between runs.
            'cpu': ('cpu_info',),
            'memory_capacity': ('total_ram', 'lshw_memory'),
            'storage_hardware': ('lshw_storage', 'lshw_disk', 'lshw_volume'),
            # Interface addresses and Docker bridge/veth names are runtime
            # topology, not hardware identity.  The raw ``ip -br addr`` output
            # remains in the report for inspection.
            'network_hardware': ('lshw_network',),
        }
        dimensions = {
            name: {
                'hash': canonical_hash(
                    {item_name: stable_value(item_name) for item_name in item_names}
                ),
                'items': list(item_names),
            }
            for name, item_names in dimension_items.items()
        }
        compatibility = report.get('postgresql_compatibility') or {}
        load_generator = compatibility.get('load_generator') or {}
        first_run = next(iter(report.get('benchmark_runs') or []), {})
        collection_scope = (first_run.get('system_metrics') or {}).get('collection_scope')
        identity = {
            'dimensions': {name: item['hash'] for name, item in dimensions.items()},
            'load_generator': load_generator,
            'system_metrics_collection_scope': collection_scope,
        }
        return {
            'schema_version': 'pg_perf_bench/environment-evidence-v1',
            'identity_hash': canonical_hash(identity),
            'dimensions': dimensions,
            'load_generator_hash': canonical_hash(load_generator),
            'system_metrics_collection_scope': collection_scope,
        }

    @staticmethod
    def get_pgbench_results(pgbench_output: str) -> list[int | float | None]:
        """Return the historical six-column metrics table."""
        metrics = parse_pgbench_metrics(pgbench_output)
        return [metrics[key] for key in LEGACY_METRIC_KEYS]

    @staticmethod
    def get_filled_load_commands(
        db_conf: dict,
        workload_conf: dict,
        pgbench_param: str,
        iter_amount: Any,
    ) -> list[str]:
        """
        Replaces placeholders (ARG_*) in the init_command and workload_command
        with actual config values and iteration-specific parameter.
        """
        arg_values = {
            'python_path': sys.executable,
            **db_conf,
            **workload_conf,
            pgbench_param: iter_amount,
        }
        init_command = workload_conf['init_command']
        if workload_conf.get('init_mode') == 'fast':
            init_command = 'common-loader:' + str(workload_conf['init_entrypoint'])
        workload_command = workload_conf['workload_command']

        for key, value in arg_values.items():
            if isinstance(key, str):
                placeholder = f'ARG_{key.upper()}'
                init_command = init_command.replace(placeholder, str(value))
                workload_command = workload_command.replace(placeholder, str(value))

        unresolved = sorted(
            set(re.findall(r'ARG_[A-Z][A-Z0-9_]*', init_command + '\n' + workload_command))
        )
        if unresolved:
            raise ValueError('Unresolved workload placeholders: ' + ', '.join(unresolved))

        return [init_command, workload_command]

    @staticmethod
    def load_iterations_config(db_conf: dict, workload_conf: dict) -> list[list[str]]:
        """
        Builds a list of [init_command, workload_command] pairs for each iteration.
        """
        db_conf_pg = {f'pg_{k}': v for k, v in db_conf.items()}

        pgbench_param_name = workload_conf.get('pgbench_iter_name')
        iter_list = workload_conf.get('pgbench_iter_list')
        if (
            not workload_conf
            or not isinstance(workload_conf, dict)
            or not pgbench_param_name
            or not iter_list
            or not isinstance(iter_list, list)
            or 'init_command' not in workload_conf
            or 'workload_command' not in workload_conf
        ):
            return []

        return [
            BenchmarkRunner.get_filled_load_commands(
                db_conf_pg, workload_conf, pgbench_param_name, iteration
            )
            for iteration in iter_list
        ]

    @staticmethod
    async def reset_db_environment(
        logger, conn_type: str, conn, db_conf: dict, workload_conf: dict
    ) -> None:
        """
        Fully resets the database environment before a test iteration.
        """
        if not workload_conf.get('allow_database_reset'):
            raise CollectionError(
                'Database reset was not explicitly confirmed for this benchmark run'
            )
        try:
            db_tasks = DBTasks(db_conf, logger)
            if workload_conf.get('managed_pg_info') or conn_type == ConnectionType.MANAGED:
                await db_tasks.check_db_access()
                await db_tasks.drop_db()
                await db_tasks.init_db()
                await db_tasks.check_user_db_access()
                return
            conn_tasks = get_conn_type_tasks(conn_type)(
                db_conf=workload_conf, conn=conn, logger=logger
            )

            patroni = await PatroniController.detect(conn, workload_conf['pg_data_path'])
            if patroni:
                patroni.validate_options(workload_conf)
                await patroni.verify_database(db_tasks)
                await db_tasks.drop_db()
                await conn_tasks.sync()
                await patroni.restart(db_tasks, logger)
                await db_tasks.init_db()
                await db_tasks.check_user_db_access()
                return

            try:
                await conn_tasks.start_db()
            except Exception as e:
                logger.warning(str(e))

            await db_tasks.check_db_access()
            await db_tasks.drop_db()
            await conn_tasks.stop_db()
            await conn_tasks.sync()
            if workload_conf.get('drop_os_caches'):
                await conn_tasks.drop_caches()
            await conn_tasks.start_db()
            await db_tasks.check_db_access()
            await db_tasks.init_db()
            await db_tasks.check_user_db_access()

        except Exception as e:
            raise RuntimeError(f'Failed to reset DB environment:\n{str(e)}') from e

    @staticmethod
    async def run_benchmark(logger, load_iteration: list[str]) -> list[Any]:
        """
        Runs a benchmark iteration and returns the parsed pgbench metrics.
        """
        init_cmd, workload_cmd = load_iteration

        logger.info(f'Executing init_command:\n {init_cmd}')
        await run_command_result(logger, init_cmd, check=True)

        logger.info(f'Executing workload_command:\n {workload_cmd}')
        workload_result = await run_command_result(
            logger,
            workload_cmd,
            check=True,
        )
        perf_result = workload_result.stdout

        if not perf_result.strip():
            logger.warning('Workload command returned an empty or whitespace-only result.')
        else:
            logger.debug(f'Result of pgbench iteration:\n{perf_result}')
        metrics = BenchmarkRunner.get_pgbench_results(perf_result)
        if metrics[5] is None:
            raise CollectionError(
                'pgbench completed but TPS could not be parsed; raw output is preserved'
            )
        return metrics

    @staticmethod
    async def run_benchmark_with_evidence(
        logger,
        load_iteration: list[str],
        *,
        db_conf: dict[str, Any],
        command_timeout: float,
        connection_type: str = 'local',
        connection: Any = None,
        system_metrics_interval: float = 1.0,
        system_metrics_duration: float | None = None,
        initialization_plan: LoadPlan | None = None,
        initialization_options: LoadOptions | None = None,
        required_replicas=None,
    ) -> dict[str, Any]:
        init_cmd, workload_cmd = load_iteration
        environment = os.environ.copy()
        environment.update(
            {
                'PGHOST': str(db_conf.get('host', '')),
                'PGPORT': str(db_conf.get('port', '')),
                'PGUSER': str(db_conf.get('user', '')),
                'PGDATABASE': str(db_conf.get('database', '')),
            }
        )
        password = db_conf.get('password')
        if password:
            environment['PGPASSWORD'] = str(password)
        secrets = (str(password) if password else None,)
        initialization = None
        if initialization_plan is not None:
            started_at = datetime.now(timezone.utc).isoformat()
            started = time.monotonic()
            initialization = await initialize_database(
                logger,
                initialization_plan,
                db_conf,
                initialization_options or LoadOptions(timeout=command_timeout),
                connection_type,
                connection,
                required_replicas=required_replicas,
            )
            init_result = ProcessResult(
                argv=(init_cmd,),
                returncode=0,
                stderr='',
                started_at=started_at,
                elapsed_seconds=time.monotonic() - started,
                stdout='Common loader completed: data, LOGGED, indexes, constraints, '
                'VACUUM ANALYZE, restored settings, replica replay barrier.\n',
            )
        else:
            logger.info('Executing benchmark initialization command.')
            init_result = await run_command_result(
                logger,
                init_cmd,
                check=True,
                timeout=command_timeout,
                env=environment,
                secrets=secrets,
            )
            await vacuum_analyze(logger, db_conf, command_timeout)
        logger.info('Collecting storage sizes before workload.')
        storage_before = await collect_storage_snapshot(logger, db_conf)
        logger.info('Executing pgbench workload command.')
        sampler_task = None
        if connection is not None and connection_type != ConnectionType.MANAGED:
            sampling_duration = infer_pgbench_duration(
                workload_cmd,
                system_metrics_duration,
            )
            sampler_task = asyncio.create_task(
                collect_system_metrics(
                    connection_type=connection_type,
                    connection=connection,
                    duration_seconds=sampling_duration,
                    interval_seconds=system_metrics_interval,
                ),
                name='pg-perf-bench:system-metrics',
            )
        try:
            workload_result = await run_command_result(
                logger,
                workload_cmd,
                check=True,
                timeout=command_timeout,
                env=environment,
                secrets=secrets,
            )
            # Providers can finish after pgbench. Keep size-query CPU and I/O
            # outside their final sampling interval.
            system_metrics = await sampler_task if sampler_task is not None else None
            logger.info('Collecting storage sizes after workload.')
            storage_after = await collect_storage_snapshot(logger, db_conf)
        except BaseException:
            if sampler_task is not None and not sampler_task.done():
                sampler_task.cancel()
            if sampler_task is not None:
                await asyncio.gather(sampler_task, return_exceptions=True)
            raise
        metrics = parse_pgbench_metrics(workload_result.stdout)
        if metrics['tps'] is None:
            raise CollectionError(
                'pgbench completed but TPS could not be parsed; raw output is preserved'
            )
        result = {
            'init': init_result.as_dict(secrets=secrets),
            'workload': workload_result.as_dict(secrets=secrets),
            'metrics': metrics,
            'legacy_metrics': [metrics[key] for key in LEGACY_METRIC_KEYS],
            'storage': {'before_workload': storage_before, 'after_workload': storage_after},
        }
        if system_metrics is not None:
            result['system_metrics'] = system_metrics
        if initialization is not None:
            result['initialization'] = initialization
        return result

    @staticmethod
    async def collect_compatibility_evidence(
        db_conf: dict[str, Any], workload_conf: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate server 10-18 and prove that newest clients run locally."""
        pgbench, psql = select_local_clients(
            str(workload_conf.get('pgbench_path') or '') or None,
            str(workload_conf.get('psql_path') or '') or None,
        )
        connection_kwargs = {
            key: value
            for key, value in db_conf.items()
            if key not in {'database', 'connect_timeout'}
        }
        connection_kwargs['database'] = 'postgres'
        connection_kwargs['timeout'] = float(db_conf.get('connect_timeout', 5.0))
        connection = await asyncpg.connect(**connection_kwargs)
        try:
            version_num = int(await connection.fetchval('SHOW server_version_num'))
            version_text = str(await connection.fetchval('SHOW server_version'))
        finally:
            await connection.close()
        server_major = server_major_from_version_num(version_num)
        return {
            'schema_version': 'pg_perf_bench/postgresql-compatibility-v1',
            'supported_server_majors': list(SUPPORTED_SERVER_MAJORS),
            'server': {
                'major': server_major,
                'version_num': version_num,
                'version': version_text,
            },
            'load_generator': {
                'execution_host': 'pg_perf_bench_local_host',
                'pgbench': pgbench.as_dict(),
                'psql': psql.as_dict(),
                'newest_installed_client_required': True,
            },
        }

    @staticmethod
    def setup_report_structure(report_conf: dict, logger) -> dict:
        """
        Prepares and returns the base report structure.
        """
        report = get_report_structure(BENCHMARK_TEMPLATE_JSON_PATH)
        report['artifact_schema_version'] = ARTIFACT_SCHEMA_VERSION
        report['generator'] = {'name': 'pg_perf_bench', 'version': __version__}
        report['runtime'] = {
            'python': sys.version.split()[0],
            'platform': platform.platform(),
        }
        report['description'] = get_datetime_report('%d/%m/%Y %H:%M:%S')
        if report_conf.get('report_name') is None:
            report['report_name'] = f'{WorkMode.BENCHMARK}-{get_default_report_name()}'
            report_conf['report_name'] = report['report_name']
        else:
            report['report_name'] = report_conf.get('report_name')

        logger.info('Report structure initialized.')
        return report

    @staticmethod
    def build_invocation_summary(
        conn_type: str,
        db_conf: dict[str, Any],
        workload_conf: dict[str, Any],
    ) -> dict[str, Any]:
        """Return the safe, user-facing parameters that define a benchmark run."""
        managed = bool(workload_conf.get('managed_pg_info'))
        return {
            'schema_version': 'pg_perf_bench/invocation-v1',
            'mode': 'benchmark',
            'connection_type': str(conn_type),
            'managed_postgresql': managed,
            'database': {
                'host': db_conf.get('host'),
                'port': db_conf.get('port'),
                'name': db_conf.get('database'),
                'user': db_conf.get('user'),
            },
            'workload': {
                'profile': workload_conf.get('workload_profile'),
                'scale': workload_conf.get('workload_scale'),
                'duration_seconds': workload_conf.get('workload_duration_seconds'),
                'iteration_parameter': workload_conf.get('pgbench_iter_name'),
                'iteration_values': list(workload_conf.get('pgbench_iter_list') or []),
            },
            'metrics': {
                'engine': None if managed else 'pg_diag',
                'interval_seconds': None
                if managed
                else workload_conf.get('system_metrics_interval'),
                'duration_override_seconds': (
                    None if managed else workload_conf.get('system_metrics_duration')
                ),
            },
            'safety': {
                'database_recreated_before_each_iteration': True,
                'database_reset_authorized': bool(workload_conf.get('allow_database_reset')),
                'os_caches_dropped_before_each_iteration': bool(
                    workload_conf.get('drop_os_caches')
                ),
            },
        }

    @staticmethod
    def setup_connection(conn_type: str, conn_conf: dict, logger):
        """
        Initializes a database connection object based on the selected type.
        """
        connection_class = get_connection(conn_type)
        if not connection_class:
            logger.error(f'No valid connection factory for type: {conn_type}')
            return None
        logger.info(f'Connection type selected: {conn_type}')

        connection = connection_class(**conn_conf)
        connection.logger = logger
        return connection

    @staticmethod
    async def run_benchmark_iterations(
        logger,
        load_iterations: list[list[str]],
        conn_type: str,
        client,
        db_conf: dict,
        workload_conf: dict,
    ) -> list[dict[str, Any]]:
        """
        Executes all load test iterations sequentially and gathers results.
        """
        perf_results = []
        initialization_plan = None
        initialization_options = None
        if workload_conf.get('init_mode') == 'fast':
            initialization_plan = load_plan(
                Path(workload_conf['workload_path']).expanduser(),
                workload_conf['init_entrypoint'],
                workload_conf.get('workload_scale', 1.0),
            )
            initialization_options = LoadOptions.from_config(workload_conf)
        logger.info('Starting load iterations...')
        for idx, load_iteration in enumerate(load_iterations, start=1):
            logger.info(f'Preparing for iteration {idx}...')
            required_replicas = None
            if initialization_plan is not None or InitializationSettings.recovery_pending():
                # Recover an interrupted fsync override and check privileges
                # before the destructive reset. Reset can restart PostgreSQL,
                # so initialization acquires a fresh connection afterward.
                preflight = InitializationSettings(
                    logger,
                    db_conf,
                    initialization_options or LoadOptions(fsync='keep'),
                    conn_type,
                    client,
                )
                await preflight.open(recover_only=initialization_plan is None)
                required_replicas = preflight.replicas.copy()
                await preflight.close()
            await BenchmarkRunner.reset_db_environment(
                logger, conn_type, client, db_conf, workload_conf
            )
            result = deepcopy(
                await BenchmarkRunner.run_benchmark_with_evidence(
                    logger,
                    load_iteration,
                    db_conf=db_conf,
                    command_timeout=float(workload_conf.get('command_timeout', 300.0)),
                    connection_type=conn_type,
                    connection=client,
                    system_metrics_interval=float(
                        workload_conf.get('system_metrics_interval', 1.0)
                    ),
                    system_metrics_duration=workload_conf.get('system_metrics_duration'),
                    initialization_plan=initialization_plan,
                    initialization_options=initialization_options,
                    required_replicas=required_replicas,
                )
            )
            iteration_values = workload_conf.get('pgbench_iter_list', [])
            result['iteration'] = {
                'index': idx,
                'parameter': workload_conf.get('pgbench_iter_name'),
                'value': (iteration_values[idx - 1] if idx - 1 < len(iteration_values) else None),
            }
            perf_results.append(result)
            logger.info(f'Iteration {idx} completed.')
        return perf_results

    @staticmethod
    async def collect_monitoring_metrics(
        logger,
        db_conf: dict,
        report_data: dict,
        report: dict,
        log_conf: dict,
        client,
    ) -> None:
        """
        Collects system and database metrics after the benchmark is complete.
        """
        logger.info('Connecting to DB for monitoring metrics...')
        connection_kwargs = {
            key: value for key, value in db_conf.items() if key != 'connect_timeout'
        }
        connection_kwargs['timeout'] = float(db_conf.get('connect_timeout', 5.0))
        connection_kwargs['server_settings'] = {
            'default_transaction_read_only': 'on',
            'statement_timeout': '10000',
        }
        db_conn = await asyncpg.connect(**connection_kwargs)
        try:
            await fill_info_report(logger, client, db_conn, report_data, report)
            logger.info('Monitoring data collected.')
            if log_conf.get('collect_pg_logs') and not report.get('managed_pg_info'):
                await collect_db_logs(
                    logger,
                    client,
                    db_conn,
                    report,
                    log_conf.get('db_logs_dir'),
                )
        finally:
            await db_conn.close()
            logger.info('Monitoring DB connection closed.')

    @staticmethod
    async def run_benchmark_and_collect_metrics(
        args: dict,
        conn_type: str,
        conn_conf: dict,
        db_conf: dict,
        workload_conf: dict,
        report_conf: dict,
        log_conf: dict,
        logger,
    ) -> dict[str, Any] | None:
        """
        Main entry point to execute the full benchmarking workflow.
        """
        started_at = datetime.now(timezone.utc).isoformat()
        started_clock = time.monotonic()
        display_user_configuration(args, logger)

        try:
            report = BenchmarkRunner.setup_report_structure(report_conf, logger)
            managed_path = workload_conf.get('managed_pg_info')
            if managed_path:
                add_managed_report_metadata(report, read_managed_pg_info(managed_path))
            report['invocation'] = BenchmarkRunner.build_invocation_summary(
                conn_type,
                db_conf,
                workload_conf,
            )
            load_iterations = BenchmarkRunner.load_iterations_config(db_conf, workload_conf)
            if not load_iterations:
                logger.error('No valid load iterations configured.')
                return None

            connection = (
                nullcontext(None)
                if managed_path
                else BenchmarkRunner.setup_connection(conn_type, conn_conf, logger)
            )
            if not connection:
                return None

            report_data = {
                'args': args,
                'workload_conf': workload_conf,
                'report_conf': report_conf,
                'managed_postgresql': bool(managed_path),
            }
            workload_evidence = build_workload_evidence(workload_conf, load_iterations)
            report_data['workload_evidence'] = workload_evidence
            report['workload_evidence'] = workload_evidence
            report['benchmark_methodology'] = {
                'database_recreated_before_each_iteration': True,
                'server_restarted_before_each_iteration': not bool(managed_path),
                'managed_postgresql': bool(managed_path),
                'os_caches_dropped_before_each_iteration': bool(
                    workload_conf.get('drop_os_caches')
                ),
                'workload_definition_hash': workload_evidence['definition_hash'],
                'workload_execution_hash': workload_evidence['execution_hash'],
                'vacuum_analyze_before_each_workload': True,
                'storage_snapshots': ['before_workload', 'after_workload'],
                'initialization': workload_evidence['initialization'],
                'system_metrics_engine': None if managed_path else 'pg_diag',
                'system_metrics_collected_during_workload': not bool(managed_path),
                'system_metrics_interval_seconds': (
                    None
                    if managed_path
                    else float(workload_conf.get('system_metrics_interval', 1.0))
                ),
                'system_metrics_duration_override': (
                    None if managed_path else workload_conf.get('system_metrics_duration')
                ),
            }

            async with connection as client:
                compatibility = await BenchmarkRunner.collect_compatibility_evidence(
                    db_conf,
                    workload_conf,
                )
                report['postgresql_compatibility'] = compatibility
                if workload_conf.get('pg_custom_config') and not managed_path:
                    patroni = await PatroniController.detect(client, workload_conf['pg_data_path'])
                    if patroni:
                        patroni.validate_options(workload_conf)
                    custom_path = workload_conf['pg_custom_config']
                    db_path = workload_conf.get('pg_data_path', '')
                    logger.info(f'Sending custom PostgreSQL config: {custom_path}')
                    remote_config = await client.send_pg_config_file(custom_path, db_path)
                    logger.info(f'Config applied: {custom_path} -> {remote_config}')

                benchmark_runs = await BenchmarkRunner.run_benchmark_iterations(
                    logger,
                    load_iterations,
                    conn_type,
                    client,
                    db_conf,
                    workload_conf,
                )
                report_data['benchmark_runs'] = benchmark_runs
                report_data['pgbench_outputs'] = [run['legacy_metrics'] for run in benchmark_runs]
                report['benchmark_runs'] = benchmark_runs
                report['maximum_tps'] = BenchmarkRunner.maximum_tps(benchmark_runs)
                report['sections']['storage'] = build_storage_section(benchmark_runs)
                if workload_conf.get('init_mode') == 'fast':
                    report['sections']['initialization'] = build_initialization_section(
                        benchmark_runs, workload_evidence['initialization']
                    )
                report['sections']['os_metrics'] = build_system_metrics_section(benchmark_runs)
                if managed_path:
                    section = report['sections']['os_metrics']
                    section['description'] = MANAGED_NO_DATA
                    for item in section['reports'].values():
                        item['header'] = (
                            item['header'].removeprefix('os.').replace('_', ' ').title()
                        )
                        item['description'] = ''
                        mark_managed_unavailable(item)

                await BenchmarkRunner.collect_monitoring_metrics(
                    logger, db_conf, report_data, report, log_conf, client
                )
                report['environment_evidence'] = BenchmarkRunner.environment_evidence(report)
                effective_settings = report['sections']['db']['reports']['pg_settings'].get('data')
                database_evidence = {
                    'schema_version': 'pg_perf_bench/database-configuration-evidence-v1',
                    'effective_settings_hash': canonical_hash(effective_settings),
                }
                custom_config = workload_conf.get('pg_custom_config')
                if custom_config:
                    custom_path = Path(str(custom_config)).expanduser()
                    database_evidence['supplied_config'] = {
                        'name': custom_path.name,
                        'hash': file_hash(custom_path),
                    }
                report['database_configuration_evidence'] = database_evidence

            logger.info('Benchmarking process completed successfully.')
            report['timing'] = {
                'started_at': started_at,
                'finished_at': datetime.now(timezone.utc).isoformat(),
                'elapsed_seconds': time.monotonic() - started_clock,
            }
            return report

        except Exception as e:
            logger.error(f'Benchmark failed: {e}')
            if isinstance(e, CollectionError):
                raise
            raise CollectionError(f'Benchmark failed: {e}') from e
