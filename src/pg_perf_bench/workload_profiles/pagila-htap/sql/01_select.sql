-- pagila OLTP: short read transactions with point lookups. Random identifiers come from
-- pgbench (\set random) within bounds read once per client, so transaction cost does not
-- grow with table size. Dates are offsets from 2022-01-01, the start of the generated data.
-- Table bounds come from pagila.bench_bounds, a one-row table filled by the setup script,
-- so identifier selection costs one single-row read instead of a scan per table.
SELECT * FROM bench_bounds \gset
\set customer_id random(1, :max_customer)
\set film_id random(1, :max_film)
\set store_id random(1, :max_store)
\set inventory_id random(1, :max_inventory)
\set actor_id random(1, :max_actor)
\set staff_id random(1, :max_staff)
\set category_id random(1, :max_category)
\set day random(0, :data_days - 1)
\set next_day :day + 1

-- Customer rental history
SELECT c.first_name, c.last_name, f.title, r.rental_date, r.return_date
FROM customer c
JOIN rental r ON r.customer_id = c.customer_id
JOIN inventory i ON i.inventory_id = r.inventory_id
JOIN film f ON f.film_id = i.film_id
WHERE c.customer_id = :customer_id
ORDER BY r.rental_date DESC
LIMIT 10;

-- Customer balance as of a date
SELECT get_customer_balance(:customer_id, to_timestamp(:data_start_epoch + :day * 86400));

-- Film availability at a store
SELECT film_in_stock(:film_id, :store_id);
SELECT inventory_in_stock(:inventory_id);

-- Film card: independently aggregate copies and cast, avoiding a many-to-many fan-out.
SELECT f.title, f.rental_rate, f.rating, c.name AS category,
       stock.total_copies, stock.rented_copies, cast_list.actors
FROM film f
JOIN film_category fc ON fc.film_id = f.film_id
JOIN category c ON c.category_id = fc.category_id
CROSS JOIN LATERAL (
    SELECT count(*) AS total_copies,
           count(*) FILTER (WHERE EXISTS (
               SELECT 1 FROM rental r WHERE r.inventory_id = i.inventory_id
                                        AND r.return_date IS NULL)) AS rented_copies
    FROM inventory i WHERE i.film_id = f.film_id
) stock
CROSS JOIN LATERAL (
    SELECT string_agg(a.first_name || ' ' || a.last_name, ', ' ORDER BY a.actor_id) AS actors
    FROM film_actor fa JOIN actor a ON a.actor_id = fa.actor_id
    WHERE fa.film_id = f.film_id
) cast_list
WHERE f.film_id = :film_id;

-- One result per film, even when several copies are available at the store.
SELECT f.film_id, f.title, f.rental_rate
FROM film_category fc JOIN film f ON f.film_id = fc.film_id
WHERE fc.category_id = :category_id
  AND EXISTS (
      SELECT 1 FROM inventory i
      WHERE i.film_id = f.film_id AND i.store_id = :store_id
        AND NOT EXISTS (SELECT 1 FROM rental r WHERE r.inventory_id = i.inventory_id
                                                AND r.return_date IS NULL)
  )
ORDER BY f.title
LIMIT 10;

-- Staff: rentals processed during one day
SELECT r.rental_id, r.rental_date, c.first_name || ' ' || c.last_name AS customer, f.title
FROM rental r
JOIN customer c ON c.customer_id = r.customer_id
JOIN inventory i ON i.inventory_id = r.inventory_id
JOIN film f ON f.film_id = i.film_id
WHERE r.staff_id = :staff_id
  AND r.rental_date >= to_timestamp(:data_start_epoch + :day * 86400)
  AND r.rental_date <  to_timestamp(:data_start_epoch + :next_day * 86400)
ORDER BY r.rental_date DESC
LIMIT 20;

-- Overdue rentals of a customer
SELECT r.rental_id, f.title, r.rental_date, f.rental_duration
FROM rental r
JOIN inventory i ON i.inventory_id = r.inventory_id
JOIN film f ON f.film_id = i.film_id
WHERE r.customer_id = :customer_id
  AND r.return_date IS NULL
  AND r.rental_date + make_interval(days => f.rental_duration)
      < to_timestamp(:data_start_epoch + :day * 86400)
ORDER BY r.rental_date;

-- Actor filmography
SELECT f.title, f.release_year, f.rating
FROM film_actor fa
JOIN film f ON f.film_id = fa.film_id
WHERE fa.actor_id = :actor_id
ORDER BY f.release_year DESC, f.title
LIMIT 20;

-- Inventory status of a film in a store
SELECT i.inventory_id,
       inventory_in_stock(i.inventory_id) AS in_stock,
       inventory_held_by_customer(i.inventory_id) AS held_by_customer
FROM inventory i
WHERE i.film_id = :film_id AND i.store_id = :store_id;
