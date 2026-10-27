# Deployment and run guide

Causa is one Python package with one entry point, `causa`. The same image or checkout serves the web
UI, the HTTP API, the MCP server and the CLI. Configuration comes from `CAUSA_*` environment
variables or a `.env` file in the working directory (`src/causa/config.py`); `.env.example` lists
them all.

## 1. Local run, no Exasol needed

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/rani700/causa.git && cd causa
uv sync
uv run causa sim run      # builds the demo company in data/causa.duckdb (~70 s)
uv run causa serve        # web UI at http://127.0.0.1:8000, API docs at /docs
```

With no `.env`, the backend is DuckDB (`CAUSA_BACKEND=duckdb`, file `data/causa.duckdb`). All SQL is
written in the Exasol dialect and transpiled for DuckDB, so this is the same engine code that runs on
Exasol. Open http://127.0.0.1:8000/?tour=1 for the guided tour.

Checks:

```bash
make test                 # uv run pytest -q
make lint                 # ruff, ruff format --check, mypy
```

## 2. Running against Exasol

### Exasol SaaS

1. Create a database (free trial at https://cloud.exasol.com/signup).
2. Add the IPv4 address Causa connects from to **Security → allow list**.
3. From **Connect via tools**, take the connection string, the user name and a personal access token.
4. Configure Causa:

```bash
cp .env.example .env
```

Then edit `.env`:

| Variable | Value |
|---|---|
| `CAUSA_BACKEND` | `exasol` |
| `CAUSA_EXASOL_DSN` | `host:8563` from the connection string |
| `CAUSA_EXASOL_USER` | the database user |
| `CAUSA_EXASOL_PASSWORD` | the personal access token |
| `CAUSA_EXASOL_ENCRYPTION` | `true` |
| `CAUSA_EXASOL_TLS_INSECURE` | remove the line or set `false`; `true` is only for the local docker-db's self-signed certificate |

Never commit `.env` (it is in `.gitignore`).

### Local Exasol in Docker

```bash
make exasol-up            # docker compose -f docker/docker-compose.yml up -d
make exasol-down
```

The `exasol/docker-db` image is x86_64 only; on Apple Silicon it runs under emulation and needs about
8 GB of memory for Docker. The defaults in `.env.example` (`localhost:8563`, TLS check off) are
meant for this local database. Set `EXASOL_IMAGE` to pin a specific image tag.

### Demo data on Exasol

```bash
uv run causa sim run                  # builds the simulated company in Exasol (~35 min over a long link)
uv run causa eval health && uv run causa eval verdict
uv run causa serve
```

### Your own warehouse

```bash
uv run causa init                     # proposes causa.yaml (metrics, dimensions), checks the audit log
uv run causa serve --query-log audit  # lineage from EXA_STATISTICS.EXA_DBA_AUDIT_SQL
```

`--query-log` also takes a `SCHEMA.TABLE` holding a query log; the default, `OPS.QUERY_LOG`, is the
simulator's.

### Database permissions

Causa needs a user that can:

- **read the tables behind the metrics** declared in `causa.yaml`, and the pipeline tables upstream
  of them (health checks and replay read them);
- **read Exasol's audit log** (`EXA_STATISTICS.EXA_DBA_AUDIT_SQL` and `EXA_DBA_AUDIT_SESSIONS`): this
  needs `SELECT ANY DICTIONARY`, and auditing must be enabled (it is on by default on SaaS);
  `causa init` reports if it cannot read the log;
- **create and write its own two schemas**: `CAUSA` (metric snapshots, notifications) and
  `CAUSA_REPLAY` (scratch tables of a replay, dropped after each run).

It never writes to the company's own schemas, and nothing in the pipeline has to change.

## 3. Docker image

The `Dockerfile` builds a two-stage image: dependencies and the package from `uv.lock`, plus the demo
company built into `/app/data/causa.duckdb` as the fallback copy; the runtime stage runs as a
non-root user and serves on port 8000 (`causa serve --host 0.0.0.0 --port 8000`).

```bash
docker build -t causa .
docker run --rm -p 8000:8000 causa                     # built-in DuckDB copy
docker run --rm -p 8000:8000 --env-file .env causa     # Exasol, from your .env
```

The image sets `CAUSA_FALLBACK_DUCKDB_PATH=/app/data/causa.duckdb`, so when Exasol is configured
the built-in copy is already in place as the failover.

## 4. Production notes

- **Probes.** `GET /health` is liveness (the process answers, no dependency checks). `GET /ready` is
  readiness: which warehouse is answering (Exasol or the fallback copy) and the warm-up state. It
  never queries the warehouse.
- **Failover.** With `CAUSA_FALLBACK_DUCKDB_PATH` set, a lost Exasol connection (never an SQL error)
  switches to the local DuckDB copy, retried every two minutes; the UI says so. This keeps the demo
  up while an idle SaaS cluster has stopped itself.
- **Warm-up.** `causa serve` prepares every incident's investigation in the background at start
  (`--no-warm` to skip). Pages never wait behind it.
- **Fresh data.** Every five minutes the server checks the query log's newest statement; new
  activity reloads lineage and health and drops cached answers.
- **Watcher (optional).** `CAUSA_WATCH_MINUTES` makes the server look for new serious incidents at
  that interval (default `0`, off). Each is announced once and recorded in `CAUSA.NOTIFICATIONS`
  (`GET /notifications`). `CAUSA_BRIEF_AT` (`HH:MM`, server local time) and `CAUSA_BRIEF_METRIC` add a
  daily brief. Without a server, `causa watch` runs the same loop (`--once` for cron).
- **Slack (optional).** `CAUSA_SLACK_WEBHOOK_URL` sends alerts and briefs to Slack. Without it they
  are only recorded.
- **Ask tab (optional).** `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` enables the investigator agent.
  With both, Claude is used unless `CAUSA_AGENT_PROVIDER=openai`; `CAUSA_OPENAI_MODEL` picks the
  OpenAI model (default `gpt-5-nano`). Everything else works without a key.
- **Concurrency.** One warehouse connection per process, used by one engine task at a time.
- **Secrets.** Pass the Exasol token, model keys and the Slack webhook as environment variables from
  a secret store, never baked into the image.

## 5. Kubernetes and GitOps

The live demo runs this way:

1. **Build.** `.github/workflows/release-and-publish.yml` runs on every push to `main`: it cuts the
   next patch release (`v0.0.1`, `v0.0.2`, ...), builds the `Dockerfile` for `linux/amd64` and pushes
   `ghcr.io/rani700/causa` tagged with that version, `latest` and the short commit SHA.
   `.github/workflows/ci.yml` runs lint, types and tests on every push and pull request.
2. **Deploy.** Argo CD watches a GitOps repository holding the Kubernetes manifests and syncs them to
   the cluster. A minimal setup is:
   - a Deployment running the image on port 8000, with a liveness probe on `/health` and a readiness
     probe on `/ready`;
   - a Secret with `CAUSA_BACKEND`, the `CAUSA_EXASOL_*` connection settings and any optional keys,
     exposed to the container as environment variables;
   - a Service and an Ingress with TLS for the public hostname.
3. **Roll out.** A new image is picked up by updating the tag in the GitOps repository (or by
   restarting the Deployment when it tracks `latest`).
