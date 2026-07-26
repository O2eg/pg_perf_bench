from pg_perf_bench.const import (
    ALL_INFO_TEMPLATE_JSON_PATH,
    DB_INFO_TEMPLATE_JSON_PATH,
    SYS_INFO_TEMPLATE_JSON_PATH,
    ConnectionType,
    WorkMode,
)

from .base_context import BaseContext, transform_key


class CollectInfoContext(BaseContext):
    def __init__(self, args, logger):
        super().__init__(args, logger)

        try:
            self.filter_none(vars(args))
        except Exception as e:
            raise ValueError(f'Incorrect parameters specified:\n{str(e)}') from e

        report_template_path = {
            WorkMode.COLLECT_DB_INFO: DB_INFO_TEMPLATE_JSON_PATH,
            WorkMode.COLLECT_SYS_INFO: SYS_INFO_TEMPLATE_JSON_PATH,
            WorkMode.COLLECT_ALL_INFO: ALL_INFO_TEMPLATE_JSON_PATH,
        }

        if args.pg_bin_path is None and args.mode == WorkMode.COLLECT_SYS_INFO:
            args.pg_bin_path = ''

        # Add connection configuration
        self._add_connection_config(args)

        # Add SSH tunnel if needed for DB or ALL info
        if args.connection_type == ConnectionType.SSH and args.mode in {
            WorkMode.COLLECT_DB_INFO,
            WorkMode.COLLECT_ALL_INFO,
        }:
            self.structured_params['conn_conf']['tunnel_params'] = {
                'remote_host': args.remote_pg_host,
                'remote_port': int(args.remote_pg_port),
                'local_host': args.pg_host,
                'local_port': int(args.pg_port),
            }

        # Add database configuration
        self.structured_params['db_conf'] = {
            'db_conn_params': {
                'host': args.pg_host,
                'port': args.pg_port,
                'user': args.pg_user,
                'password': args.pg_password,
                'database': args.pg_database,
                'timeout': float(getattr(args, 'connect_timeout', 5.0)),
            },
            'db_env': {
                'pg_data_path': args.pg_data_path,
                'pg_bin_path': args.pg_bin_path,
            },
        }

        # Add custom config if present
        if args.pg_custom_config is not None:
            self.structured_params['db_conf']['db_env']['pg_custom_config'] = args.pg_custom_config

        # Add report configuration
        self.structured_params['report_conf'] = {
            'report_name': args.report_name,
            'report_template': report_template_path[args.mode],
            'mode': args.mode,
        }

        # Add log configuration
        self.structured_params['log_conf'] = {
            'collect_pg_logs': args.collect_pg_logs,
            'clear_logs': args.clear_logs,
            'log_level': args.log_level,
        }

    def filter_none(self, d: dict) -> dict:
        SSHConnectionArgs = ['ssh_host', 'ssh_port']
        DockerConnectionArgs = [
            'container_name',
        ]
        DBParamsArgs = [
            'pg_host',
            'pg_port',
            'pg_user',
            'pg_database',
            'pg_data_path',
            'pg_bin_path',
        ]
        mode = d.get('mode')
        if mode in {WorkMode.COLLECT_DB_INFO, WorkMode.COLLECT_ALL_INFO}:
            for key in DBParamsArgs:
                if d.get(key) is None:
                    raise ValueError(
                        f'Parameter "{transform_key(key)}" must be specified for DB connection'
                    )
            SSHConnectionArgs.append('remote_pg_host')
            SSHConnectionArgs.append('remote_pg_port')

        ctype = d.get('connection_type')
        if ctype == ConnectionType.SSH:
            if bool(d.get('ssh_key')) == bool(d.get('ssh_agent')):
                raise ValueError(
                    'Exactly one of "--ssh-key" or "--ssh-agent" must be specified '
                    f'for connection type "{ConnectionType.SSH}"'
                )
            for key in SSHConnectionArgs:
                if d.get(key) is None:
                    raise ValueError(
                        f'Parameter "{transform_key(key)}" must be specified '
                        f'for connection type "{ConnectionType.SSH}"'
                    )
        elif ctype == ConnectionType.DOCKER:
            for key in DockerConnectionArgs:
                if d.get(key) is None:
                    raise ValueError(
                        f'Parameter "{transform_key(key)}" must be specified '
                        f'for connection type "{ConnectionType.DOCKER}"'
                    )
        elif ctype == ConnectionType.LOCAL:
            pass
        else:
            raise ValueError(
                'You must specify the connection type parameter '
                f'"{transform_key("connection_type")}"'
            )

        return d
