"""Initialization cadence and non-mutating checks for an existing dataset."""

from pg_perf_bench.errors import ConfigurationError
from pg_perf_bench.initialization import qualified_name


def initializes_iteration(policy, index):
    if policy not in ('each-iteration', 'once', 'skip'):
        raise ConfigurationError('Unsupported initialization policy')
    return policy == 'each-iteration' or (policy == 'once' and index == 1)


def policy_evidence(config):
    policy = config.get('init_policy', 'each-iteration')
    return {
        'init_policy': policy,
        'dataset_reused_between_iterations': policy != 'each-iteration',
        'existing_dataset': policy == 'skip',
        'workload_scale_verified': False if policy == 'skip' else None,
        'vacuum_analyze_before_each_workload': policy == 'each-iteration',
    }


async def validate_existing_dataset(db, plan, *, builtin=False):
    """Check schema access, readable relations and invalid indexes, without scanning data.

    This is a structural preflight, not proof of generator identity or scale.
    Custom workloads without a load plan only get the connection/session checks.
    """
    if plan is None:
        if builtin:
            for table, columns in {
                'pgbench_accounts': 'aid, bid, abalance, filler',
                'pgbench_branches': 'bid, bbalance, filler',
                'pgbench_tellers': 'tid, bid, tbalance, filler',
                'pgbench_history': 'tid, bid, aid, delta, mtime, filler',
            }.items():
                await db.fetch(f'SELECT {columns} FROM {qualified_name("public", table)} LIMIT 0')
        return {
            'scope': 'builtin tables/columns' if builtin else 'connection only',
            'generator_and_scale_verified': False,
        }
    relations = []
    for schema in plan.schemas:
        if not await db.fetchval(
            'SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname=$1 '
            "AND has_schema_privilege(oid, 'USAGE'))",
            schema,
        ):
            raise ConfigurationError(
                f'Existing dataset schema is missing or inaccessible: {schema}'
            )
        tables = await db.fetch(
            'SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace '
            "WHERE n.nspname=$1 AND c.relkind IN ('r','p','v','m','f')",
            schema,
        )
        if not tables:
            raise ConfigurationError(f'Existing dataset schema has no relations: {schema}')
        for row in tables:
            name = qualified_name(schema, row['relname'])
            await db.fetch(f'SELECT * FROM {name} LIMIT 0')
            relations.append(name)
        invalid = await db.fetch(
            'SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid '
            'JOIN pg_namespace n ON n.oid=c.relnamespace '
            'WHERE n.nspname=$1 AND (NOT i.indisvalid OR NOT i.indisready)',
            schema,
        )
        if invalid:
            raise ConfigurationError(f'Existing dataset has invalid indexes in {schema}')
    return {
        'scope': 'schema access, readable relations, index validity',
        'relations': relations,
        'generator_and_scale_verified': False,
    }
