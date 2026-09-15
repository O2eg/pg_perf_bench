# SSH transport

Use SSH transport when PostgreSQL and host fact collection run on a remote
server while `pg_perf_bench`, `pgbench`, and `psql` run locally. The transport
uses AsyncSSH and native local port forwarding; `sshtunnel` and server-side
`AcceptEnv` changes are not required.

## Execution boundary

| Operation | Location |
|---|---|
| PostgreSQL connection | local forwarded port |
| `pgbench` and `psql` | local workload-generator host |
| Host fact collectors | remote host |
| Timed `pg_diag` OS sampler | remote host |
| PostgreSQL lifecycle | remote Patroni API when detected; otherwise `pg_ctl` as `--ssh-user` |
| Filesystem sync and optional cache drop | remote host |

## SSH setup

Create a dedicated Ed25519 key and register the server host key:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/pg_perf_bench
chmod 600 ~/.ssh/pg_perf_bench
ssh-keyscan -H db-host.example >> ~/.ssh/known_hosts
```

Install the public key for the account selected by `--ssh-user`. For benchmark
database reset without Patroni that account must be able to run `pg_ctl` for the selected
cluster; using the PostgreSQL service owner is the simplest model.

The key may be referenced directly with `--ssh-key`, or loaded into an
already-running local agent and selected with `--ssh-agent`:

```bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/pg_perf_bench
ssh-add -l
```

The two options are mutually exclusive. Agent mode requires a live Unix socket
in the inherited `SSH_AUTH_SOCK` for the complete command. `pg-perf-bench`
does not start the agent, load identities, or forward the agent to the target.
Key mode explicitly disables agent fallback.

Host-key verification is enabled by default. Use `--ssh-known-hosts` for a
dedicated file. Reserve `--ssh-insecure-no-host-key-check` for isolated,
disposable stands.

## Address model

SSH database modes use two address pairs:

- `--host` and `--port` define a free local bind address and port;
- `--remote-pg-host` and `--remote-pg-port` identify PostgreSQL as seen from
  the SSH server.

`asyncpg`, `pgbench`, and `psql` connect to the local pair. AsyncSSH forwards
that traffic to the remote pair. The local port must not already be in use.

## Read-only collection

```bash
PGPASSWORD=secret pg-perf-bench collect-all-info \
  --connection-type ssh \
  --ssh-host db-host.example \
  --ssh-user postgres \
  --ssh-key ~/.ssh/pg_perf_bench \
  --ssh-known-hosts ~/.ssh/known_hosts \
  --remote-pg-host 127.0.0.1 \
  --remote-pg-port 5432 \
  --host 127.0.0.1 \
  --port 55432 \
  --user postgres \
  --database postgres \
  --pg-bin-path /usr/lib/postgresql/18/bin \
  --report-name ssh-facts
```

Collection opens the tunnel and runs remote fact commands, but does not stop
PostgreSQL or install a configuration file.

## Benchmark

```bash
PGPASSWORD=secret pg-perf-bench benchmark \
  --connection-type ssh \
  --ssh-host db-host.example \
  --ssh-port 22 \
  --ssh-user postgres \
  --ssh-key ~/.ssh/pg_perf_bench \
  --ssh-known-hosts ~/.ssh/known_hosts \
  --remote-pg-host 127.0.0.1 \
  --remote-pg-port 5432 \
  --host 127.0.0.1 \
  --port 55432 \
  --user postgres \
  --database pg_perf_bench_test \
  --pg-data-path /var/lib/postgresql/18/main \
  --pg-bin-path /usr/lib/postgresql/18/bin \
  --allow-database-reset \
  --benchmark-type default \
  --pgbench-clients 1,4,16 \
  --init-command 'ARG_PGBENCH_PATH -i -s 10 -h ARG_PG_HOST -p ARG_PG_PORT -U ARG_PG_USER ARG_PG_DATABASE' \
  --workload-command 'ARG_PGBENCH_PATH -T 60 -c ARG_PGBENCH_CLIENTS -j ARG_PGBENCH_CLIENTS -h ARG_PG_HOST -p ARG_PG_PORT -U ARG_PG_USER ARG_PG_DATABASE' \
  --command-timeout 120 \
  --report-name ssh-pg18
```

## Fast loading in an existing database

The command above uses legacy `pgbench -i` initialization. Schema reset requires
an initialization profile with a common LoadPlan; adding only `--reset-mode schema`
to that command will fail. Pre-create a dedicated `pg_perf_bench_test` database,
ensure the SQL role has CREATE/schema ownership and replication-monitoring rights,
and use a bundled profile instead:

```bash
PGPASSWORD=secret pg-perf-bench benchmark \
  --connection-type ssh --ssh-host db-host.example --ssh-user postgres \
  --ssh-key ~/.ssh/pg_perf_bench --ssh-known-hosts ~/.ssh/known_hosts \
  --remote-pg-host 127.0.0.1 --remote-pg-port 5432 \
  --host 127.0.0.1 --port 55432 --user postgres \
  --pg-data-path /var/lib/postgresql/18/main \
  --pg-bin-path /usr/lib/postgresql/18/bin \
  --database pg_perf_bench_test --allow-database-reset --reset-mode schema \
  --workload-profile pagila --workload-scale 1.15 --init-mode fast \
  --init-fsync keep --init-workers 4 --init-batch-rows 100000 \
  --workload-duration-seconds 5 --pgbench-clients 1,2 \
  --command-timeout 300 --report-name ssh-pagila-schema
```

This loads in batches, converts UNLOGGED tables to LOGGED, builds indexes in
parallel and waits for direct physical replicas to replay preparation WAL.
The database and server are not restarted. Server paths remain required for this
transport's evidence collection. Use `--reset-mode database` for a database
recreation/restart run; that mode also requires CREATEDB and access to `postgres`.

The default protocol is simple. Add `--pgbench-prepared` to this profile command
for prepared statements. `--init-synchronous-commit keep` is the default; add
`--init-synchronous-commit off` only when deliberately skipping acknowledgement
waits during loading. The workload's commit policy and the replay barrier remain
unchanged. For larger runs, increase scale, duration, clients and timeout together.
For SQL-only comparisons with managed PostgreSQL, follow the
[paired managed/Patroni example](managed_mode_usage.md#compare-managed-postgresql-and-patroni).

## Configuration and lifecycle

[Patroni is detected automatically](../README.md#patroni) in database reset mode. The SSH account must
be able to read its process environment and configuration. API requests execute
on the remote host using Patroni's configured authentication and TLS settings.
With Patroni, `--pg-custom-config` and `--drop-os-caches` are rejected before changes.

The remote discovery interpreter and Patroni's Python environment must be Python
3.10 or newer. The local client's Python version does not satisfy this remote
requirement. See the linked Patroni section for mTLS client settings.

Without Patroni, `--pg-custom-config` names a local source file. It is uploaded
and atomically renamed to the remote cluster's `postgresql.conf` before the reset sequence.

`--drop-os-caches` runs on the remote host and requires a narrow passwordless
sudo rule. Hardware collectors use `sudo -n` and fail fast when permission is
not available.

The load commands always execute locally through the tunnel. The newest local
pgbench is selected automatically even when the remote server is PostgreSQL
10–18. Concurrent CPU, RAM, disk, and network sampling executes on the remote
database host through the already authenticated SSH session.

## Common failures

- host-key error: update the selected known-hosts file after independently
  verifying the server key;
- authentication failure in key mode: verify `--ssh-user`, private key
  permissions, and the installed public key;
- authentication failure in agent mode: verify `SSH_AUTH_SOCK`, `ssh-add -l`,
  the agent lifetime, and the installed public key;
- local bind failure: choose an unused `--port`;
- PostgreSQL connection failure with working SSH: verify the remote address,
  PostgreSQL authentication, and listen rules;
- lifecycle failure: ensure `--ssh-user` owns or can control the cluster.
