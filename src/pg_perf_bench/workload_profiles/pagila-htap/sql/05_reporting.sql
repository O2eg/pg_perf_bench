-- Store dashboard over a bounded period in the generated dataset.
-- All timestamps and calendar-day groups use UTC. Each statement has an indexed
-- store/cashier, date or category predicate; no global all-history report is run.
SELECT * FROM bench_bounds \gset
\set store_id random(1, :max_store)
\set category_id random(1, :max_category)
\set day random(:report_window_days, :data_days)
\set window_start :data_start_epoch + (:day - :report_window_days) * 86400
\set window_end :data_start_epoch + :day * 86400

BEGIN READ ONLY;
-- Cash received by each cashier at the selected store, by payment date.
SELECT s.staff_id, s.first_name, s.last_name,
       count(*) AS payments, sum(p.amount) AS revenue
FROM staff s JOIN payment p ON p.staff_id = s.staff_id
WHERE s.store_id = :store_id
  AND p.payment_date >= to_timestamp(:window_start)
  AND p.payment_date < to_timestamp(:window_end)
GROUP BY s.staff_id, s.first_name, s.last_name
ORDER BY revenue DESC, s.staff_id;

-- Aggregate receipts by rented copy before looking up its film/category.
-- The scalar primary-key lookup bounds inventory access to paid copies.
WITH paid_inventory AS (
    SELECT r.inventory_id, sum(p.amount) AS revenue
    FROM staff s JOIN payment p ON p.staff_id = s.staff_id
    JOIN rental r ON r.rental_id = p.rental_id
    WHERE s.store_id = :store_id
      AND p.payment_date >= to_timestamp(:window_start)
      AND p.payment_date < to_timestamp(:window_end)
    GROUP BY r.inventory_id
)
SELECT c.category_id, c.name AS category, sum(pi.revenue) AS revenue
FROM paid_inventory pi
JOIN film_category fc ON fc.film_id = (
    SELECT i.film_id FROM inventory i WHERE i.inventory_id = pi.inventory_id)
JOIN category c ON c.category_id = fc.category_id
GROUP BY c.category_id, c.name
ORDER BY revenue DESC, c.category_id;

-- Catalog size of one category at this store; no actor fan-out or film_list view.
SELECT count(DISTINCT i.film_id) AS films, count(*) AS copies
FROM film_category fc JOIN inventory i ON i.film_id = fc.film_id
WHERE fc.category_id = :category_id AND i.store_id = :store_id;

-- Daily rentals of the selected category, counted once per rental, not payment.
SELECT (r.rental_date AT TIME ZONE 'UTC')::date AS rental_day,
       count(*) AS rentals,
       count(*) FILTER (WHERE r.return_date IS NOT NULL) AS returned_rentals
FROM staff s JOIN rental r ON r.staff_id = s.staff_id
JOIN inventory i ON i.inventory_id = r.inventory_id
JOIN film_category fc ON fc.film_id = i.film_id
WHERE s.store_id = :store_id AND fc.category_id = :category_id
  AND r.rental_date >= to_timestamp(:window_start)
  AND r.rental_date < to_timestamp(:window_end)
GROUP BY 1 ORDER BY 1;

-- Most rented titles in the selected category and store during this period.
SELECT f.film_id, f.title, count(*) AS rentals
FROM staff s JOIN rental r ON r.staff_id = s.staff_id
JOIN inventory i ON i.inventory_id = r.inventory_id
JOIN film_category fc ON fc.film_id = i.film_id
JOIN film f ON f.film_id = i.film_id
WHERE s.store_id = :store_id AND fc.category_id = :category_id
  AND r.rental_date >= to_timestamp(:window_start)
  AND r.rental_date < to_timestamp(:window_end)
GROUP BY f.film_id, f.title
ORDER BY rentals DESC, f.film_id
LIMIT 10;

-- Daily cash receipts: paid_rentals is distinct within each payment day.
SELECT (p.payment_date AT TIME ZONE 'UTC')::date AS payment_day,
       count(*) AS payments, count(DISTINCT p.rental_id) AS paid_rentals,
       sum(p.amount) AS revenue
FROM staff s JOIN payment p ON p.staff_id = s.staff_id
WHERE s.store_id = :store_id
  AND p.payment_date >= to_timestamp(:window_start)
  AND p.payment_date < to_timestamp(:window_end)
GROUP BY 1 ORDER BY 1;

-- Loyalty candidates from the same period, using real purchases and no temp DDL.
WITH qualified AS (
    SELECT p.customer_id, count(DISTINCT p.rental_id) AS rentals, sum(p.amount) AS spent
    FROM staff s JOIN payment p ON p.staff_id = s.staff_id
    WHERE s.store_id = :store_id
      AND p.payment_date >= to_timestamp(:window_start)
      AND p.payment_date < to_timestamp(:window_end)
    GROUP BY p.customer_id
    HAVING count(DISTINCT p.rental_id) >= 3 AND sum(p.amount) >= 20.00
    ORDER BY spent DESC, p.customer_id LIMIT 20
)
SELECT c.customer_id, c.first_name, c.last_name, c.email, q.rentals, q.spent
FROM qualified q JOIN customer c ON c.customer_id = q.customer_id
ORDER BY q.spent DESC, c.customer_id;
COMMIT;
