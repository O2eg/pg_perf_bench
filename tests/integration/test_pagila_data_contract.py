"""Opt-in semantic checks on a newly generated disposable Pagila database."""

import asyncio
from decimal import Decimal

import asyncpg
import pytest

from pg_perf_bench.const import WORKLOAD_PROFILES_PATH
from tests.integration.test_pagila_semantics import connect

# Reuse the opt-in/environment guard from the existing suite.
from tests.integration.test_pagila_semantics import pytestmark as environment_marks

pytestmark = environment_marks


def test_initial_history_and_receipts_are_consistent():
    async def run():
        c = await connect()
        try:
            assert not await c.fetchval("""SELECT EXISTS (
                SELECT 1 FROM (
                    SELECT rental_date, max(return_date) OVER (
                        PARTITION BY inventory_id ORDER BY rental_date, rental_id
                        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) previous_end
                    FROM rental) h WHERE rental_date < previous_end)""")
            assert not await c.fetchval("""SELECT EXISTS (
                SELECT 1 FROM rental r JOIN inventory i USING(inventory_id)
                JOIN film f USING(film_id)
                WHERE r.rental_rate<>f.rental_rate OR r.rental_duration<>f.rental_duration)""")
            assert not await c.fetchval("""SELECT EXISTS (
                SELECT r.rental_id FROM rental r JOIN payment p USING(rental_id)
                GROUP BY r.rental_id,r.rental_rate HAVING sum(p.amount)<>r.rental_rate)""")
            assert not await c.fetchval("""SELECT EXISTS (
                SELECT 1 FROM payment p CROSS JOIN bench_bounds b
                WHERE p.payment_date >= to_timestamp(b.data_start_epoch+b.data_days*86400))""")
            assert await c.fetchval(
                'SELECT count(DISTINCT store_id) FROM customer'
            ) == await c.fetchval('SELECT count(*) FROM store')
            assert await c.fetchval(
                'SELECT count(DISTINCT city_id) FROM address'
            ) == await c.fetchval('SELECT count(*) FROM city')
            assert await c.fetchval('SELECT pagila.benchmark_now() >= max(return_date) FROM rental')
        finally:
            await c.close()

    asyncio.run(run())


def test_future_receipts_keep_constraints_and_rental_terms():
    from tests.integration.test_pagila_semantics import rental_sql

    async def run():
        c = await connect()
        try:
            await c.execute('BEGIN')
            await c.execute("""UPDATE bench_bounds SET
                clock_epoch=extract(epoch FROM TIMESTAMPTZ '2022-09-01 UTC')::bigint,
                clock_started_at=clock_timestamp()""")
            copy = await c.fetchval(
                'INSERT INTO inventory(film_id,store_id) VALUES(1,1) RETURNING inventory_id'
            )
            insert, returning, _ = rental_sql(
                copy, dict(await c.fetchrow('SELECT * FROM bench_bounds'))
            )
            assert await c.execute(insert) == 'INSERT 0 1'
            row = await c.fetchrow(
                """SELECT p.tableoid::regclass::text AS leaf,
                p.amount, r.rental_id, r.rental_rate, r.rental_duration,
                f.rental_rate AS catalog_rate, f.rental_duration AS catalog_duration,
                p.payment_date=r.rental_date AS paid_at_checkout
                FROM payment p JOIN rental r USING(rental_id)
                JOIN inventory i USING(inventory_id) JOIN film f USING(film_id)
                WHERE r.inventory_id=$1""",
                copy,
            )
            assert row['leaf'].split('.')[-1] == 'payment_future'
            assert row['paid_at_checkout']
            assert row['amount'] == row['rental_rate'] == row['catalog_rate']
            assert row['rental_duration'] == row['catalog_duration']
            assert await c.execute(returning) == 'UPDATE 1'
            await c.execute('SAVEPOINT invalid_receipt')
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await c.execute(
                    """INSERT INTO payment
                    (customer_id,staff_id,rental_id,amount,payment_date)
                    VALUES (-1,1,$1,1,'2022-09-01 UTC')""",
                    row['rental_id'],
                )
            await c.execute('ROLLBACK TO SAVEPOINT invalid_receipt')
        finally:
            await c.execute('ROLLBACK')
            await c.close()

    asyncio.run(run())


def test_balance_is_historical_and_late_payment_is_once_only():
    async def run():
        c = await connect()
        try:
            await c.execute('BEGIN')
            customer = await c.fetchval("""INSERT INTO customer
                (store_id, first_name, last_name, address_id)
                VALUES (1,'Contract','Test',1) RETURNING customer_id""")
            rental = await c.fetchval(
                """INSERT INTO rental
                (customer_id, inventory_id, staff_id, rental_date, return_date,
                 rental_rate, rental_duration)
                VALUES ($1,1,1,'2022-01-01 UTC','2022-01-08 UTC',5,3)
                RETURNING rental_id""",
                customer,
            )
            before = await c.fetchval("SELECT get_customer_balance($1,'2022-01-10 UTC')", customer)
            assert before == Decimal('9')
            await c.execute(
                'UPDATE film SET rental_rate=5.99,rental_duration=8 '
                'WHERE film_id=(SELECT film_id FROM inventory WHERE inventory_id=1)'
            )
            assert (
                await c.fetchval("SELECT get_customer_balance($1,'2022-01-10 UTC')", customer)
                == before
            )
            source = (WORKLOAD_PROFILES_PATH / 'pagila/sql/03_update.sql').read_text()
            fee = source[
                source.index('WITH candidate AS') : source.index('-- Film metadata drift')
            ].replace(':customer_id', str(customer))
            assert await c.execute(fee) == 'INSERT 0 1'
            assert await c.fetchval('SELECT amount FROM payment WHERE rental_id=$1', rental) == 4
            assert await c.execute(fee) == 'INSERT 0 0'
            assert await c.fetchval('SELECT count(*) FROM payment WHERE rental_id=$1', rental) == 1
            # Payment collected now cannot rewrite receipts at an earlier cutoff.
            assert (
                await c.fetchval("SELECT get_customer_balance($1,'2022-01-10 UTC')", customer)
                == before
            )
            assert (
                await c.fetchval('SELECT get_customer_balance($1,pagila.benchmark_now())', customer)
                == 5
            )
            await c.execute(
                "UPDATE rental SET return_date='2022-01-03 UTC',late_fee_paid=false "
                'WHERE rental_id=$1',
                rental,
            )
            assert await c.execute(fee) == 'INSERT 0 0'
        finally:
            await c.execute('ROLLBACK')
            await c.close()

    asyncio.run(run())


def test_historical_overdue_includes_later_returns():
    async def run():
        c = await connect()
        try:
            await c.execute('BEGIN')
            customer = await c.fetchval("""INSERT INTO customer
                (store_id, first_name, last_name, address_id)
                VALUES (1,'Historical','Test',1) RETURNING customer_id""")
            rental = await c.fetchval(
                """INSERT INTO rental
                (customer_id,inventory_id,staff_id,rental_rate,rental_duration,rental_date,return_date)
                VALUES ($1,1,1,5,3,'2022-01-01 UTC','2022-01-10 UTC') RETURNING rental_id""",
                customer,
            )
            source = (WORKLOAD_PROFILES_PATH / 'pagila/sql/01_select.sql').read_text()
            sql = source[
                source.index('SELECT r.rental_id, f.title') : source.index('-- Actor filmography')
            ]
            sql = (
                sql.replace(':customer_id', str(customer))
                .replace(':data_start_epoch', '1640995200')
                .replace(':day', '5')
            )
            rows = await c.fetch(sql)
            assert [r['rental_id'] for r in rows] == [rental]
        finally:
            await c.execute('ROLLBACK')
            await c.close()

    asyncio.run(run())
