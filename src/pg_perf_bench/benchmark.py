import os
import platform
import re
import sys
from copy import deepcopy
from typing import Any

import asyncpg

from pg_perf_bench import __version__
from pg_perf_bench.connections import get_connection
from pg_perf_bench.const import (
    BENCHMARK_TEMPLATE_JSON_PATH,
    WorkMode,
    get_datetime_report,
    get_default_report_name,
)
from pg_perf_bench.contracts import ARTIFACT_SCHEMA_VERSION
from pg_perf_bench.db_operations import (
    DBTasks,
    collect_db_logs,
    get_conn_type_tasks,
    run_command_result,
)
from pg_perf_bench.errors import CollectionError
from pg_perf_bench.log import display_user_configuration
from pg_perf_bench.report.commands import fill_info_report
from pg_perf_bench.report.processing import get_report_structure


class BenchmarkRunner:
    """
    A stateless utility class that encapsulates all steps for running
    PostgreSQL performance benchmarks and collecting metrics.
    All methods are static since they share no internal state.
    """

    @staticmethod
    def get_pgbench_results(pgbench_output: str) -> list[int | float]:
        """
        Extracts key performance metrics from the pgbench output string.
        Returns a list of metrics in the following order:
        [clients, duration, transactions, latency_avg, init_conn_time, tps].
        """

        def get_val(iter_matches, val_type: str) -> int | float | None:
            for match_obj in iter_matches:
                sub_str = pgbench_output[match_obj.span()[0] : match_obj.span()[1]]
                val_iter = re.finditer(r'\d+([.,]\d+)?', sub_str)
                for vv in val_iter:
                    numeric_str = sub_str[vv.span()[0] : vv.span()[1]]
                    numeric_str = numeric_str.replace(',', '.')
                    if val_type == 'float':
                        return float(numeric_str)
                    elif val_type == 'int':
                        return int(numeric_str)
            return None

        clients = get_val(re.finditer(r'number\sof\sclients\:\s(\d+)', pgbench_output), 'int')
        duration = get_val(re.finditer(r'duration\:\s(\d+)', pgbench_output), 'int')
        transactions = get_val(
            re.finditer(
                r'number\sof\stransactions\sactually\sprocessed\:\s((\d+)/\d+|\d+)',
                pgbench_output,
            ),
            'int',
        )
        latency_avg = get_val(
            re.finditer(r'latency\saverage\s=\s\d+(?:[.,]\d+)?\sms', pgbench_output),
            'float',
        )
        init_conn_time = get_val(
            re.finditer(
                r'initial\sconnection\stime\s=\s\d+(?:[.,]\d+)?\sms',
                pgbench_output,
            ),
            'float',
        )
        tps = get_val(re.finditer(r'tps\s=\s\d+(?:[.,]\d+)?', pgbench_output), 'float')

        return [
            clients,
            duration,
            transactions,
            latency_avg,
            init_conn_time,
            tps,
        ]

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
        arg_values = {**db_conf, **workload_conf, pgbench_param: iter_amount}
        init_command = workload_conf['init_command']
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
            conn_tasks = get_conn_type_tasks(conn_type)(
                db_conf=workload_conf, conn=conn, logger=logger
            )

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
    ) -> dict[str, Any]:
        init_cmd, workload_cmd = load_iteration
        environment = os.environ.copy()
        password = db_conf.get('password')
        if password:
            environment['PGPASSWORD'] = str(password)
        secrets = (str(password) if password else None,)
        logger.info('Executing benchmark initialization command.')
        init_result = await run_command_result(
            logger,
            init_cmd,
            check=True,
            timeout=command_timeout,
            env=environment,
            secrets=secrets,
        )
        logger.info('Executing pgbench workload command.')
        workload_result = await run_command_result(
            logger,
            workload_cmd,
            check=True,
            timeout=command_timeout,
            env=environment,
            secrets=secrets,
        )
        metrics = BenchmarkRunner.get_pgbench_results(workload_result.stdout)
        return {
            'init': init_result.as_dict(secrets=secrets),
            'workload': workload_result.as_dict(secrets=secrets),
            'metrics': {
                'clients': metrics[0],
                'duration_seconds': metrics[1],
                'transactions': metrics[2],
                'latency_average_ms': metrics[3],
                'initial_connection_time_ms': metrics[4],
                'tps': metrics[5],
            },
            'legacy_metrics': metrics,
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
        logger.info('Starting load iterations...')
        for idx, load_iteration in enumerate(load_iterations, start=1):
            logger.info(f'Preparing for iteration {idx}...')
            await BenchmarkRunner.reset_db_environment(
                logger, conn_type, client, db_conf, workload_conf
            )
            result = deepcopy(
                await BenchmarkRunner.run_benchmark_with_evidence(
                    logger,
                    load_iteration,
                    db_conf=db_conf,
                    command_timeout=float(workload_conf.get('command_timeout', 300.0)),
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
            if log_conf.get('collect_pg_logs'):
                await collect_db_logs(logger, client, db_conn, report)
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
        display_user_configuration(args, logger)

        try:
            report = BenchmarkRunner.setup_report_structure(report_conf, logger)
            load_iterations = BenchmarkRunner.load_iterations_config(db_conf, workload_conf)
            if not load_iterations:
                logger.error('No valid load iterations configured.')
                return None

            connection = BenchmarkRunner.setup_connection(conn_type, conn_conf, logger)
            if not connection:
                return None

            report_data = {
                'args': args,
                'workload_conf': workload_conf,
                'report_conf': report_conf,
            }
            report['benchmark_methodology'] = {
                'database_recreated_before_each_iteration': True,
                'os_caches_dropped_before_each_iteration': bool(
                    workload_conf.get('drop_os_caches')
                ),
            }

            async with connection as client:
                if workload_conf.get('pg_custom_config'):
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

                await BenchmarkRunner.collect_monitoring_metrics(
                    logger, db_conf, report_data, report, log_conf, client
                )

            logger.info('Benchmarking process completed successfully.')
            return report

        except Exception as e:
            logger.error(f'Benchmark failed: {e}')
            if isinstance(e, CollectionError):
                raise
            raise CollectionError(f'Benchmark failed: {e}') from e
