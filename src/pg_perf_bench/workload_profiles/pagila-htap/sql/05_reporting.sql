-- pagila HTAP: reporting queries that scan and aggregate whole tables while the OLTP
-- scripts run. Selected with a small weight, they model the analyst next to the shop.
\set day random(30, 200)

BEGIN;
SELECT * FROM sales_by_store;

SELECT * FROM sales_by_film_category;

SELECT count(*) AS films, count(DISTINCT category) AS categories
FROM film_list;

SELECT rb.category, rb.total_sales,
       count(r.rental_id) AS rental_count,
       round(avg(f.rental_rate), 2) AS avg_rental_rate
FROM rental_by_category rb
JOIN category c ON c.name = rb.category
JOIN film_category fc ON fc.category_id = c.category_id
JOIN film f ON f.film_id = fc.film_id
JOIN inventory i ON i.film_id = f.film_id
JOIN rental r ON r.inventory_id = i.inventory_id
GROUP BY rb.category, rb.total_sales
ORDER BY rb.total_sales DESC;

SELECT c.name AS category, count(*) AS rentals
FROM rental r
JOIN inventory i ON i.inventory_id = r.inventory_id
JOIN film_category fc ON fc.film_id = i.film_id
JOIN category c ON c.category_id = fc.category_id
WHERE r.rental_date >= TIMESTAMPTZ '2022-01-01 00:00:00+00' + make_interval(days => :day) - INTERVAL '30 days'
  AND r.rental_date <  TIMESTAMPTZ '2022-01-01 00:00:00+00' + make_interval(days => :day)
GROUP BY c.name
ORDER BY rentals DESC;

SELECT date_trunc('day', r.rental_date) AS rental_day, count(*) AS rentals, sum(p.amount) AS revenue
FROM rental r
JOIN payment p ON p.rental_id = r.rental_id
WHERE r.rental_date >= TIMESTAMPTZ '2022-01-01 00:00:00+00' + make_interval(days => :day) - INTERVAL '30 days'
  AND r.rental_date <  TIMESTAMPTZ '2022-01-01 00:00:00+00' + make_interval(days => :day)
GROUP BY 1
ORDER BY 1 DESC;

SELECT first_name, last_name, email
FROM rewards_report(3, 100.00)
LIMIT 5;
COMMIT;
