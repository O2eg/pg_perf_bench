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
    (SELECT max(language_id) FROM pagila.language) AS max_language;


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
