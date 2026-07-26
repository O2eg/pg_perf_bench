import argparse
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh

from pg_perf_bench.connections import SSHConnection
from pg_perf_bench.const import ConnectionType, WorkloadTypes, WorkMode
from pg_perf_bench.context import Context


class TestSSHConnectionFunctions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        args = argparse.Namespace(
            mode=WorkMode.BENCHMARK,
            connection_type=ConnectionType.SSH,
            ssh_host='127.0.0.1',
            ssh_port=22,
            ssh_user='postgres',
            ssh_key='/home/test/.ssh/id_rsa',
            ssh_known_hosts='/home/test/.ssh/known_hosts',
            ssh_insecure_no_host_key_check=False,
            remote_pg_host='192.168.1.100',
            remote_pg_port=5432,
            pg_host='127.0.0.1',
            pg_port=5433,
            pg_user='postgres',
            pg_password='secret',
            pg_database='test_db',
            pg_data_path='/var/lib/postgresql/18/main',
            pg_bin_path='/usr/lib/postgresql/18/bin',
            pgbench_path='/usr/bin/pgbench',
            psql_path='/usr/bin/psql',
            benchmark_type=WorkloadTypes.DEFAULT,
            workload_path=None,
            init_command='init_command_example',
            workload_command='workload_command_example',
            pgbench_clients=[10, 20, 30],
            pgbench_time=None,
            pg_custom_config=None,
            report_name='TestReport',
            collect_pg_logs=True,
            clear_logs=False,
            log_level='info',
            connect_timeout=5,
            command_timeout=30,
        )
        context = Context(args, MagicMock())
        self.connection = SSHConnection(**context.structured_params['conn_conf'])

    async def test_context_uses_asyncssh_parameters_and_native_tunnel(self):
        self.assertEqual(self.connection.conn_params['username'], 'postgres')
        self.assertEqual(
            self.connection.conn_params['known_hosts'],
            '/home/test/.ssh/known_hosts',
        )
        self.assertEqual(
            self.connection.tunnel_params,
            {
                'remote_host': '192.168.1.100',
                'remote_port': 5432,
                'local_host': '127.0.0.1',
                'local_port': 5433,
            },
        )

        client = MagicMock()
        client.forward_local_port = AsyncMock(return_value=MagicMock())
        with patch(
            'pg_perf_bench.connections.ssh.asyncssh.connect',
            new=AsyncMock(return_value=client),
        ):
            await self.connection.start()

        client.forward_local_port.assert_awaited_once_with('127.0.0.1', 5433, '192.168.1.100', 5432)

    async def test_connection_error_is_normalized(self):
        with patch(
            'pg_perf_bench.connections.ssh.asyncssh.connect',
            side_effect=OSError('unreachable'),
        ):
            with self.assertRaisesRegex(ConnectionError, 'SSH connection failed'):
                await self.connection.start()

    async def test_nonzero_exit_status_is_failure_even_without_stderr(self):
        result = MagicMock(stdout='partial output', stderr='', exit_status=7)
        client = MagicMock()
        client.run = AsyncMock(return_value=result)
        self.connection.client = client

        with self.assertRaisesRegex(RuntimeError, 'exit code 7'):
            await self.connection.run_command('false', check=True)

    async def test_permission_error_has_actionable_message(self):
        with patch(
            'pg_perf_bench.connections.ssh.asyncssh.connect',
            side_effect=asyncssh.PermissionDenied('denied'),
        ):
            with self.assertRaisesRegex(PermissionError, 'selected key or agent identity'):
                await self.connection.start()


if __name__ == '__main__':
    unittest.main()
