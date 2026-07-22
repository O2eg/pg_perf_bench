from pathlib import Path

from pg_perf_bench.benchmark import BenchmarkRunner
from pg_perf_bench.cli import build_parser
from pg_perf_bench.config import build_runtime_config
from pg_perf_bench.join_catalog import join_task_catalog, load_join_task
from pg_perf_bench.report.commands import workload_parse
from pg_perf_bench.workloads import (
    build_workload_evidence,
    bundled_profile_names,
    validate_workload_profiles,
    workload_profile_catalog,
)


def _profile_runtime(profile: str = 'imdb', duration: int = 10):
    args = build_parser().parse_args(
        [
            'benchmark',
            '--connection-type',
            'local',
            '--pg-data-path',
            '/tmp/pg-data',
            '--pg-bin-path',
            '/usr/lib/postgresql/18/bin',
            '--host',
            '127.0.0.1',
            '--port',
            '5432',
            '--database',
            'benchmark_db',
            '--workload-profile',
            profile,
            '--workload-scale',
            '0.25',
            '--workload-duration-seconds',
            str(duration),
            '--pgbench-clients',
            '1,4',
            '--pgbench-path',
            '/usr/bin/pgbench',
            '--psql-path',
            '/usr/bin/psql',
            '--allow-database-reset',
        ]
    )
    return build_runtime_config(args)


def test_bundled_profiles_are_complete_and_do_not_copy_pg_workload_manifest():
    assert bundled_profile_names() == ('imdb', 'pagila')
    assert validate_workload_profiles() == []
    catalog = workload_profile_catalog()
    assert [profile['id'] for profile in catalog['profiles']] == ['imdb', 'pagila']
    for profile_id in bundled_profile_names():
        root = Path(__file__).parents[1] / 'src/pg_perf_bench/workload_profiles' / profile_id
        assert not (root / 'profile.yml').exists()
        assert (root / 'generator.py').is_file()
        assert list((root / 'sql').glob('*.sql'))


def test_profile_supplies_commands_and_report_evidence_embeds_all_sources():
    config = _profile_runtime()
    assert config.workload is not None and config.database is not None and config.host is not None
    workload = config.workload.as_legacy_dict(config.host)
    commands = BenchmarkRunner.load_iterations_config(config.database.as_legacy_dict(), workload)
    evidence = build_workload_evidence(workload, commands)

    assert len(commands) == 2
    assert all('ARG_' not in command for pair in commands for command in pair)
    assert all('--time=10' in pair[1] for pair in commands)
    assert evidence['profile_id'] == 'imdb'
    assert evidence['definition_hash'].startswith('sha256:')
    assert evidence['execution_hash'].startswith('sha256:')
    assert evidence['pgbench']['iteration_values'] == [1, 4]
    assert {source['role'] for source in evidence['files']} >= {
        'schema',
        'generator',
        'setup',
        'query',
        'manifest',
    }
    generator = next(source for source in evidence['files'] if source['role'] == 'generator')
    assert 'subprocess.run' in generator['content']
    assert 'g::bigint * 104729' in generator['content']
    assert len(evidence['pgbench']['resolved_commands']) == 2

    longer = _profile_runtime(duration=20)
    assert longer.workload is not None and longer.database is not None and longer.host is not None
    longer_workload = longer.workload.as_legacy_dict(longer.host)
    longer_commands = BenchmarkRunner.load_iterations_config(
        longer.database.as_legacy_dict(),
        longer_workload,
    )
    assert (
        build_workload_evidence(longer_workload, longer_commands)['execution_hash']
        != evidence['execution_hash']
    )

    init_item = {}
    query_item = {}
    workload_parse({'workload_evidence': evidence}, init_item, phase='init')
    workload_parse({'workload_evidence': evidence}, query_item, phase='workload')
    assert '[generator] generator.py' in init_item['data']
    assert '[schema] sql/schema.sql' in init_item['data']
    assert '[query] sql/05_join_stress.sql' in query_item['data']


def test_join_catalog_has_documented_practical_scenarios():
    catalog = join_task_catalog()
    ids = {task['id'] for task in catalog}
    assert ids >= {
        'optimize-db-config',
        'scale-cpu',
        'scale-memory',
        'compare-storage',
        'tune-os-kernel',
        'compare-postgresql-major',
        'repeatability',
    }
    assert load_join_task('task_compare_dbs_on_single_host.json')['id'] == 'optimize-db-config'
    assert (
        'sections.db.reports.pg_settings.data' not in load_join_task('optimize-db-config')['items']
    )
    assert (
        'database_configuration_evidence.effective_settings_hash'
        in load_join_task('scale-cpu')['items']
    )
