"""Unified pg_perf_bench command line interface."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pg_perf_bench import __version__
from pg_perf_bench.benchmark import BenchmarkRunner
from pg_perf_bench.collect_info import InfoCollector
from pg_perf_bench.config import RuntimeConfig, build_runtime_config
from pg_perf_bench.const import (
    ALL_INFO_TEMPLATE_JSON_PATH,
    DB_INFO_TEMPLATE_JSON_PATH,
    SYS_INFO_TEMPLATE_JSON_PATH,
    ConnectionType,
    LogLevel,
    WorkloadTypes,
    WorkMode,
)
from pg_perf_bench.contracts import (
    ARTIFACT_SCHEMA_VERSION,
    CONTRACT_VERSION,
    EXIT_CODES,
    envelope,
    redact_mapping,
    redact_text,
)
from pg_perf_bench.errors import ConfigurationError, PgPerfBenchError
from pg_perf_bench.join import ReportJoiner
from pg_perf_bench.log import setup_logger
from pg_perf_bench.report.html import render_from_json
from pg_perf_bench.report.processing import save_report
from pg_perf_bench.validator import validate_content


class CLIArgumentParser(argparse.ArgumentParser):
    """Argument parser which keeps validation failures inside the CLI contract."""

    def error(self, message: str) -> None:
        raise ConfigurationError(message)


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('must be an integer') from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError('must be greater than zero')
    return parsed


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('must be a number') from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError('must be greater than zero')
    return parsed


def parse_pgbench_options(value: str) -> list[int]:
    try:
        values = [positive_int(item.strip()) for item in value.split(',') if item.strip()]
    except argparse.ArgumentTypeError:
        raise
    if not values:
        raise argparse.ArgumentTypeError('must contain at least one positive integer')
    return values


def _add_service_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--report-name')
    parser.add_argument('--output-dir', default='report')
    parser.add_argument('--log-dir', default='log')
    parser.add_argument(
        '--log-level',
        choices=[str(level) for level in LogLevel],
        default=str(LogLevel.INFO),
    )
    parser.add_argument('--clear-logs', action='store_true')


def _add_host_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        '--connection-type',
        choices=[str(value) for value in ConnectionType],
        required=True,
    )
    parser.add_argument('--command-timeout', type=positive_float, default=300.0)
    parser.add_argument('--pg-data-path')
    parser.add_argument('--pg-bin-path')
    parser.add_argument('--container-name')
    parser.add_argument('--ssh-host')
    parser.add_argument('--ssh-port', type=positive_int, default=22)
    parser.add_argument('--ssh-user', default='postgres')
    parser.add_argument('--ssh-key')
    parser.add_argument('--ssh-known-hosts')
    parser.add_argument('--ssh-insecure-no-host-key-check', action='store_true')
    parser.add_argument('--remote-pg-host')
    parser.add_argument('--remote-pg-port', type=positive_int)


def _add_database_args(
    parser: argparse.ArgumentParser,
    *,
    include_custom_config: bool = False,
) -> None:
    parser.add_argument('--pg-host')
    parser.add_argument('--pg-port', type=positive_int)
    parser.add_argument('--pg-user', default='postgres')
    parser.add_argument(
        '--pg-password',
        '--pg-user-password',
        dest='pg_password',
        default=os.environ.get('PGPASSWORD'),
    )
    parser.add_argument('--pg-database')
    parser.add_argument('--connect-timeout', type=positive_float, default=5.0)
    parser.add_argument('--collect-pg-logs', action='store_true')
    if include_custom_config:
        parser.add_argument(
            '--pg-custom-config',
            '--custom-config',
            dest='pg_custom_config',
        )


def _add_collect_parser(subparsers, mode: WorkMode, help_text: str) -> None:
    command = subparsers.add_parser(str(mode), help=help_text)
    _add_service_args(command)
    _add_host_args(command)
    if mode != WorkMode.COLLECT_SYS_INFO:
        _add_database_args(command)


def build_parser() -> argparse.ArgumentParser:
    parser = CLIArgumentParser(
        prog='pg-perf-bench',
        description='Reproducible PostgreSQL benchmarks with environment evidence',
    )
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('--machine', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--request-id', help=argparse.SUPPRESS)
    parser.add_argument('--component-capabilities', action='store_true', help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest='command')

    benchmark = subparsers.add_parser('benchmark', help='Run pgbench and collect evidence')
    _add_service_args(benchmark)
    _add_host_args(benchmark)
    _add_database_args(benchmark, include_custom_config=True)
    benchmark.add_argument(
        '--benchmark-type',
        choices=[str(value) for value in WorkloadTypes],
        required=True,
    )
    benchmark.add_argument('--workload-path')
    iterations = benchmark.add_mutually_exclusive_group(required=True)
    iterations.add_argument('--pgbench-clients', type=parse_pgbench_options)
    iterations.add_argument('--pgbench-time', type=parse_pgbench_options)
    benchmark.add_argument('--init-command', required=True)
    benchmark.add_argument('--workload-command', required=True)
    benchmark.add_argument('--pgbench-path', required=True)
    benchmark.add_argument('--psql-path', required=True)
    benchmark.add_argument(
        '--allow-database-reset',
        action='store_true',
        help='confirm that the selected benchmark database may be dropped and recreated',
    )
    benchmark.add_argument(
        '--drop-os-caches',
        action='store_true',
        help='drop host filesystem caches between iterations (requires sudo)',
    )

    _add_collect_parser(
        subparsers,
        WorkMode.COLLECT_SYS_INFO,
        'Collect host information without a database connection',
    )
    _add_collect_parser(
        subparsers,
        WorkMode.COLLECT_DB_INFO,
        'Collect PostgreSQL information',
    )
    _add_collect_parser(
        subparsers,
        WorkMode.COLLECT_ALL_INFO,
        'Collect host and PostgreSQL information',
    )

    join = subparsers.add_parser('join', help='Compare and combine benchmark reports')
    _add_service_args(join)
    join.add_argument('--join-tasks', '--join-task', dest='join_tasks', required=True)
    join.add_argument('--reference-report')
    join.add_argument('--input-dir', required=True)

    render = subparsers.add_parser('render', help='Render portable HTML from report JSON')
    render.add_argument('--from-json', required=True, dest='from_json')
    render.add_argument('--out', required=True)

    subparsers.add_parser('validate', help='Validate packaged report content')

    plan = subparsers.add_parser('plan', help='Build a deterministic execution plan')
    plan.add_argument('planned_command', choices=[str(mode) for mode in WorkMode])
    plan.add_argument('arguments', nargs=argparse.REMAINDER)

    subparsers.add_parser('capabilities', help='Print component capabilities')
    return parser


def get_args_parser() -> argparse.ArgumentParser:
    return build_parser()


def _legacy_argv(argv: list[str]) -> list[str]:
    mode: str | None = None
    remaining: list[str] = []
    iterator = iter(range(len(argv)))
    skip: set[int] = set()
    for index in iterator:
        if index in skip:
            continue
        argument = argv[index]
        if argument.startswith('--mode='):
            mode = argument.split('=', 1)[1]
            continue
        if argument == '--mode' and index + 1 < len(argv):
            mode = argv[index + 1]
            skip.add(index + 1)
            continue
        remaining.append(argument)
    normalized = remaining if mode is None else [mode, *remaining]
    global_args: list[str] = []
    command_args: list[str] = []
    index = 0
    while index < len(normalized):
        argument = normalized[index]
        if argument in {'--machine', '--component-capabilities'}:
            global_args.append(argument)
        elif argument == '--request-id' and index + 1 < len(normalized):
            global_args.extend(normalized[index : index + 2])
            index += 1
        elif argument.startswith('--request-id='):
            global_args.append(argument)
        else:
            command_args.append(argument)
        index += 1
    return [*global_args, *command_args]


def capabilities() -> dict[str, Any]:
    return {
        'contract_version': CONTRACT_VERSION,
        'component': 'pg_perf_bench',
        'component_version': __version__,
        'artifact_schema_versions': [ARTIFACT_SCHEMA_VERSION],
        'commands': {
            'benchmark': {'mutates_target': True, 'machine_output': True},
            'collect-sys-info': {'mutates_target': False, 'machine_output': True},
            'collect-db-info': {'mutates_target': False, 'machine_output': True},
            'collect-all-info': {'mutates_target': False, 'machine_output': True},
            'join': {'mutates_target': False, 'machine_output': True},
            'render': {'mutates_target': False, 'machine_output': True},
            'validate': {'mutates_target': False, 'machine_output': True},
            'plan': {'mutates_target': False, 'machine_output': True},
        },
        'exit_codes': EXIT_CODES,
        'secret_policy': {
            'password_sources': ['argument', 'PGPASSWORD'],
            'logs_are_redacted': True,
            'artifacts_contain_secrets': False,
        },
        'safety': {
            'benchmark_database_reset_requires_confirmation': True,
            'os_cache_drop_is_opt_in': True,
            'collection_queries_are_read_only': True,
        },
    }


def _runtime_plan(config: RuntimeConfig) -> dict[str, Any]:
    document = redact_mapping(asdict(config))
    document['mode'] = str(config.mode)
    if config.host:
        document['host']['connection_type'] = str(config.host.connection_type)
        if config.host.ssh_key:
            document['host']['ssh_key'] = str(config.host.ssh_key)
        if config.host.ssh_known_hosts:
            document['host']['ssh_known_hosts'] = str(config.host.ssh_known_hosts)
    document['report_dir'] = str(config.report_dir)
    payload = json.dumps(document, sort_keys=True, separators=(',', ':'), default=str)
    return {
        'schema_version': 'pg_perf_bench/plan-v1',
        'plan_hash': 'sha256:' + hashlib.sha256(payload.encode()).hexdigest(),
        'configuration': document,
    }


async def execute_namespace(args: argparse.Namespace, logger) -> dict[str, Any] | None:
    config = build_runtime_config(args)
    run_args = {**config.raw_args, 'mode': config.mode}
    if config.mode == WorkMode.JOIN:
        report = ReportJoiner.join_reports(
            raw_args=run_args,
            join_tasks=args.join_tasks,
            reference_report=args.reference_report,
            input_dir=args.input_dir,
            report_name=config.report_name,
            logger=logger,
        )
        if report is None:
            raise PgPerfBenchError('Reports could not be joined')
        return report

    assert config.host is not None
    connection_kwargs = config.host.connection_kwargs(
        config.database,
        start_if_stopped=config.mode == WorkMode.BENCHMARK,
    )
    log_conf = {
        'collect_pg_logs': config.collect_pg_logs,
        'clear_logs': bool(getattr(args, 'clear_logs', False)),
        'log_level': getattr(args, 'log_level', str(LogLevel.INFO)),
    }
    if config.mode == WorkMode.BENCHMARK:
        assert config.database is not None and config.workload is not None
        workload = config.workload.as_legacy_dict(config.host)
        workload['command_timeout'] = config.host.command_timeout
        return await BenchmarkRunner.run_benchmark_and_collect_metrics(
            args=run_args,
            conn_type=str(config.host.connection_type),
            conn_conf=connection_kwargs,
            db_conf=config.database.as_legacy_dict(),
            workload_conf=workload,
            report_conf={'report_name': config.report_name},
            log_conf=log_conf,
            logger=logger,
        )

    template = {
        WorkMode.COLLECT_SYS_INFO: SYS_INFO_TEMPLATE_JSON_PATH,
        WorkMode.COLLECT_DB_INFO: DB_INFO_TEMPLATE_JSON_PATH,
        WorkMode.COLLECT_ALL_INFO: ALL_INFO_TEMPLATE_JSON_PATH,
    }[config.mode]
    database_conf = config.database.as_asyncpg_kwargs() if config.database is not None else {}
    db_environment = {
        'pg_data_path': config.host.pg_data_path,
        'pg_bin_path': config.host.pg_bin_path,
    }
    if getattr(args, 'pg_custom_config', None):
        db_environment['pg_custom_config'] = args.pg_custom_config
    return await InfoCollector.collect_info(
        args=run_args,
        conn_type=str(config.host.connection_type),
        conn_conf=connection_kwargs,
        db_conf={
            'db_conn_params': database_conf,
            'db_env': db_environment,
        },
        report_conf={
            'report_name': config.report_name,
            'report_template': template,
            'mode': config.mode,
        },
        log_conf=log_conf,
        logger=logger,
    )


def _emit_machine(args, status: str, **kwargs: Any) -> None:
    print(
        json.dumps(
            envelope(
                args.command,
                status,
                component_version=__version__,
                request_id=getattr(args, 'request_id', None),
                **kwargs,
            ),
            indent=2,
            sort_keys=True,
            default=str,
        )
    )


def _collection_warnings(report: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    sections = report.get('sections', {})
    if not isinstance(sections, dict):
        return ['report has no valid sections object']
    for section_name, section in sections.items():
        if not isinstance(section, dict):
            continue
        reports = section.get('reports', {})
        if not isinstance(reports, dict):
            continue
        for report_name, item in reports.items():
            if not isinstance(item, dict):
                continue
            status = item.get('collection_status')
            if status in {'error', 'partial', 'skipped'}:
                reason = item.get('reason') or item.get('data') or status
                warnings.append(f'{section_name}.{report_name}: {reason}')
    return warnings


def _safe_error_message(args: argparse.Namespace, exc: BaseException) -> str:
    secrets = (
        getattr(args, 'pg_password', None),
        os.environ.get('PGPASSWORD'),
    )
    return redact_text(str(exc), secrets)


def _fallback_namespace(argv: list[str]) -> argparse.Namespace:
    request_id = None
    command = 'unknown'
    for index, argument in enumerate(argv):
        if argument.startswith('--request-id='):
            request_id = argument.split('=', 1)[1]
        elif argument == '--request-id' and index + 1 < len(argv):
            request_id = argv[index + 1]
        elif argument.startswith('--mode='):
            command = argument.split('=', 1)[1]
        elif argument == '--mode' and index + 1 < len(argv):
            command = argv[index + 1]
        elif argument in {str(mode) for mode in WorkMode} | {
            'render',
            'validate',
            'plan',
            'capabilities',
        }:
            command = argument
    return argparse.Namespace(
        machine='--machine' in argv,
        request_id=request_id,
        command=command,
        pg_password=None,
    )


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = _fallback_namespace(raw_argv)
    try:
        args = parser.parse_args(_legacy_argv(raw_argv))
        if args.component_capabilities:
            args.command = 'capabilities'
        if args.command is None:
            parser.error('a command is required')
        if args.command == 'capabilities':
            result = capabilities()
            if args.machine:
                _emit_machine(args, 'succeeded', result=result)
            else:
                print(json.dumps(result, indent=2, sort_keys=True))
            return EXIT_CODES['success']
        if args.command == 'validate':
            errors = validate_content()
            if errors:
                if args.machine:
                    _emit_machine(
                        args,
                        'failed',
                        error={'code': 'validation_error', 'details': errors},
                    )
                else:
                    for error in errors:
                        print(f'ERROR: {error}', file=sys.stderr)
                return EXIT_CODES['validation_error']
            if args.machine:
                _emit_machine(args, 'succeeded', result={'valid': True})
            else:
                print('OK')
            return EXIT_CODES['success']
        if args.command == 'render':
            render_from_json(args.from_json, args.out)
            result = {'html': str(Path(args.out).resolve())}
            if args.machine:
                _emit_machine(args, 'succeeded', result=result)
            else:
                print(f'Wrote {args.out}')
            return EXIT_CODES['success']
        if args.command == 'plan':
            nested = build_parser().parse_args([args.planned_command, *args.arguments])
            result = _runtime_plan(build_runtime_config(nested))
            if args.machine:
                _emit_machine(args, 'succeeded', result=result)
            else:
                print(json.dumps(result, indent=2, sort_keys=True, default=str))
            return EXIT_CODES['success']

        logger = setup_logger(
            args.log_level,
            args.clear_logs,
            stream=sys.stderr if args.machine else sys.stdout,
            log_dir=args.log_dir,
        )
        report = asyncio.run(execute_namespace(args, logger))
        if report is None:
            raise PgPerfBenchError('No report was generated')
        config = build_runtime_config(args)
        warnings = _collection_warnings(report)
        warning_secrets = (
            getattr(args, 'pg_password', None),
            os.environ.get('PGPASSWORD'),
        )
        warnings = [redact_text(warning, warning_secrets) for warning in warnings]
        report['collection_summary'] = {
            'status': 'partial' if warnings else 'succeeded',
            'warning_count': len(warnings),
        }
        artifacts = save_report(logger, report, str(config.report_dir))
        result = {'report_name': report['report_name'], 'artifacts': artifacts}
        if args.machine:
            _emit_machine(
                args,
                'partial' if warnings else 'succeeded',
                result=result,
                artifacts=[{'kind': kind, 'path': path} for kind, path in artifacts.items()],
                warnings=warnings,
            )
        return EXIT_CODES['partial'] if warnings else EXIT_CODES['success']
    except ConfigurationError as exc:
        message = _safe_error_message(args, exc)
        if getattr(args, 'machine', False):
            _emit_machine(args, 'failed', error={'code': 'validation_error', 'message': message})
        else:
            print(f'ERROR: {message}', file=sys.stderr)
        return EXIT_CODES['validation_error']
    except (FileNotFoundError, PermissionError, ConnectionError) as exc:
        message = _safe_error_message(args, exc)
        if getattr(args, 'machine', False):
            _emit_machine(
                args,
                'failed',
                error={'code': 'precondition_failed', 'message': message},
            )
        else:
            print(f'ERROR: {message}', file=sys.stderr)
        return EXIT_CODES['precondition_failed']
    except PgPerfBenchError as exc:
        message = _safe_error_message(args, exc)
        if getattr(args, 'machine', False):
            _emit_machine(args, 'failed', error={'code': 'execution_error', 'message': message})
        else:
            print(f'ERROR: {message}', file=sys.stderr)
        return EXIT_CODES['execution_error']
    except KeyboardInterrupt:
        print('Interrupted', file=sys.stderr)
        return 130
    except Exception as exc:
        message = _safe_error_message(args, exc)
        if getattr(args, 'machine', False):
            _emit_machine(
                args,
                'failed',
                error={'code': 'execution_error', 'message': message},
            )
        else:
            print(f'ERROR: {message}', file=sys.stderr)
        return EXIT_CODES['execution_error']
