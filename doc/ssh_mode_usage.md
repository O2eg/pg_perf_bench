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
| `pg_ctl` lifecycle | remote host as `--ssh-user` |
| Filesystem sync and optional cache drop | remote host |

## SSH setup

Create a dedicated Ed25519 key and register the server host key:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/pg_perf_bench
chmod 600 ~/.ssh/pg_perf_bench
ssh-keyscan -H db-host.example >> ~/.ssh/known_hosts
```

Install the public key for the account selected by `--ssh-user`. For benchmark
mode that account must be able to run `pg_ctl` for the selected cluster; using
the PostgreSQL service owner is the simplest model.

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

`--pg-custom-config` names a local source file. It is uploaded and atomically
renamed to the remote cluster's `postgresql.conf` before the reset sequence.

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
