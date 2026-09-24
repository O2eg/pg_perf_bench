"""Opt-in checks against an already initialized disposable Pagila database.

Set PG_PERF_BENCH_PROFILE_VALIDATION=1 and standard PGHOST/PGPORT/PGUSER/
PGPASSWORD/PGDATABASE variables. The database name must start with
perf_check_profile_validation_; the SQL smoke test commits workload mutations.
"""

import asyncio
import json
import os
import re
import subprocess
from collections import defaultdict
from decimal import Decimal

import asyncpg
import pytest

from pg_perf_bench.const import WORKLOAD_PROFILES_PATH

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get('PG_PERF_BENCH_PROFILE_VALIDATION') != '1',
        reason='requires explicitly initialized disposable Pagila database',
    ),
]


async def connect():
    database = os.environ['PGDATABASE']
    assert database.startswith('perf_check_profile_validation_')
    conn = await asyncpg.connect(
        host=os.environ['PGHOST'],
        port=int(os.environ.get('PGPORT', '5432')),
        user=os.environ['PGUSER'],
        password=os.environ['PGPASSWORD'],
        database=database,
        ssl=False,
        command_timeout=60,
    )
    await conn.execute("SET search_path=pagila,public; SET statement_timeout='60s'")
    return conn


async def reporting(conn):
    bounds = dict(await conn.fetchrow('SELECT * FROM bench_bounds'))
    day = min(120, bounds['data_days'])
    values = {
        'store_id': 1,
        'category_id': 1,
        'window_start': bounds['data_start_epoch'] + (day - bounds['report_window_days']) * 86400,
        'window_end': bounds['data_start_epoch'] + day * 86400,
    }
    source = (WORKLOAD_PROFILES_PATH / 'pagila-htap/sql/05_reporting.sql').read_text()
    source = '\n'.join(
        line
        for line in source.splitlines()
        if not line.lstrip().startswith(('--', '\\')) and '\\gset' not in line
    )
    queries = [
        q.strip() for q in source.split(';') if q.strip() not in ('', 'BEGIN READ ONLY', 'COMMIT')
    ]
    assert len(queries) == 7
    return [re.sub(r'(?<!:):([a-z_]+)', lambda m: str(values[m[1]]), q) for q in queries], values


def test_loaded_data_and_availability():
    async def run():
        conn = await connect()
        try:
            assert await conn.fetchval('SELECT count(*) FROM country') == 109
            assert not await conn.fetchval("""SELECT EXISTS (
                SELECT inventory_id FROM rental WHERE return_date IS NULL
                GROUP BY inventory_id HAVING count(*) > 1)""")
            assert not await conn.fetchval("""SELECT EXISTS (
                SELECT 1 FROM payment p JOIN rental r USING(rental_id)
                WHERE p.customer_id<>r.customer_id OR p.staff_id<>r.staff_id)""")
            assert not await conn.fetchval("""SELECT EXISTS (
                SELECT 1 FROM payment p JOIN rental r USING(rental_id)
                WHERE p.payment_date < r.rental_date)""")
            # SQL helpers must agree with the underlying state, including missing IDs.
            for inventory in (1, 2, 3, 10, 100, -1):
                expected = await conn.fetchval(
                    """SELECT EXISTS (
                    SELECT 1 FROM inventory WHERE inventory_id=$1) AND NOT EXISTS (
                    SELECT 1 FROM rental WHERE inventory_id=$1 AND return_date IS NULL)""",
                    inventory,
                )
                assert await conn.fetchval('SELECT inventory_in_stock($1)', inventory) == expected
            b = await conn.fetchrow('SELECT * FROM bench_bounds')
            assert (
                await conn.fetchval(
                    """SELECT count(DISTINCT s.store_id)
                FROM rental r JOIN staff s USING(staff_id)
                WHERE s.store_id <= $1""",
                    b['max_store'],
                )
                == b['max_store']
            )
            assert 1 <= b['report_window_days'] <= b['data_days'] <= 201
        finally:
            await conn.close()

    asyncio.run(run())


def test_reporting_counts_money_and_utc_days():
    async def run():
        conn = await connect()
        try:
            queries, values = await reporting(conn)
            await conn.execute('BEGIN READ ONLY')
            await conn.execute("SET LOCAL TimeZone='Europe/Moscow'")
            rows = [await conn.fetch(q) for q in queries]
            await conn.execute("SET LOCAL TimeZone='UTC'")
            assert rows[5] == await conn.fetch(queries[5])
            assert rows[3] == await conn.fetch(queries[3])
            payments = await conn.fetch(
                """SELECT p.customer_id, p.rental_id, p.amount,
                (p.payment_date AT TIME ZONE 'UTC')::date AS day
                FROM payment p JOIN staff s ON s.staff_id=p.staff_id
                WHERE s.store_id=$1 AND p.payment_date>=to_timestamp($2)
                                    AND p.payment_date<to_timestamp($3)""",
                values['store_id'],
                values['window_start'],
                values['window_end'],
            )
            days = defaultdict(lambda: {'count': 0, 'rentals': set(), 'amount': Decimal(0)})
            customers = defaultdict(lambda: {'rentals': set(), 'amount': Decimal(0)})
            for payment in payments:
                d = days[payment['day']]
                d['count'] += 1
                d['rentals'].add(payment['rental_id'])
                d['amount'] += payment['amount']
                c = customers[payment['customer_id']]
                c['rentals'].add(payment['rental_id'])
                c['amount'] += payment['amount']
            expected = [
                (day, d['count'], len(d['rentals']), d['amount']) for day, d in sorted(days.items())
            ]
            assert [tuple(row) for row in rows[5]] == expected
            assert sum(row['revenue'] for row in rows[0]) == sum(d['amount'] for d in days.values())
            assert sum(row['revenue'] for row in rows[1]) == sum(d['amount'] for d in days.values())
            candidates = sorted(
                (
                    (-c['amount'], key, len(c['rentals']))
                    for key, c in customers.items()
                    if len(c['rentals']) >= 3 and c['amount'] >= 20
                )
            )[:20]
            assert [(r['customer_id'], r['rentals'], r['spent']) for r in rows[6]] == [
                (key, count, -amount) for amount, key, count in candidates
            ]
        finally:
            await conn.execute('ROLLBACK')
            await conn.close()

    asyncio.run(run())


def test_customer_balance_respects_effective_date():
    async def run():
        conn = await connect()
        try:
            await conn.execute('BEGIN')
            customer = await conn.fetchval("""INSERT INTO customer
                (store_id,first_name,last_name,address_id)
                VALUES (1,'Audit','Balance',1) RETURNING customer_id""")
            await conn.execute("""UPDATE film SET rental_rate=5, rental_duration=3
                WHERE film_id=(SELECT film_id FROM inventory WHERE inventory_id=1)""")
            rental = await conn.fetchval(
                """INSERT INTO rental
                (customer_id,inventory_id,staff_id,rental_rate,rental_duration,rental_date,return_date)
                VALUES ($1,1,1,5,3,'2022-01-01 00:00:00+00','2022-01-10 00:00:00+00')
                RETURNING rental_id""",
                customer,
            )
            await conn.execute(
                """INSERT INTO payment
                (customer_id,staff_id,rental_id,amount,payment_date)
                VALUES ($1,1,$2,1,'2022-01-09 00:00:00+00')""",
                customer,
                rental,
            )
            for day, expected in ((2, 5), (6, 7), (10, 10)):
                actual = await conn.fetchval(
                    """SELECT get_customer_balance($1,
                    TIMESTAMPTZ '2022-01-01 00:00:00+00' + ($2::integer-1)*INTERVAL '1 day')""",
                    customer,
                    day,
                )
                assert actual == expected
        finally:
            await conn.execute('ROLLBACK')
            await conn.close()

    asyncio.run(run())


def test_reporting_uses_indexes_on_large_relations():
    async def run():
        conn = await connect()
        try:
            if await conn.fetchval('SELECT count(*) FROM rental') < 100_000:
                pytest.skip('index plan check requires scale >= 7; small tables may use seqscan')
            queries, _ = await reporting(conn)
            # Reading a small catalog such as film_category (about 1 MB at scale 30)
            # can be cheaper than repeated index probes after workload mutations.
            # Assert indexed access to rental/payment history and the large inventory.
            large = {'rental', 'inventory', 'film_actor'}

            def scans(node):
                if node.get('Actual Loops', 0) and node['Node Type'] == 'Seq Scan':
                    relation = node.get('Relation Name', '')
                    assert relation not in large and not relation.startswith('payment_p'), node
                for child in node.get('Plans', []):
                    scans(child)

            assert await conn.fetchval('SHOW enable_seqscan') == 'on'
            for query in queries:
                plan = json.loads(
                    await conn.fetchval('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON,TIMING OFF) ' + query)
                )[0]
                scans(plan['Plan'])
        finally:
            await conn.close()

    asyncio.run(run())


def test_oltp_read_and_update_plans_use_indexes():
    async def run():
        conn = await connect()
        try:
            if await conn.fetchval('SELECT count(*) FROM rental') < 100_000:
                pytest.skip('index plan check requires scale >= 7; small tables may use seqscan')
            bounds = dict(await conn.fetchrow('SELECT * FROM bench_bounds'))
            values = {
                name: 1
                for name in (
                    'customer_id',
                    'film_id',
                    'store_id',
                    'inventory_id',
                    'actor_id',
                    'staff_id',
                    'category_id',
                    'rental_id',
                )
            }
            values.update(
                data_start_epoch=bounds['data_start_epoch'],
                day=1,
                next_day=2,
                suffix=123,
                ret_days=3,
                duration_roll=4,
                rating_roll=5,
            )
            for name in ('01_select.sql', '03_update.sql'):
                source = (WORKLOAD_PROFILES_PATH / 'pagila/sql' / name).read_text()
                sql = '\n'.join(
                    line
                    for line in source.splitlines()
                    if not line.lstrip().startswith(('--', '\\')) and '\\gset' not in line
                )
                for query in (q.strip() for q in sql.split(';') if q.strip()):
                    if query in ('BEGIN ISOLATION LEVEL READ COMMITTED', 'COMMIT'):
                        continue
                    query = re.sub(r'(?<!:):([a-z_]+)', lambda m: str(values[m[1]]), query)
                    await conn.execute('BEGIN')
                    try:
                        plan = json.loads(
                            await conn.fetchval(
                                'EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON,TIMING OFF) ' + query
                            )
                        )[0]

                        def check(node):
                            relation = node.get('Relation Name', '')
                            if node.get('Actual Loops', 0) and node['Node Type'] == 'Seq Scan':
                                assert relation not in {'rental', 'inventory', 'film', 'customer'}
                                assert not relation.startswith('payment_p')
                            for child in node.get('Plans', []):
                                check(child)

                        check(plan['Plan'])
                    finally:
                        await conn.execute('ROLLBACK')
        finally:
            await conn.close()

    asyncio.run(run())


@pytest.mark.parametrize('protocol', ['simple', 'prepared'])
def test_all_oltp_scripts_and_concurrent_rental_attempts(tmp_path, protocol):
    assert os.environ['PGDATABASE'].startswith('perf_check_profile_validation_')
    root = WORKLOAD_PROFILES_PATH / 'pagila/sql'
    for file in ('01_select.sql', '02_insert.sql', '03_update.sql', '04_delete.sql'):
        script = (root / file).read_text()
        if file == '02_insert.sql':

            async def available_copy():
                conn = await connect()
                try:
                    return await conn.fetchval("""SELECT inventory_id FROM inventory i
                        WHERE NOT EXISTS (SELECT 1 FROM rental r
                            WHERE r.inventory_id=i.inventory_id AND r.return_date IS NULL)
                        ORDER BY inventory_id LIMIT 1""")
                finally:
                    await conn.close()

            inventory = asyncio.run(available_copy())
            assert inventory is not None
            # Exercise all normally rare branches and concurrent attempts on one copy.
            script = re.sub(
                r'\\set chance_\w+ random\([^\n]+',
                lambda m: m[0].split(' random')[0] + ' 1',
                script,
            )
            script = script.replace(
                '\\set inventory_id random(1, :max_inventory)', f'\\set inventory_id {inventory}'
            )
        path = tmp_path / file
        path.write_text(script)
        result = subprocess.run(
            [
                os.environ.get('PGBENCH', '/usr/bin/pgbench'),
                '-n',
                '-M',
                protocol,
                '-c',
                '4',
                '-j',
                '4',
                '-t',
                '3',
                '--random-seed=42',
                '-f',
                str(path),
            ],
            text=True,
            capture_output=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'number of transactions actually processed: 12/12' in result.stdout
        assert 'number of failed transactions: 0' in result.stdout
        if file == '02_insert.sql':

            async def open_rentals(copy_id):
                conn = await connect()
                try:
                    return await conn.fetchval(
                        """SELECT count(*) FROM rental
                        WHERE inventory_id=$1 AND return_date IS NULL""",
                        copy_id,
                    )
                finally:
                    await conn.close()

            assert asyncio.run(open_rentals(inventory)) == 1
    # Committed attempts must still satisfy the uniqueness/ownership invariants.
    test_loaded_data_and_availability()


def rental_sql(inventory_id, bounds, *, sec=0):
    root = WORKLOAD_PROFILES_PATH / 'pagila/sql'
    insert = (root / '02_insert.sql').read_text()
    insert = insert[insert.index('WITH available AS') : insert.index('COMMIT;')]
    update = (root / '03_update.sql').read_text()
    returning = update[update.index('UPDATE rental\n') : update.index('-- Customer contact update')]
    moving = update[update.index('BEGIN ISOLATION LEVEL READ COMMITTED;') :]
    values = dict(
        inventory_id=inventory_id,
        customer_id=1,
        store_id=2,
        data_start_epoch=bounds['data_start_epoch'],
        day=1,
        sec=sec,
        hours=1,
        ret_days=3,
    )
    return tuple(
        re.sub(r'(?<!:):([a-z_]+)', lambda m: str(values[m[1]]), sql)
        for sql in (insert, returning, moving)
    )


def test_new_rental_can_be_returned_and_rented_again():
    async def run():
        conn = await connect()
        try:
            await conn.execute('BEGIN')
            bounds = dict(await conn.fetchrow('SELECT * FROM bench_bounds'))
            # Isolate one original copy; leave the initialized dataset intact on rollback.
            await conn.execute('UPDATE rental SET return_date=rental_date WHERE inventory_id=1')
            for sec in range(3):
                insert, returning, _ = rental_sql(1, bounds, sec=sec)
                assert await conn.execute(insert) == 'INSERT 0 1'
                rental_id = await conn.fetchval(
                    'SELECT rental_id FROM rental WHERE inventory_id=1 AND return_date IS NULL'
                )
                assert rental_id > bounds['max_rental']
                assert await conn.execute(returning) == 'UPDATE 1'
                assert await conn.execute(returning) == 'UPDATE 0'
                assert await conn.fetchval('SELECT inventory_in_stock(1)')
                assert (
                    await conn.fetchval(
                        'SELECT count(*) FROM payment WHERE rental_id=$1', rental_id
                    )
                    == 1
                )
        finally:
            await conn.execute('ROLLBACK')
            await conn.close()

    asyncio.run(run())


@pytest.mark.parametrize('outcome', ['COMMIT', 'ROLLBACK'])
def test_move_rechecks_rental_after_inventory_lock_wait(outcome):
    async def run():
        observer, renter, mover = await connect(), await connect(), await connect()
        inventory_id = None
        moving = None
        try:
            inventory_id = await observer.fetchval(
                'INSERT INTO inventory(film_id,store_id) VALUES(1,1) RETURNING inventory_id'
            )
            bounds = dict(await observer.fetchrow('SELECT * FROM bench_bounds'))
            insert, _, move = rental_sql(inventory_id, bounds)
            await renter.execute('BEGIN')
            assert await renter.execute(insert) == 'INSERT 0 1'
            mover_pid = await mover.fetchval('SELECT pg_backend_pid()')
            moving = asyncio.create_task(mover.execute(move))
            # Synchronize on the real row lock, not a timing assumption.
            for _ in range(300):
                waiting = await observer.fetchval(
                    "SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=$1",
                    mover_pid,
                )
                if waiting:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail('move did not wait for the rental inventory lock')
            await renter.execute(outcome)
            await asyncio.wait_for(moving, timeout=10)
            assert await observer.fetchval(
                'SELECT store_id FROM inventory WHERE inventory_id=$1', inventory_id
            ) == (1 if outcome == 'COMMIT' else 2)
            assert await observer.fetchval(
                'SELECT count(*) FROM rental WHERE inventory_id=$1 AND return_date IS NULL',
                inventory_id,
            ) == (1 if outcome == 'COMMIT' else 0)
        finally:
            if moving is not None and not moving.done():
                moving.cancel()
                await asyncio.gather(moving, return_exceptions=True)
            await renter.execute('ROLLBACK')
            await mover.execute('ROLLBACK')
            if inventory_id is not None:
                await observer.execute(
                    'DELETE FROM payment WHERE rental_id IN '
                    '(SELECT rental_id FROM rental WHERE inventory_id=$1)',
                    inventory_id,
                )
                await observer.execute('DELETE FROM rental WHERE inventory_id=$1', inventory_id)
                await observer.execute('DELETE FROM inventory WHERE inventory_id=$1', inventory_id)
            await renter.close()
            await mover.close()
            await observer.close()

    asyncio.run(run())
