-- pagila OLTP: write transactions. An available copy is rented with its payment;
-- customer registration, catalogue and staff changes are gated by pgbench-side
-- probabilities, so the mix stays shop-like and reproducible under --random-seed. Dates
-- use a logical clock after the generated history; later payments have a future partition.
-- Table bounds come from pagila.bench_bounds, a one-row table filled by the setup script,
-- so identifier selection costs one single-row read instead of a scan per table.
SELECT * FROM bench_bounds \gset
\set customer_id random(1, :max_customer)
\set inventory_id random(1, :max_inventory)
\set staff_id random(1, :max_staff)
\set store_id random(1, :max_store)
\set film_id random(1, :max_film)
\set category_id random(1, :max_category)
\set actor_id random(1, :max_actor)
\set city_id random(1, :max_city)
\set language_id random(1, :max_language)
\set address_id random(1, :max_address)
\set day random(0, :data_days - 1)
\set sec random(0, 86399)
\set hours random(1, 72)
\set suffix random(1, 1000000)
\set rating_idx random(1, 5)
\set copies random(1, 3)
\set chance_customer random(1, 100)
\set chance_film random(1, 100)
\set chance_stock random(1, 100)
\set chance_staff random(1, 1000)
\set chance_store random(1, 1000)

-- Rental attempt: unavailable or concurrently locked copies are skipped. The partial
-- unique index also prevents races from creating two open rentals for one copy.
BEGIN;
WITH available AS (
    SELECT i.inventory_id, s.manager_staff_id AS staff_id, f.rental_rate, f.rental_duration
    FROM inventory i JOIN store s ON s.store_id = i.store_id
    JOIN film f ON f.film_id = i.film_id
    WHERE i.inventory_id = :inventory_id
      AND NOT EXISTS (SELECT 1 FROM rental r WHERE r.inventory_id = i.inventory_id
                                              AND r.return_date IS NULL)
    FOR UPDATE OF i SKIP LOCKED
), new_rental AS (
    INSERT INTO rental (rental_date, inventory_id, customer_id, staff_id, rental_rate, rental_duration)
    SELECT pagila.benchmark_now(), inventory_id, :customer_id::bigint, staff_id,
           rental_rate, rental_duration FROM available
    ON CONFLICT DO NOTHING
    RETURNING rental_id, rental_date, staff_id, rental_rate
)
INSERT INTO payment (customer_id, staff_id, rental_id, amount, payment_date)
SELECT :customer_id::bigint, nr.staff_id, nr.rental_id,
       nr.rental_rate, nr.rental_date
FROM new_rental nr;
COMMIT;

-- New customer with address (one transaction in ten)
\if :chance_customer <= 10
BEGIN;
WITH new_address AS (
    INSERT INTO address (address, district, city_id, postal_code, phone)
    VALUES (
        'Street ' || :suffix,
        'District ' || (:suffix % 100),
        :city_id,
        lpad((:suffix % 100000)::text, 5, '0'),
        '+1-' || lpad(:suffix::text, 10, '0')
    )
    RETURNING address_id
)
INSERT INTO customer
    (store_id, first_name, last_name, email, address_id, activebool, create_date, active)
SELECT
    :store_id::bigint,
    'Name' || :suffix,
    'Surname' || (:suffix % 1000),
    'customer' || :suffix || '@example.test',
    na.address_id,
    true,
    (pagila.benchmark_now() AT TIME ZONE 'UTC')::date,
    1
FROM new_address AS na;
COMMIT;
\endif

-- New film with category, actor and first inventory copy (one transaction in twenty)
\if :chance_film <= 5
BEGIN;
WITH new_film AS (
    INSERT INTO film
        (title, description, release_year, language_id, rental_duration, rental_rate,
         length, replacement_cost, rating, special_features)
    VALUES (
        'Film ' || :suffix,
        'Description ' || :suffix,
        (2000 + :suffix % 23)::year,
        :language_id,
        2 + :suffix % 7,
        (0.99 + (:suffix % 5))::numeric(4,2),
        60 + :suffix % 120,
        (9.99 + (:suffix % 20))::numeric(5,2),
        (ARRAY['G', 'PG', 'PG-13', 'R', 'NC-17'])[:rating_idx::integer]::mpaa_rating,
        ARRAY['Trailers', 'Commentaries']
    )
    RETURNING film_id
),
new_film_category AS (
    INSERT INTO film_category (film_id, category_id)
    SELECT film_id, :category_id::bigint FROM new_film
),
new_film_actor AS (
    INSERT INTO film_actor (actor_id, film_id)
    SELECT :actor_id::bigint, film_id FROM new_film
)
INSERT INTO inventory (film_id, store_id)
SELECT film_id, :store_id::bigint FROM new_film;
COMMIT;
\endif

-- Extra copies of a film for a store (one transaction in ten)
\if :chance_stock <= 10
INSERT INTO inventory (film_id, store_id)
SELECT :film_id::bigint, :store_id::bigint
FROM generate_series(1, :copies::integer);
\endif

-- New staff member (one transaction in five hundred)
\if :chance_staff <= 2
BEGIN;
WITH new_address AS (
    INSERT INTO address (address, district, city_id, postal_code, phone)
    VALUES (
        'Staff Street ' || :suffix,
        'Staff District ' || (:suffix % 100),
        :city_id,
        lpad((:suffix % 100000)::text, 5, '0'),
        '+1-' || lpad(:suffix::text, 10, '0')
    )
    RETURNING address_id
)
INSERT INTO staff (first_name, last_name, address_id, email, store_id, active, username, password)
SELECT
    'Staff' || :suffix,
    'StaffSurname' || (:suffix % 1000),
    na.address_id,
    'staff' || :suffix || '@example.test',
    :store_id::bigint,
    true,
    'staff_' || :suffix,
    NULL
FROM new_address AS na;
COMMIT;
\endif

-- New store managed by a staff member without a store (one transaction in five hundred).
-- SKIP LOCKED plus ON CONFLICT keeps concurrent clients from racing on the same manager.
\if :chance_store <= 2
BEGIN;
WITH new_staff AS (
    SELECT staff_id
    FROM staff
    WHERE NOT EXISTS (
        SELECT 1 FROM store WHERE store.manager_staff_id = staff.staff_id
    )
    ORDER BY staff_id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
), new_store AS (
    INSERT INTO store (manager_staff_id, address_id)
    SELECT staff_id, :address_id::bigint FROM new_staff
    ON CONFLICT (manager_staff_id) DO NOTHING
    RETURNING store_id, manager_staff_id
)
UPDATE staff SET store_id = new_store.store_id
FROM new_store WHERE staff.staff_id = new_store.manager_staff_id;
COMMIT;
\endif
