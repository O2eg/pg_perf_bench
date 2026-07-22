"""Unified pg_perf_bench command line interface."""

from __future__ import annotations

import argparse
import asyncio
import copy
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
    CAPABILITY_SCHEMA_VERSION,
    CONTRACT_VERSION,
    EXIT_CODES,
    MACHINE_INTERFACE,
    canonical_hash,
    envelope,
    redact_mapping,
    redact_text,
)
from pg_perf_bench.errors import ConfigurationError, PgPerfBenchError, PreconditionError
from pg_perf_bench.join import ReportJoiner
from pg_perf_bench.join_catalog import join_task_catalog
from pg_perf_bench.log import setup_logger
from pg_perf_bench.orchestration import (
    artifact_descriptor,
    load_artifact,
    summarize_artifact,
)
from pg_perf_bench.report.html import render_from_json
from pg_perf_bench.report.processing import save_report
from pg_perf_bench.validator import validate_content
from pg_perf_bench.workloads import bundled_profile_names, workload_profile_catalog


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
    parser.add_argument('--out', '--output-dir', dest='output_dir', default='report')
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
    parser.add_argument('--host', '--pg-host', dest='pg_host')
    parser.add_argument('--port', '--pg-port', dest='pg_port', type=positive_int)
    parser.add_argument('--user', '--pg-user', dest='pg_user', default='postgres')
    parser.add_argument(
        '--password',
        '--pg-password',
        '--pg-user-password',
        dest='pg_password',
        default=os.environ.get('PGPASSWORD'),
    )
    parser.add_argument('--database', '--pg-database', dest='pg_database')
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
    )
    benchmark.add_argument('--workload-profile', choices=bundled_profile_names())
    benchmark.add_argument('--workload-path')
    benchmark.add_argument('--workload-scale', type=positive_float, default=1.0)
    benchmark.add_argument(
        '--workload-duration-seconds',
        type=positive_int,
        help='pgbench duration for a bundled profile (defaults to the profile value)',
    )
    iterations = benchmark.add_mutually_exclusive_group(required=True)
    iterations.add_argument('--pgbench-clients', type=parse_pgbench_options)
    iterations.add_argument('--pgbench-time', type=parse_pgbench_options)
    benchmark.add_argument('--init-command')
    benchmark.add_argument('--workload-command')
    benchmark.add_argument(
        '--pgbench-path',
        help='local pgbench executable; defaults to the newest installed version',
    )
    benchmark.add_argument(
        '--psql-path',
        help='local psql executable; defaults to the version paired with pgbench',
    )
    benchmark.add_argument(
        '--system-metrics-interval',
        type=positive_float,
        default=1.0,
        help='sampling interval for pg_diag CPU, RAM, disk, and network metrics',
    )
    benchmark.add_argument(
        '--system-metrics-duration',
        type=positive_float,
        help='sampling window; by default it is inferred from pgbench --time/-T',
    )
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
    benchmark.add_argument('--plan-hash', help=argparse.SUPPRESS)

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
    join_inputs = join.add_mutually_exclusive_group(required=True)
    join_inputs.add_argument('--input-dir')
    join_inputs.add_argument(
        '--report',
        dest='reports',
        action='append',
        help='Exact report path; repeat for every report to join',
    )

    render = subparsers.add_parser('render', help='Render portable HTML from report JSON')
    render.add_argument('--from-json', required=True, dest='from_json')
    render.add_argument('--out', required=True)

    validate_artifact = subparsers.add_parser(
        'validate-artifact', help='Validate a pg_perf_bench JSON artifact'
    )
    validate_artifact.add_argument('artifact')

    summarize = subparsers.add_parser(
        'summarize', help='Print a compact deterministic benchmark summary'
    )
    summarize.add_argument('artifact')

    subparsers.add_parser('validate', help='Validate packaged report content')
    subparsers.add_parser('profiles', help='List bundled maximum-TPS workload profiles')
    subparsers.add_parser('join-tasks', help='List documented JOIN scenarios')

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
        'capability_schema_version': CAPABILITY_SCHEMA_VERSION,
        'machine_interface': MACHINE_INTERFACE,
        'contract_version': CONTRACT_VERSION,
        'component': 'pg_perf_bench',
        'component_version': __version__,
        'artifact_schema_versions': [ARTIFACT_SCHEMA_VERSION],
        'commands': {
            'capabilities': {
                'mutates_target': False,
                'machine_output': True,
                'accepts_plan_hash': False,
            },
            'benchmark': {
                'mutates_target': True,
                'machine_output': True,
                'accepts_plan_hash': True,
            },
            'collect-sys-info': {
                'mutates_target': False,
                'machine_output': True,
                'accepts_plan_hash': False,
            },
            'collect-db-info': {
                'mutates_target': False,
                'machine_output': True,
                'accepts_plan_hash': False,
            },
            'collect-all-info': {
                'mutates_target': False,
                'machine_output': True,
                'accepts_plan_hash': False,
            },
            'join': {'mutates_target': False, 'machine_output': True, 'accepts_plan_hash': False},
            'render': {'mutates_target': False, 'machine_output': True, 'accepts_plan_hash': False},
            'validate': {
                'mutates_target': False,
                'machine_output': True,
                'accepts_plan_hash': False,
            },
            'validate-artifact': {
                'mutates_target': False,
                'machine_output': True,
                'accepts_plan_hash': False,
            },
            'profiles': {
                'mutates_target': False,
                'machine_output': True,
                'accepts_plan_hash': False,
            },
            'join-tasks': {
                'mutates_target': False,
                'machine_output': True,
                'accepts_plan_hash': False,
            },
            'summarize': {
                'mutates_target': False,
                'machine_output': True,
                'accepts_plan_hash': False,
            },
            'plan': {'mutates_target': False, 'machine_output': True, 'accepts_plan_hash': False},
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
        'benchmark_compatibility': {
            'postgresql_server_majors': list(range(10, 19)),
            'load_generator': 'newest_local_pgbench',
            'system_metrics_engine': 'pg_diag',
            'system_metric_families': ['cpu', 'memory', 'disk', 'network'],
            'joined_os_chart_layout': 'vertical_by_report_and_iteration',
        },
    }


def _input_descriptor(path_value: str | None) -> dict[str, Any] | None:
    if not path_value:
        return None
    path = Path(path_value).expanduser().resolve()
    if path.is_file():
        return artifact_descriptor(path, kind='PlanInput', schema_version=None)
    if not path.is_dir():
        raise ConfigurationError(f'plan input does not exist: {path}')
    files = []
    for candidate in sorted(path.rglob('*')):
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(path)
        except ValueError as exc:
            raise ConfigurationError(f'plan input escapes workload root: {candidate}') from exc
        descriptor = artifact_descriptor(resolved, kind='PlanInput', schema_version=None)
        descriptor['path'] = relative.as_posix()
        files.append(descriptor)
    return {'kind': 'PlanInputTree', 'path': str(path), 'files': files}


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
    stable = copy.deepcopy(document)
    stable.pop('report_name', None)
    stable.pop('report_dir', None)
    database = stable.get('database')
    if isinstance(database, dict):
        database.pop('password', None)
    raw_args = stable.get('raw_args')
    if isinstance(raw_args, dict):
        for name in (
            'clear_logs',
            'component_capabilities',
            'log_dir',
            'log_level',
            'machine',
            'output_dir',
            'plan_hash',
            'pg_password',
            'report_name',
            'request_id',
        ):
            raw_args.pop(name, None)
    evidence = {
        'workload_path': _input_descriptor(
            config.workload.workload_path if config.workload is not None else None
        ),
        'postgresql_config': _input_descriptor(
            config.workload.pg_custom_config if config.workload is not None else None
        ),
    }
    return {
        'schema_version': 'pg_perf_bench/plan-v1',
        'plan_hash': canonical_hash({'configuration': stable, 'inputs': evidence}),
        'configuration': document,
        'inputs': evidence,
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
            report_paths=args.reports,
            raise_on_error=True,
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
        'db_logs_dir': config.report_dir / 'db_logs',
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
            'summarize',
            'validate',
            'validate-artifact',
            'plan',
            'capabilities',
            'profiles',
            'join-tasks',
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
                        error={
                            'code': 'validation_error',
                            'message': 'packaged report content is invalid',
                            'details': errors,
                        },
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
        if args.command == 'profiles':
            result = workload_profile_catalog()
            if args.machine:
                _emit_machine(args, 'succeeded', result=result)
            else:
                print(json.dumps(result, indent=2, sort_keys=True))
            return EXIT_CODES['success']
        if args.command == 'join-tasks':
            result = {
                'schema_version': 'pg_perf_bench/join-task-catalog-v1',
                'tasks': join_task_catalog(),
            }
            if args.machine:
                _emit_machine(args, 'succeeded', result=result)
            else:
                print(json.dumps(result, indent=2, sort_keys=True))
            return EXIT_CODES['success']
        if args.command in {'validate-artifact', 'summarize'}:
            artifact = load_artifact(args.artifact)
            descriptor = artifact_descriptor(
                args.artifact,
                kind='BenchmarkReport',
                schema_version=ARTIFACT_SCHEMA_VERSION,
            )
            result = (
                {'valid': True, 'artifact_hash': descriptor['hash']}
                if args.command == 'validate-artifact'
                else summarize_artifact(artifact)
            )
            if args.machine:
                _emit_machine(args, 'succeeded', result=result, artifacts=[descriptor])
            else:
                print(json.dumps(result, indent=2, sort_keys=True, default=str))
            return EXIT_CODES['success']
        if args.command == 'render':
            render_from_json(args.from_json, args.out)
            descriptor = artifact_descriptor(
                args.out,
                kind='BenchmarkReportHtml',
                schema_version=None,
            )
            result = {'html': descriptor['path']}
            if args.machine:
                _emit_machine(args, 'succeeded', result=result, artifacts=[descriptor])
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

        if args.command == str(WorkMode.BENCHMARK) and (args.machine or args.plan_hash):
            if not args.plan_hash:
                raise PreconditionError('machine benchmark requires --plan-hash from plan')
            current_plan = _runtime_plan(build_runtime_config(args))
            if args.plan_hash != current_plan['plan_hash']:
                raise PreconditionError(
                    f'stale benchmark plan: expected {args.plan_hash}, '
                    f'current plan is {current_plan["plan_hash"]}'
                )

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
        artifact_paths = save_report(logger, report, str(config.report_dir))
        artifacts = [
            artifact_descriptor(
                path,
                kind='BenchmarkReport' if kind == 'json' else 'BenchmarkReportHtml',
                schema_version=ARTIFACT_SCHEMA_VERSION if kind == 'json' else None,
            )
            for kind, path in sorted(artifact_paths.items())
        ]
        result = {'report_name': report['report_name'], 'outputs': artifacts}
        if args.machine:
            _emit_machine(
                args,
                'partial' if warnings else 'succeeded',
                result=result,
                artifacts=artifacts,
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
    except PreconditionError as exc:
        message = _safe_error_message(args, exc)
        if getattr(args, 'machine', False):
            _emit_machine(
                args,
                'blocked',
                error={'code': 'precondition_failed', 'message': message},
            )
        else:
            print(f'ERROR: {message}', file=sys.stderr)
        return EXIT_CODES['precondition_failed']
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
        if getattr(args, 'machine', False):
            _emit_machine(
                args,
                'cancelled',
                error={'code': 'cancelled', 'message': 'interrupted'},
            )
            return EXIT_CODES['cancelled']
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
