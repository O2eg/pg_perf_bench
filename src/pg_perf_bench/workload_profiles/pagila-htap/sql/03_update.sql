-- pagila OLTP: update transactions by primary key or indexed foreign key.
-- Table bounds come from pagila.bench_bounds, a one-row table filled by the setup script,
-- so identifier selection costs one single-row read instead of a scan per table.
SELECT * FROM bench_bounds \gset
\set customer_id random(1, :max_customer)
\set film_id random(1, :max_film)
\set staff_id random(1, :max_staff)
\set inventory_id random(1, :max_inventory)
\set store_id random(1, :max_store)
\set day random(0, :data_days - 1)
\set ret_days random(1, 8)
\set suffix random(1, 1000000)
\set rating_roll random(0, 19)
\set duration_roll random(0, 9)

-- Return the selected copy, including rentals created after initialization.
-- The partial unique inventory index locates at most one open rental.
UPDATE rental
SET return_date = rental_date + make_interval(days => :ret_days)
WHERE inventory_id = :inventory_id AND return_date IS NULL;

-- Customer contact update
UPDATE customer
SET email = 'updated' || :suffix || '@example.test',
    activebool = (:suffix % 10 <> 0),
    active = CASE WHEN :suffix % 10 = 0 THEN 0 ELSE 1 END
WHERE customer_id = :customer_id;

-- Re-price a film by its rental count
UPDATE film f
SET rental_rate = CASE
        WHEN s.rentals > 50 THEN LEAST(f.rental_rate * 1.1, 4.99)
        WHEN s.rentals < 10 THEN GREATEST(f.rental_rate * 0.9, 0.99)
        ELSE f.rental_rate
    END
FROM (
    SELECT count(*) AS rentals
    FROM inventory i
    JOIN rental r ON r.inventory_id = i.inventory_id
    WHERE i.film_id = :film_id
) AS s
WHERE f.film_id = :film_id;

-- Staff account update
UPDATE staff
SET email = 'staff' || :suffix || '@example.test',
    username = 'user' || (:suffix % 100000),
    password = 'pass' || :suffix
WHERE staff_id = :staff_id;

-- Late fee on exactly one latest payment; partition-local customer/date indexes.
WITH latest AS (
    SELECT payment_date, payment_id FROM payment WHERE customer_id = :customer_id
    ORDER BY payment_date DESC, payment_id DESC LIMIT 1
)
UPDATE payment p SET amount = p.amount + 1.00
FROM latest WHERE p.payment_date = latest.payment_date AND p.payment_id = latest.payment_id;

-- Film metadata drift (a CASE test parameter must be cast: prepared mode would type it text)
UPDATE film
SET rental_duration = GREATEST(3, LEAST(7, rental_duration
        + CASE :duration_roll::integer WHEN 0 THEN -1 WHEN 1 THEN 1 ELSE 0 END)),
    replacement_cost = GREATEST(9.99, LEAST(29.99, replacement_cost + (:duration_roll - 5) * 0.2)),
    rating = CASE :rating_roll::integer
        WHEN 0 THEN 'G'::mpaa_rating
        WHEN 1 THEN 'PG'::mpaa_rating
        WHEN 2 THEN 'PG-13'::mpaa_rating
        WHEN 3 THEN 'R'::mpaa_rating
        WHEN 4 THEN 'NC-17'::mpaa_rating
        ELSE rating
    END
WHERE film_id = :film_id;

-- Touch overdue rentals of a customer
UPDATE rental r
SET last_update = now()
FROM inventory i
JOIN film f ON f.film_id = i.film_id
WHERE i.inventory_id = r.inventory_id
  AND r.customer_id = :customer_id
  AND r.return_date IS NULL
  AND r.rental_date + make_interval(days => f.rental_duration)
      < to_timestamp(:data_start_epoch + :day * 86400);

-- Lock first, then check availability in a fresh READ COMMITTED snapshot.
-- A rental committed while this lock was pending must prevent the move.
BEGIN ISOLATION LEVEL READ COMMITTED;
SELECT inventory_id FROM inventory
WHERE inventory_id = :inventory_id
FOR UPDATE;
UPDATE inventory
SET store_id = :store_id
WHERE inventory_id = :inventory_id
  AND store_id <> :store_id
  AND NOT EXISTS (
      SELECT 1 FROM rental r WHERE r.inventory_id = :inventory_id AND r.return_date IS NULL
  );
COMMIT;
