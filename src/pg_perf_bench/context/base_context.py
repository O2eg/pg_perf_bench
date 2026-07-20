from pathlib import Path

from pg_perf_bench.const import ConnectionType


def transform_key(key: str) -> str:
    return '--' + key.replace('_', '-')


class BaseContext:
    """Base class for all context classes with common functionality"""

    def __init__(self, args, logger):
        self.structured_params = {'args': vars(args), 'logger': logger}

    def filter_none(self, d: dict) -> dict:
        """Base method to be overridden by child classes"""
        return d

    def _add_connection_config(self, args):
        """Add connection configuration based on connection type"""
        conn_type = args.connection_type
        self.structured_params['conn_type'] = conn_type

        if conn_type == ConnectionType.SSH:
            self._add_ssh_connection_config(args)
        elif conn_type == ConnectionType.DOCKER:
            self._add_docker_connection_config(args)
        elif conn_type == ConnectionType.LOCAL:
            self._add_local_connection_config(args)
        else:
            raise ValueError(
                'You must specify the connection type parameter '
                f'"{transform_key("connection_type")}"'
            )

    def _add_ssh_connection_config(self, args):
        """Add SSH connection configuration"""
        conn_params = {
            'host': args.ssh_host,
            'port': args.ssh_port,
            'username': getattr(args, 'ssh_user', None) or 'postgres',
            'client_keys': args.ssh_key,
            'known_hosts': (
                None
                if getattr(args, 'ssh_insecure_no_host_key_check', False)
                else getattr(args, 'ssh_known_hosts', None)
                or str(Path('~/.ssh/known_hosts').expanduser())
            ),
            'connect_timeout': float(getattr(args, 'connect_timeout', 5.0)),
        }

        self.structured_params['conn_conf'] = {
            'conn_params': conn_params,
            'env': {'ARG_PG_BIN_PATH': f'{args.pg_bin_path or ""}'},
            'command_timeout': float(getattr(args, 'command_timeout', 300.0)),
        }

    def _add_docker_connection_config(self, args):
        """Add Docker connection configuration"""
        self.structured_params['conn_conf'] = {
            'conn_params': {'container_name': args.container_name},
            'env': {'ARG_PG_BIN_PATH': args.pg_bin_path},
            'command_timeout': float(getattr(args, 'command_timeout', 300.0)),
        }

    def _add_local_connection_config(self, args):
        """Add Local connection configuration"""
        self.structured_params['conn_conf'] = {
            'env': {'ARG_PG_BIN_PATH': args.pg_bin_path or ''},
            'command_timeout': float(getattr(args, 'command_timeout', 300.0)),
        }
