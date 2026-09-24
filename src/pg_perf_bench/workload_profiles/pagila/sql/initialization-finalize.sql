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
