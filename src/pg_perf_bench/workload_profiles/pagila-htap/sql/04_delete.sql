-- pagila OLTP: remove one rental with its payments. The script weight controls the
-- delete share of the mix; identifiers that were already deleted simply affect zero rows.
-- Table bounds come from pagila.bench_bounds, a one-row table filled by the setup script,
-- so identifier selection costs one single-row read instead of a scan per table.
SELECT * FROM bench_bounds \gset
\set rental_id random(1, :max_rental)

BEGIN;
DELETE FROM payment WHERE rental_id = :rental_id;
DELETE FROM rental WHERE rental_id = :rental_id;
COMMIT;
