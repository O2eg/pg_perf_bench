# Archived planner probes

These 118 SQL files preserve the previous IMDb workload for explicit diagnosis.
They are excluded from the default profile command. Each file executes one SELECT,
so a pgbench script no longer groups several expensive queries into one iteration.

## Result interpretation

The 113 `select_*` probes compute independent minima. For example,
`MIN(person.name), MIN(title.title), MIN(rating)` summarizes each column separately:
the returned person, film and rating need not belong to the same joined row.
Even when a particular dataset happens to produce one matching row, that is not a
SQL guarantee. Treat the output as aggregate values, never as a movie card or a
relationship between the displayed entities. An all-NULL aggregate means that no
non-NULL values met its restrictions; it does not count as a useful catalog result.

Restrictive probes may legitimately be empty. Intersecting the global bottom ten
with one year range, title prefix and company country can eliminate the whole
list. Character-name, multiple-role and linked-series predicates also depend on
joint distributions; growing scale alone does not guarantee a match.

The five numbered reports retain the former grouped reports and join stress
query. The join stress query enumerates combinations of companies, credits and
keywords; its rows are not distinct movies, and a credit is not necessarily an
actor/actress. Such multiplication is deliberate diagnostic work, not a movie-card
implementation or a realistic traffic mix.

## Choosing a workload

Use the 14 scripts in the parent `sql` directory for the supported catalog workload.
They return coherent records or explicitly defined aggregate groups and are
validated against an independent source-record oracle. See the
[profile README](../../README.md) for data contracts, weights and timeout policy.

Use this archive only when investigating optimizer behavior. Check the matching
population and actual plan, and set a per-statement timeout. Do not mix its timings
or TPS with the default workload: SQL semantics and script boundaries differ.
