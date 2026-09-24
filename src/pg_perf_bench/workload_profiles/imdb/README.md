# IMDb movie-catalog profile

A deterministic synthetic dataset with 21 tables and **11 active independent read-only
pgbench scripts**. Three additional scripts are commented out pending performance
optimization and excluded from the default workload. Each active script executes one business SELECT: a movie card,
filmography, ranking, catalog search or analytical report. This models useful
movie-catalog operations; the documented script weights are a reproducible benchmark mix,
not a measured production traffic distribution.

The measured workload contains no `INSERT`, `UPDATE` or `DELETE`. Initialization
still writes data and builds indexes. During measurement, read-only sorts and
hash operations can spill to temporary files, producing disk writes without
changing catalog data. Reported TPS counts completed pgbench scripts, not
individual `SELECT`s; some scripts also run a helper query to choose parameters.
See the [profile comparison table](../../../../README.md#bundled-workload-profiles).

The previous 118 SELECTs are retained separately in [`sql/planner`](sql/planner/README.md)
as individual diagnostic scripts. See their [result interpretation](sql/planner/README.md).
They are **not selected by the default workload**.
Many compute independent minima or aggregate the entire dataset, so their results
must not be interpreted as a coherent movie record or interactive application traffic.

## Data contract

Scale 1 generates 100,000 titles and 100,000 people, approximately 2.6 million
rows before relationship deduplication. Scale may be fractional; minimum table
sizes keep the reference scenarios usable on small datasets.

Every title has keywords, numeric rating and votes, budget, genre, country,
release metadata, a production-company credit, at least one actor/actress credit
and a writer credit. Cast roles and character references agree with gender and
credit type. Queries specify attribute IDs directly; these IDs are a stable
contract of the generated `info_type` dictionary. Numeric values retain text
storage, with a matching numeric expression index for ordering and comparison.

Names such as Tim, Robert and Angela occur throughout the scaled background.
Murder and Money title families also scale: a Murder title receives the murder
keyword and a Money title receives the sequel keyword. Additional keywords keep
a skewed popularity distribution. Selecting the first several hundred keyword
IDs therefore selects popular themes, not a uniform sample of the catalog.

Production years remain in 1950–2022 and release years extend through 2024;
queries use these explicit historical windows. Dated credits/releases never
precede production. Each dated company note represents one event with one year;
VHS, Blu-ray and theatrical releases are not merged into a shared date. Production
year is not substituted for release year. The synthetic VHS release window is
1980–2005, and Blu-ray dates lie in 2006–2024. Background titles produced in
1990–1994 receive VHS releases in 1994, including reissues of earlier productions;
other historical VHS releases have independent dates. The VHS fixture is a 1994
production. The rated Murder fixture passes the catalog
rating threshold. The Money sequel fixture points toward an earlier title;
inverse relationships point forward. Episodes have existing parent series and
consistent years/season/episode metadata. Relationship sets are deduplicated.

The first 22 titles and first eight people are small compatibility fixtures.
They supplement the scalable background; the default workload does not rely
exclusively on their names. Top-250 and bottom-10 are global, disjoint rankings
ordered by rating, votes and title ID. Ranking queries return the ranked movies
without unrelated year/company filters that could accidentally eliminate the list.

All pseudo-random data uses deterministic `hashint8()` streams. Modular mappings
use strides coprime to the actual cardinality, including fractional scales.
Loader worker count and batch size do not change logical data.

## Operations

| Script | Weight | Business result / scope |
|---|---:|---|
| `01_company_catalog` | 1 | Company/year title counts and average ratings, 2000–2022; each company/title is counted once |
| `02_people_by_keyword` | 5 | Actors/actresses associated with one sampled popular theme, 2000–2022; distinct-title counts |
| `03_keyword_trends` | Disabled | Catalog-wide keyword trends by five-year period from 1980; a deliberately broad report |
| `04_genre_cast` | Disabled | Credit, distinct-person and distinct-title counts by role for one genre, 2010–2022 |
| `05_movie_details` | 30 | One sampled movie with rating, budget and separate collections of credits, keywords and companies |
| `06_person_filmography` | 20 | Existing participant's movies with roles and ratings; the participant is sampled through an actual credit |
| `07_top_ranked` | 3 | Top 50 entries from the global top-250 list, with ratings and votes |
| `08_bottom_ranked` | 1 | Global bottom-10 list, with ratings and votes |
| `09_vhs_archive` | 2 | US VHS releases in 1994, including 1990–1993 reissues; production and release years are returned separately |
| `10_crime_catalog` | Disabled | Rated contemporary Murder titles with the matching keyword and country metadata |
| `11_movie_links` | 5 | Money-title sequel/follows pairs, preserving source, target and relation direction |
| `12_cast_coverage` | 2 | Catalog completeness by production year, subject and status, 2010–2022 |
| `13_genre_chart` | 15 | Top 20 movies by votes for one genre, 2010–2022, with a writer credit |
| `14_people_search` | 8 | Name-search candidates followed by their actual credit counts and career years |

Active weights sum to **92**; each selection probability is its weight divided by 92.
Broad company and completeness reports receive 3/92 (about 3.26%) of script selections;
lookups, searches and charts form the rest.
These are selection probabilities, not percentages of elapsed time: expensive
reports can consume most server resources even with a small selection weight.

Scripts 03, 04 and 10 are retained as block comments and listed under
`files.disabled_queries` for source evidence. They have no `-f` entries in the
default command, so they cannot count as empty successful transactions. To restore
one, uncomment it, move it to `files.queries` and add its weighted `-f` argument;
the previous weights were 1, 2 and 5 respectively.

Keywords, genres, name patterns and sampled IDs vary within the scripts using
pgbench variables. `--random-seed=42` makes selection reproducible for the same
client setup. Movie-card and filmography scripts read the actual minimum/maximum
ID using an indexed bounds query and `\gset`, then call pgbench
`random(:bounds_lo, :bounds_hi)` directly. There is no fixed-size ticket domain or
integer rescaling that excludes IDs on large datasets. The generator guarantees
nonempty, dense IDs for `title` and `cast_info`; filmography samples credits
uniformly, not people (a person with more credits is more likely to be selected).
Manually deleting rows violates this contract: an absent sampled ID returns no
record; there is no nearest-ID substitution that silently biases selection.
The bounds lookup adds one SQL round trip per script, included in latency. Each
script still performs one business SELECT, in addition to metadata/control SQL.
See [pgbench custom scripts](https://www.postgresql.org/docs/current/pgbench.html#PGBENCH-SCRIPTS)
for `\gset` and random-variable semantics. A movie card's child collections are aggregated independently; existence
filters use `EXISTS` when multiplying matching relationships has no business meaning.

The archived writer, voice-credit and complete-catalog probes also use existence
filters where this preserves their aggregate results. Other planner probes retain
broad or deliberately restrictive predicates: a NULL aggregate may be a valid
negative result there. They are not guaranteed interactive response times.

## Approximate database sizes

Reference PostgreSQL 18 footprint at scale 8: **4,343,076,543 bytes**, including
**1,943,977,984 bytes of table storage** and **2,389,295,104 bytes of indexes**.
The remaining approximately 9.8 MB is database overhead.

| Target database size, including indexes | Approximate `--workload-scale` | Table storage | Index storage | Titles |
|---|---:|---:|---:|---:|
| 1 GB | 2 | 0.49 GB | 0.60 GB | 200,000 |
| 10 GB | 18 | 4.4 GB | 5.4 GB | 1,800,000 |
| 100 GB | 185 | 45 GB | 55 GB | 18,500,000 |
| 1 TB | 1850 | 450 GB | 553 GB | 185,000,000 |

Units are decimal: 1 GB = 10^9 bytes; 1 TB = 1000 GB. These are rounded estimates,
not measurements at every target size or hard storage limits. Fixed overhead is
not multiplied by scale. String lengths, page occupancy, indexes and PostgreSQL
settings affect the actual size; leave headroom and inspect storage after loading.
WAL, temporary files, backups and replica copies are excluded. Scale units differ
between IMDb and Pagila.

## Running and validating

The common initializer loads bounded batches, creates indexes/constraints,
restores durability and waits for replica replay before pgbench.
`--init-mode legacy` installs the same indexes. See [initialization](../../../../INITIALIZATION.md).

Example (connection and reset-safety arguments omitted):

```bash
pg-perf-bench benchmark --workload-profile imdb --workload-scale 8 \
  --pgbench-clients 1,4,16 --workload-duration-seconds 60 \
  --statement-timeout-seconds 100 --command-timeout 600
```

The default measurement window is 120 seconds; IMDb's default per-statement limit
is 300 seconds. `--statement-timeout-seconds` applies to SQL-file workloads and is
independent of the initialization/process `--command-timeout`. The runner stages
copies of the scripts with explicit session SET/RESET commands, so session poolers
that ignore startup `PGOPTIONS` still enforce the limit. Sources are not overwritten.
SET/RESET round trips are included in pgbench script latency; compare runs with
the same timeout policy. Use session pooling with connection cleanup; transaction
pooling is unsupported.
A timeout aborts the affected pgbench run and is reported as a failure, not a valid
TPS measurement. Completed iteration evidence and available failed-command output remain
in `<report-name>.progress.json` even if no final HTML can be produced. If the outer
process deadline expires first, the checkpoint records that error but captured command
output may be unavailable. The generic process timeout defaults to 300 seconds;
raise it when allowing a 300-second SQL tail after the measurement window.

`pgbench -T` allows an in-flight script to finish. One business SELECT per script
bounds this tail to that operation plus control statements; the server timeout
also limits the operation. Set `--command-timeout` above the workload window plus
the statement timeout and allow sufficient time for initialization.

To select an archived probe explicitly, override `--workload-command`, for example:

```text
ARG_PGBENCH_PATH -n -M ARG_PGBENCH_PROTOCOL -c ARG_PGBENCH_CLIENTS -j ARG_PGBENCH_CLIENTS --time=ARG_WORKLOAD_DURATION_SECONDS -f ARG_WORKLOAD_PATH/sql/planner/select_18_03.sql ARG_PG_DATABASE
```

Validate a new dataset with one client before increasing concurrency. Check useful
results, temporal/reference invariants and actual plans; sequential scans can be
appropriate for broad reports. Parallel timings include resource contention and
are not interchangeable with isolated timings. The integration suite executes all
default scripts in simple/prepared protocols, tests parameter domains and compares
rewritten planner probes with their reference SQL on disposable databases. An independent
Python oracle reconstructs every default result from source records, including all
small parameter domains, sampled-ID boundaries, ranking order, distinct counts and
nested collections. This oracle is bounded to datasets with at most 50,000 titles;
reference/temporal checks and script execution also run on larger datasets.
Sampling regression tests execute the production pgbench sampling commands in
simple and prepared modes, with virtual bounds up to 18.5 million IDs and an
otherwise unreachable final credit. They check range/distribution without
creating oversized datasets. Temporal tests require one event year, valid format
windows and earlier productions in the actual 1994 VHS result.

These generator and workload changes alter benchmark semantics and source hashes.
Regenerate data and rerun all compared environments using the same version;
results from the old 38-script mix are not directly comparable.
