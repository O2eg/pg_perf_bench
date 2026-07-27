import unittest

from pg_perf_bench.benchmark import BenchmarkRunner


class TestBenchmarkFunctions(unittest.TestCase):
    def test_get_pgbench_results(self):
        out = """
        number of clients: 10
        duration: 60
        number of transactions actually processed: 150/150
        latency average = 13.2 ms
        initial connection time = 2.5 ms
        tps = 100.5
        """
        results = BenchmarkRunner.get_pgbench_results(out)
        self.assertEqual(results, [10, 60, 150, 13.2, 2.5, 100.5])

    def test_get_filled_load_commands(self):
        db_conf = {'host': '127.0.0.1'}
        workload_conf = {
            'init_command': 'init ARG_HOST',
            'workload_command': 'work ARG_PGBENCH_ITER',
        }
        result = BenchmarkRunner.get_filled_load_commands(
            db_conf, workload_conf, 'pgbench_iter', 50
        )
        self.assertEqual(len(result), 2)
        self.assertIn('127.0.0.1', result[0])
        self.assertIn('50', result[1])

    def test_load_iterations_config_empty(self):
        res = BenchmarkRunner.load_iterations_config({}, {})
        self.assertEqual(res, [])

    def test_maximum_tps_keeps_winning_iteration_and_metrics(self):
        result = BenchmarkRunner.maximum_tps(
            [
                {'iteration': {'value': 1}, 'metrics': {'tps': 10.0, 'latency': 2.0}},
                {'iteration': {'value': 8}, 'metrics': {'tps': 42.0, 'latency': 4.0}},
            ]
        )

        self.assertEqual(result['tps'], 42.0)
        self.assertEqual(result['iteration']['value'], 8)
        self.assertEqual(result['metrics']['latency'], 4.0)

    def test_invocation_summary_contains_safe_report_parameters(self):
        summary = BenchmarkRunner.build_invocation_summary(
            'local_docker',
            {
                'host': '127.0.0.1',
                'port': 55432,
                'user': 'postgres',
                'password': 'must-not-leak',
                'database': 'workload_demo',
            },
            {
                'workload_profile': 'pagila',
                'workload_scale': 0.05,
                'workload_duration_seconds': 30,
                'pgbench_iter_name': 'pgbench_clients',
                'pgbench_iter_list': [1, 2, 4],
                'system_metrics_interval': 1.0,
                'system_metrics_duration': None,
                'allow_database_reset': True,
                'drop_os_caches': False,
            },
        )

        self.assertEqual(
            summary['database'],
            {
                'host': '127.0.0.1',
                'port': 55432,
                'name': 'workload_demo',
                'user': 'postgres',
            },
        )
        self.assertEqual(summary['workload']['iteration_values'], [1, 2, 4])
        self.assertEqual(summary['metrics']['engine'], 'pg_diag')
        self.assertNotIn('password', str(summary).lower())
