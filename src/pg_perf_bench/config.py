"""Typed runtime configuration and CLI validation."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pg_perf_bench.client_tools import select_local_clients
from pg_perf_bench.const import (
    WORKLOAD_PROFILES_PATH,
    ConnectionType,
    WorkloadTypes,
    WorkMode,
)
from pg_perf_bench.errors import ConfigurationError
from pg_perf_bench.workloads import load_workload_profile

DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_COMMAND_TIMEOUT_SECONDS = 300.0
PROTECTED_DATABASES = frozenset({'postgres', 'template0', 'template1'})


def _positive_int(value: Any, option: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f'{option} must be an integer') from exc
    if parsed <= 0:
        raise ConfigurationError(f'{option} must be greater than zero')
    return parsed


def _positive_float(value: Any, option: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f'{option} must be a number') from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ConfigurationError(f'{option} must be a positive finite number')
    return parsed


def _required(value: Any, option: str) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ConfigurationError(f'{option} is required')
    return value


def _report_name(value: Any) -> str | None:
    if value is None:
        return None
    name = str(value).strip()
    if not name:
        raise ConfigurationError('--report-name must not be empty')
    if name in {'.', '..'} or Path(name).name != name or '\x00' in name:
        raise ConfigurationError(f'unsafe --report-name: {name!r}')
    return name


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    user: str
    database: str
    password: str | None = field(default=None, repr=False)
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS

    def as_asyncpg_kwargs(self, *, database: str | None = None) -> dict[str, Any]:
        return {
            'host': self.host,
            'port': self.port,
            'user': self.user,
            'database': database or self.database,
            'password': self.password,
            'timeout': self.connect_timeout,
        }

    def as_legacy_dict(self) -> dict[str, Any]:
        return {
            'host': self.host,
            'port': self.port,
            'user': self.user,
            'password': self.password,
            'database': self.database,
            'connect_timeout': self.connect_timeout,
        }


@dataclass(frozen=True)
class HostConfig:
    connection_type: ConnectionType
    pg_data_path: str | None
    pg_bin_path: str | None
    command_timeout: float
    container_name: str | None = None
    ssh_host: str | None = None
    ssh_port: int = 22
    ssh_user: str = 'postgres'
    ssh_key: Path | None = None
    ssh_known_hosts: Path | None = None
    ssh_insecure_no_host_key_check: bool = False
    remote_pg_host: str | None = None
    remote_pg_port: int | None = None

    def connection_kwargs(
        self,
        database: DatabaseConfig | None,
        *,
        start_if_stopped: bool = False,
    ) -> dict[str, Any]:
        env = {'ARG_PG_BIN_PATH': self.pg_bin_path or ''}
        if self.connection_type == ConnectionType.LOCAL:
            return {'env': env, 'command_timeout': self.command_timeout}
        if self.connection_type == ConnectionType.DOCKER:
            return {
                'conn_params': {'container_name': self.container_name},
                'env': env,
                'command_timeout': self.command_timeout,
                'start_if_stopped': start_if_stopped,
            }
        known_hosts: str | None = (
            None
            if self.ssh_insecure_no_host_key_check
            else str(self.ssh_known_hosts or Path('~/.ssh/known_hosts').expanduser())
        )
        result: dict[str, Any] = {
            'conn_params': {
                'host': self.ssh_host,
                'port': self.ssh_port,
                'username': self.ssh_user,
                'client_keys': str(self.ssh_key) if self.ssh_key else None,
                'known_hosts': known_hosts,
                'connect_timeout': min(self.command_timeout, 30.0),
            },
            'env': env,
            'command_timeout': self.command_timeout,
        }
        if database is not None and self.remote_pg_host and self.remote_pg_port:
            result['tunnel_params'] = {
                'local_host': database.host,
                'local_port': database.port,
                'remote_host': self.remote_pg_host,
                'remote_port': self.remote_pg_port,
            }
        return result


@dataclass(frozen=True)
class WorkloadConfig:
    benchmark_type: WorkloadTypes
    init_command: str
    workload_command: str
    pgbench_path: str
    psql_path: str
    iteration_name: str
    iterations: tuple[int, ...]
    workload_path: str | None = None
    workload_profile: str | None = None
    workload_scale: float = 1.0
    workload_duration_seconds: int | None = None
    pg_custom_config: str | None = None
    allow_database_reset: bool = False
    drop_os_caches: bool = False
    system_metrics_interval: float = 1.0
    system_metrics_duration: float | None = None

    def as_legacy_dict(self, host: HostConfig) -> dict[str, Any]:
        return {
            'pg_data_path': host.pg_data_path,
            'pg_bin_path': host.pg_bin_path,
            'pgbench_path': self.pgbench_path,
            'psql_path': self.psql_path,
            'benchmark_type': self.benchmark_type,
            'workload_path': self.workload_path,
            'workload_profile': self.workload_profile,
            'workload_scale': self.workload_scale,
            'workload_duration_seconds': self.workload_duration_seconds,
            'init_command': self.init_command,
            'workload_command': self.workload_command,
            'pgbench_iter_name': self.iteration_name,
            'pgbench_iter_list': list(self.iterations),
            'pg_custom_config': self.pg_custom_config,
            'allow_database_reset': self.allow_database_reset,
            'drop_os_caches': self.drop_os_caches,
            'system_metrics_interval': self.system_metrics_interval,
            'system_metrics_duration': self.system_metrics_duration,
        }


@dataclass(frozen=True)
class RuntimeConfig:
    mode: WorkMode
    host: HostConfig | None
    database: DatabaseConfig | None
    workload: WorkloadConfig | None
    report_name: str | None
    report_dir: Path
    collect_pg_logs: bool
    raw_args: dict[str, Any]


def build_runtime_config(args: Any) -> RuntimeConfig:
    values = vars(args).copy()
    report_name = _report_name(values.get('report_name'))
    try:
        mode = WorkMode(values.get('mode') or values.get('command'))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError('a command is required') from exc

    if mode == WorkMode.JOIN:
        return RuntimeConfig(
            mode=mode,
            host=None,
            database=None,
            workload=None,
            report_name=report_name,
            report_dir=Path(values.get('output_dir') or 'report').expanduser(),
            collect_pg_logs=False,
            raw_args=values,
        )

    try:
        connection_type = ConnectionType(
            _required(values.get('connection_type'), '--connection-type')
        )
    except ValueError as exc:
        raise ConfigurationError(
            f'unsupported connection type: {values.get("connection_type")}'
        ) from exc

    needs_database = mode != WorkMode.COLLECT_SYS_INFO
    database: DatabaseConfig | None = None
    if needs_database:
        database = DatabaseConfig(
            host=str(_required(values.get('pg_host'), '--host')),
            port=_positive_int(_required(values.get('pg_port'), '--port'), '--port'),
            user=str(_required(values.get('pg_user'), '--user')),
            database=str(_required(values.get('pg_database'), '--database')),
            password=values.get('pg_password'),
            connect_timeout=_positive_float(
                values.get('connect_timeout', DEFAULT_CONNECT_TIMEOUT_SECONDS),
                '--connect-timeout',
            ),
        )

    pg_data_path = values.get('pg_data_path')
    pg_bin_path = values.get('pg_bin_path')
    if mode == WorkMode.BENCHMARK:
        _required(pg_data_path, '--pg-data-path')
    if needs_database:
        _required(pg_bin_path, '--pg-bin-path')

    ssh_key_value = values.get('ssh_key')
    host = HostConfig(
        connection_type=connection_type,
        pg_data_path=pg_data_path,
        pg_bin_path=pg_bin_path,
        command_timeout=_positive_float(
            values.get('command_timeout', DEFAULT_COMMAND_TIMEOUT_SECONDS),
            '--command-timeout',
        ),
        container_name=values.get('container_name'),
        ssh_host=values.get('ssh_host'),
        ssh_port=_positive_int(values.get('ssh_port') or 22, '--ssh-port'),
        ssh_user=values.get('ssh_user') or 'postgres',
        ssh_key=Path(ssh_key_value).expanduser() if ssh_key_value else None,
        ssh_known_hosts=(
            Path(values['ssh_known_hosts']).expanduser() if values.get('ssh_known_hosts') else None
        ),
        ssh_insecure_no_host_key_check=bool(values.get('ssh_insecure_no_host_key_check')),
        remote_pg_host=values.get('remote_pg_host'),
        remote_pg_port=(
            _positive_int(values['remote_pg_port'], '--remote-pg-port')
            if values.get('remote_pg_port') is not None
            else None
        ),
    )
    _validate_host(host, needs_database=needs_database)

    workload: WorkloadConfig | None = None
    if mode == WorkMode.BENCHMARK:
        if not values.get('allow_database_reset'):
            raise ConfigurationError(
                'benchmark recreates --database; pass --allow-database-reset '
                'after selecting a dedicated disposable database'
            )
        assert database is not None
        if database.database.lower() in PROTECTED_DATABASES:
            raise ConfigurationError(f'refusing to reset protected database {database.database!r}')
        clients = values.get('pgbench_clients')
        durations = values.get('pgbench_time')
        if bool(clients) == bool(durations):
            raise ConfigurationError(
                'exactly one of --pgbench-clients or --pgbench-time is required'
            )
        raw_iterations = clients or durations
        iterations = tuple(_positive_int(item, 'pgbench iteration') for item in raw_iterations)
        profile_id = values.get('workload_profile')
        profile = load_workload_profile(str(profile_id)) if profile_id else None
        if profile is not None and durations:
            raise ConfigurationError(
                'bundled workload profiles measure maximum TPS with --pgbench-clients'
            )
        if profile is not None and values.get('workload_path'):
            raise ConfigurationError('--workload-path cannot be combined with --workload-profile')
        workload_duration_seconds = (
            _positive_int(values['workload_duration_seconds'], '--workload-duration-seconds')
            if values.get('workload_duration_seconds') is not None
            else int(profile['benchmark']['default_duration_seconds'])
            if profile is not None
            else None
        )
        selected_type = values.get('benchmark_type') or (
            str(WorkloadTypes.CUSTOM) if profile is not None else None
        )
        try:
            benchmark_type = WorkloadTypes(
                _required(selected_type, '--benchmark-type or --workload-profile')
            )
        except ValueError as exc:
            raise ConfigurationError('unsupported benchmark type') from exc
        if profile is not None and benchmark_type != WorkloadTypes.CUSTOM:
            raise ConfigurationError('bundled workload profiles use benchmark type custom')
        workload_path = (
            str((WORKLOAD_PROFILES_PATH / str(profile_id)).resolve())
            if profile is not None
            else values.get('workload_path')
        )
        if benchmark_type == WorkloadTypes.CUSTOM:
            _required(workload_path, '--workload-path')
            if not Path(str(workload_path)).expanduser().exists():
                raise ConfigurationError(f'--workload-path does not exist: {workload_path}')
        custom_config = values.get('pg_custom_config')
        if custom_config and not Path(str(custom_config)).expanduser().is_file():
            raise ConfigurationError(f'--pg-custom-config does not exist: {custom_config}')
        pgbench, psql = select_local_clients(
            values.get('pgbench_path'),
            values.get('psql_path'),
        )
        workload = WorkloadConfig(
            benchmark_type=benchmark_type,
            init_command=str(
                _required(
                    values.get('init_command')
                    or (profile['benchmark']['init_command'] if profile is not None else None),
                    '--init-command',
                )
            ),
            workload_command=str(
                _required(
                    values.get('workload_command')
                    or (profile['benchmark']['workload_command'] if profile is not None else None),
                    '--workload-command',
                )
            ),
            pgbench_path=str(pgbench.path),
            psql_path=str(psql.path),
            iteration_name='pgbench_clients' if clients else 'pgbench_time',
            iterations=iterations,
            workload_path=workload_path,
            workload_profile=str(profile_id) if profile_id else None,
            workload_scale=_positive_float(values.get('workload_scale', 1.0), '--workload-scale'),
            workload_duration_seconds=workload_duration_seconds,
            pg_custom_config=custom_config,
            allow_database_reset=True,
            drop_os_caches=bool(values.get('drop_os_caches')),
            system_metrics_interval=_positive_float(
                values.get('system_metrics_interval', 1.0),
                '--system-metrics-interval',
            ),
            system_metrics_duration=(
                _positive_float(
                    values['system_metrics_duration'],
                    '--system-metrics-duration',
                )
                if values.get('system_metrics_duration') is not None
                else None
            ),
        )

    return RuntimeConfig(
        mode=mode,
        host=host,
        database=database,
        workload=workload,
        report_name=report_name,
        report_dir=Path(values.get('output_dir') or 'report').expanduser(),
        collect_pg_logs=bool(values.get('collect_pg_logs')),
        raw_args=values,
    )


def _validate_host(host: HostConfig, *, needs_database: bool) -> None:
    if host.connection_type == ConnectionType.DOCKER:
        _required(host.container_name, '--container-name')
        return
    if host.connection_type != ConnectionType.SSH:
        return
    _required(host.ssh_host, '--ssh-host')
    _required(host.ssh_user, '--ssh-user')
    _required(host.ssh_key, '--ssh-key')
    assert host.ssh_key is not None
    if not host.ssh_key.is_file():
        raise ConfigurationError(f'SSH private key does not exist: {host.ssh_key}')
    if not host.ssh_insecure_no_host_key_check:
        known_hosts = host.ssh_known_hosts or Path('~/.ssh/known_hosts').expanduser()
        if not known_hosts.is_file():
            raise ConfigurationError(
                f'SSH known_hosts does not exist: {known_hosts}; '
                'provide --ssh-known-hosts or explicitly use '
                '--ssh-insecure-no-host-key-check'
            )
    if needs_database:
        _required(host.remote_pg_host, '--remote-pg-host')
        _required(host.remote_pg_port, '--remote-pg-port')
