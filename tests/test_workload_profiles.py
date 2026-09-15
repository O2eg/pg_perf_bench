import json
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
    assert bundled_profile_names() == ('imdb', 'pagila', 'pagila-htap')
    assert validate_workload_profiles() == []
    catalog = workload_profile_catalog()
    assert [profile['id'] for profile in catalog['profiles']] == ['imdb', 'pagila', 'pagila-htap']
    for profile_id in bundled_profile_names():
        root = Path(__file__).parents[1] / 'src/pg_perf_bench/workload_profiles' / profile_id
        assert not (root / 'profile.yml').exists()
        assert (root / 'generator.py').is_file()
        assert list((root / 'sql').glob('*.sql'))


def test_bundled_profiles_stay_portable_across_postgresql_10_18():
    root = Path(__file__).parents[1] / 'src/pg_perf_bench/workload_profiles'
    for profile_id in bundled_profile_names():
        generator = (root / profile_id / 'generator.py').read_text(encoding='utf-8')
        # random()/setseed() sequences differ between PostgreSQL 10-11, 12-14 and 15+;
        # hashint8() is identical, so one scale must build one dataset on every major.
        assert 'random()' not in generator
        assert 'setseed' not in generator
        assert 'hashint8(' in generator
        assert '.det_uniform(bigint, integer);' in generator  # helper is dropped after use
    schema = (root / 'pagila/sql/pagila-schema.sql').read_text(encoding='utf-8')
    assert 'default_table_access_method' not in schema  # PostgreSQL 12+ only
    assert 'EXECUTE FUNCTION' not in schema  # PostgreSQL 11+ only
    assert "server_version_num')::integer >= 110000" in schema  # parent PK needs 11+


def test_pagila_customer_balance_uses_unbounded_numeric():
    schema = (
        Path(__file__).parents[1]
        / 'src/pg_perf_bench/workload_profiles/pagila/sql/pagila-schema.sql'
    ).read_text(encoding='utf-8')
    start = schema.index('FUNCTION pagila.get_customer_balance')
    body = schema[start : schema.index('$$;', start)]
    # DECIMAL(5,2) locals overflow for the heaviest generated customers and abort pgbench.
    assert 'DECIMAL(5,2)' not in body
    assert 'v_rentfees numeric;' in body and 'v_payments numeric;' in body


def test_pagila_store_creation_is_concurrency_safe():
    insert_sql = (
        Path(__file__).parents[1] / 'src/pg_perf_bench/workload_profiles/pagila/sql/02_insert.sql'
    ).read_text(encoding='utf-8')

    assert 'FOR UPDATE SKIP LOCKED' in insert_sql
    assert 'ON CONFLICT (manager_staff_id) DO NOTHING' in insert_sql


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
    assert 'build_load_plan' in generator['content']
    assert 'run_tasks' in generator['content']
    assert config.workload.init_mode == 'fast'
    assert 'det_uniform(g, 11)' in generator['content']
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
    assert '[schema] sql/imdb-schema.sql' in init_item['data']
    assert '[setup] sql/imdb-fkindexes.sql' in init_item['data']
    assert '[query] sql/05_join_stress.sql' in query_item['data']
    assert '[query] sql/select_33.sql' in query_item['data']
    assert len(evidence['files']) == 38 + 8  # queries, legacy assets, common-loader assets
    assert all('--random-seed=42' in pair[1] for pair in commands)


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
        'compare-deployments',
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


def test_pagila_profiles_share_sources_and_declare_the_oltp_mix():
    root = Path(__file__).parents[1] / 'src/pg_perf_bench/workload_profiles'
    shared = [
        'generator.py',
        'sql/pagila-schema.sql',
        'sql/pagila-benchmark-setup.sql',
        'sql/01_select.sql',
        'sql/02_insert.sql',
        'sql/03_update.sql',
        'sql/04_delete.sql',
    ]
    for relative in shared:
        assert (root / 'pagila' / relative).read_bytes() == (
            root / 'pagila-htap' / relative
        ).read_bytes(), relative
    oltp = json.loads((root / 'pagila/profile.json').read_text(encoding='utf-8'))
    htap = json.loads((root / 'pagila-htap/profile.json').read_text(encoding='utf-8'))
    for profile in (oltp, htap):
        command = profile['benchmark']['workload_command']
        assert '-M prepared' in command and '--random-seed=42' in command
        assert 'sql/01_select.sql@50' in command and 'sql/04_delete.sql@5' in command
        assert profile['files']['setup'] == ['sql/pagila-benchmark-setup.sql']
    assert 'sql/05_reporting.sql' not in oltp['benchmark']['workload_command']
    assert 'sql/05_reporting.sql@5' in htap['benchmark']['workload_command']
    for relative in shared[3:]:
        script = (root / 'pagila' / relative).read_text(encoding='utf-8')
        # identifiers come from pgbench within bounds read from a one-row table, never
        # from ORDER BY random() scans whose cost grows with the table
        assert 'ORDER BY random()' not in script
        assert 'SELECT * FROM bench_bounds \\gset' in script
        assert 'random(1, :max_' in script
    setup = (root / 'pagila/sql/pagila-benchmark-setup.sql').read_text(encoding='utf-8')
    assert 'VACUUM (FREEZE, ANALYZE) pagila.rental;' in setup
    assert 'CREATE TABLE pagila.bench_bounds AS' in setup
    assert "'ALTER ROLE %I IN DATABASE %I SET search_path = pagila, public'" in setup
    assert "'ALTER DATABASE %I SET search_path = pagila, public'" in setup
