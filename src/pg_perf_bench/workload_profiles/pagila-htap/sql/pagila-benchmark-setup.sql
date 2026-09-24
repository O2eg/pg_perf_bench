-- pg_perf_bench benchmark setup for the pagila schema. Runs after the generator and before
-- the measured window; the database is recreated for every iteration, so this is repeated.

-- Indexes present in Sakila (the schema Pagila derives from) but absent from Pagila. The
-- OLTP scripts probe rental by customer, inventory by film, film_category by category and
-- payment by rental; without them those probes are sequential scans that grow with scale.
CREATE INDEX idx_fk_rental_customer_id ON pagila.rental USING btree (customer_id);
CREATE INDEX idx_fk_inventory_film_id ON pagila.inventory USING btree (film_id);
CREATE INDEX idx_fk_film_category_category_id ON pagila.film_category USING btree (category_id);
CREATE INDEX payment_p2022_01_rental_id_idx ON pagila.payment_p2022_01 USING btree (rental_id);
CREATE INDEX payment_p2022_02_rental_id_idx ON pagila.payment_p2022_02 USING btree (rental_id);
CREATE INDEX payment_p2022_03_rental_id_idx ON pagila.payment_p2022_03 USING btree (rental_id);
CREATE INDEX payment_p2022_04_rental_id_idx ON pagila.payment_p2022_04 USING btree (rental_id);
CREATE INDEX payment_p2022_05_rental_id_idx ON pagila.payment_p2022_05 USING btree (rental_id);
CREATE INDEX payment_p2022_06_rental_id_idx ON pagila.payment_p2022_06 USING btree (rental_id);
CREATE INDEX payment_p2022_07_rental_id_idx ON pagila.payment_p2022_07 USING btree (rental_id);

-- Selective OLTP and store/period reporting access paths.
CREATE INDEX rental_customer_date_idx ON pagila.rental (customer_id, rental_date DESC);
CREATE INDEX rental_staff_date_idx ON pagila.rental (staff_id, rental_date, inventory_id);
CREATE UNIQUE INDEX rental_open_inventory_idx ON pagila.rental (inventory_id) WHERE return_date IS NULL;
CREATE INDEX rental_open_customer_idx ON pagila.rental (customer_id, rental_date) WHERE return_date IS NULL;
CREATE INDEX inventory_film_store_idx ON pagila.inventory (film_id, store_id, inventory_id);
CREATE INDEX staff_store_idx ON pagila.staff (store_id, staff_id);
CREATE INDEX film_category_category_film_idx ON pagila.film_category (category_id, film_id);
CREATE INDEX payment_p2022_01_staff_date_idx ON pagila.payment_p2022_01 (staff_id, payment_date, rental_id);
CREATE INDEX payment_p2022_01_customer_date_idx ON pagila.payment_p2022_01 (customer_id, payment_date DESC, payment_id DESC);
CREATE INDEX payment_p2022_02_staff_date_idx ON pagila.payment_p2022_02 (staff_id, payment_date, rental_id);
CREATE INDEX payment_p2022_02_customer_date_idx ON pagila.payment_p2022_02 (customer_id, payment_date DESC, payment_id DESC);
CREATE INDEX payment_p2022_03_staff_date_idx ON pagila.payment_p2022_03 (staff_id, payment_date, rental_id);
CREATE INDEX payment_p2022_03_customer_date_idx ON pagila.payment_p2022_03 (customer_id, payment_date DESC, payment_id DESC);
CREATE INDEX payment_p2022_04_staff_date_idx ON pagila.payment_p2022_04 (staff_id, payment_date, rental_id);
CREATE INDEX payment_p2022_04_customer_date_idx ON pagila.payment_p2022_04 (customer_id, payment_date DESC, payment_id DESC);
CREATE INDEX payment_p2022_05_staff_date_idx ON pagila.payment_p2022_05 (staff_id, payment_date, rental_id);
CREATE INDEX payment_p2022_05_customer_date_idx ON pagila.payment_p2022_05 (customer_id, payment_date DESC, payment_id DESC);
CREATE INDEX payment_p2022_06_staff_date_idx ON pagila.payment_p2022_06 (staff_id, payment_date, rental_id);
CREATE INDEX payment_p2022_06_customer_date_idx ON pagila.payment_p2022_06 (customer_id, payment_date DESC, payment_id DESC);
CREATE INDEX payment_p2022_07_staff_date_idx ON pagila.payment_p2022_07 (staff_id, payment_date, rental_id);
CREATE INDEX payment_p2022_07_customer_date_idx ON pagila.payment_p2022_07 (customer_id, payment_date DESC, payment_id DESC);

-- Pagila functions reference tables without a schema; make every new session resolve them
-- instead of paying for a SET per transaction. The database-level default covers ad-hoc
-- sessions; the role-in-database setting takes precedence over an ALTER ROLE ... SET
-- search_path the benchmark role may carry, and it disappears with the database.
DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I SET search_path = pagila, public', current_database());
    EXECUTE format(
        'ALTER ROLE %I IN DATABASE %I SET search_path = pagila, public',
        current_user,
        current_database()
    );
END
$$;

-- search_path is supplied by the common loader and pgbench connection settings.
REFRESH MATERIALIZED VIEW pagila.rental_by_category;

-- One-row bounds table: the pgbench scripts read it with \gset and pick identifiers with
-- random(1, :max_*) on the client side. Rows inserted during the measured window are not
-- picked, which keeps every iteration on the same identifier space.
CREATE TABLE pagila.bench_bounds AS
SELECT
    (SELECT max(customer_id) FROM pagila.customer) AS max_customer,
    (SELECT max(film_id) FROM pagila.film) AS max_film,
    (SELECT max(store_id) FROM pagila.store) AS max_store,
    (SELECT max(inventory_id) FROM pagila.inventory) AS max_inventory,
    (SELECT max(actor_id) FROM pagila.actor) AS max_actor,
    (SELECT max(rental_id) FROM pagila.rental) AS max_rental,
    (SELECT max(staff_id) FROM pagila.staff) AS max_staff,
    (SELECT max(address_id) FROM pagila.address) AS max_address,
    (SELECT max(city_id) FROM pagila.city) AS max_city,
    (SELECT max(category_id) FROM pagila.category) AS max_category,
    (SELECT max(language_id) FROM pagila.language) AS max_language,
    bounds.data_start_epoch, bounds.data_days,
    LEAST(30, bounds.data_days) AS report_window_days,
    bounds.data_start_epoch + bounds.data_days::bigint * 86400 AS clock_epoch,
    clock_timestamp() AS clock_started_at
FROM (
    SELECT extract(epoch FROM TIMESTAMPTZ '2022-01-01 00:00:00+00')::bigint AS data_start_epoch,
           GREATEST(1, floor(extract(epoch FROM (GREATEST(
               (SELECT max(rental_date) FROM pagila.rental),
               (SELECT max(return_date) FROM pagila.rental),
               (SELECT max(payment_date) FROM pagila.payment))
               - TIMESTAMPTZ '2022-01-01 00:00:00+00')) / 86400)::integer + 1) AS data_days
) AS bounds;

CREATE FUNCTION pagila.benchmark_now() RETURNS timestamptz LANGUAGE sql VOLATILE AS $$
    SELECT to_timestamp(clock_epoch) + GREATEST(INTERVAL '0 seconds',
           clock_timestamp() - clock_started_at) FROM pagila.bench_bounds
$$;

-- Freeze and analyze so the first measured transactions do not pay for hint-bit writes or
-- stale statistics left by the bulk load.
VACUUM (FREEZE, ANALYZE) pagila.bench_bounds;
VACUUM (FREEZE, ANALYZE) pagila.actor;
VACUUM (FREEZE, ANALYZE) pagila.address;
VACUUM (FREEZE, ANALYZE) pagila.category;
VACUUM (FREEZE, ANALYZE) pagila.city;
VACUUM (FREEZE, ANALYZE) pagila.country;
VACUUM (FREEZE, ANALYZE) pagila.customer;
VACUUM (FREEZE, ANALYZE) pagila.film;
VACUUM (FREEZE, ANALYZE) pagila.film_actor;
VACUUM (FREEZE, ANALYZE) pagila.film_category;
VACUUM (FREEZE, ANALYZE) pagila.inventory;
VACUUM (FREEZE, ANALYZE) pagila.language;
VACUUM (FREEZE, ANALYZE) pagila.rental;
VACUUM (FREEZE, ANALYZE) pagila.staff;
VACUUM (FREEZE, ANALYZE) pagila.store;
VACUUM (FREEZE, ANALYZE) pagila.payment_p2022_01;
VACUUM (FREEZE, ANALYZE) pagila.payment_p2022_02;
VACUUM (FREEZE, ANALYZE) pagila.payment_p2022_03;
VACUUM (FREEZE, ANALYZE) pagila.payment_p2022_04;
VACUUM (FREEZE, ANALYZE) pagila.payment_p2022_05;
VACUUM (FREEZE, ANALYZE) pagila.payment_p2022_06;
VACUUM (FREEZE, ANALYZE) pagila.payment_p2022_07;
ANALYZE pagila.payment;

CREATE INDEX rental_unpaid_fee_customer_idx ON pagila.rental (customer_id, return_date DESC, rental_id) WHERE NOT late_fee_paid AND return_date IS NOT NULL;
CREATE INDEX payment_future_staff_date_idx ON pagila.payment_future (staff_id, payment_date, rental_id);
CREATE INDEX payment_future_customer_date_idx ON pagila.payment_future (customer_id, payment_date DESC, payment_id DESC);
VACUUM (FREEZE, ANALYZE) pagila.payment_future;

CREATE INDEX payment_future_rental_id_idx ON pagila.payment_future (rental_id);
