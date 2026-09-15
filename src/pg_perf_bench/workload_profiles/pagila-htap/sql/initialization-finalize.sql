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
