# Causa

**A root-cause engine for business metrics, built on Exasol.**

When a KPI moves, Causa answers the three questions that usually take a data team days:

1. **Is it real, or did the data break?** It splits the change into data incidents and real business
   change, and measures each incident's exact effect by re-running the company's own pipeline on
   repaired data.
2. **Why did it move?** Which segments drove the real part of the change.
3. **Who consumed the wrong number?** Every dashboard, report, ML model and sync that read affected
   data, with a correction notice for each.

Data observability tools flag broken tables but can't say what that means for revenue. BI "explain"
features break revenue down but assume the data is correct. Causa joins the two, from nothing more
than read access to the warehouse and its query log.

- **Live demo:** https://causa.codeshare.co.in (guided tour: https://causa.codeshare.co.in/?tour=1).
  It runs on Exasol SaaS, and on a built-in copy of the same data while the trial cluster is asleep.
- **Problem statement:** [docs/problem-statement.md](docs/problem-statement.md)
- **Deployment and run guide:** [docs/deployment.md](docs/deployment.md)
- **Demo video:** (link added at submission)
- **Pitch deck:** (link added at submission)

### How well it works

Scored against ground truth on [20 randomly drawn companies](docs/benchmark.md) (120 data incidents,
100 business changes):

| | |
|---|---|
| Data incidents raised as alerts | **100%** |
| Business changes left calm (not alerted as data problems) | **97%** |
| Alerts that match a real incident | **93%** |
| Verdicts whose data impact is within 10% of the change | **218 / 220** (median error 0.0%) |

Both misses (a big promotion partly read as a data problem; a late batch whose missing order lines
were not recognised as healed) and every false alarm are listed in the report. The simulator is a
benchmark: these numbers say the engines work on these kinds of incidents, not that they will on
every warehouse. Reproduce with `uv run causa eval bench --seeds 20`.

## Status

| | |
|---|---|
| Lineage from the query log (column-level, row-gating predicates, blast radius) | ✅ |
| Data health detection, lineage-focused, grouped into cases | ✅ |
| Metric attribution (drill-down, volume/rate split, significance, restatements) | ✅ |
| Business-vs-data verdict with evidence and confidence | ✅ |
| Counterfactual replay of the real pipeline, recall with correction notices | ✅ |
| Web UI with a guided tour; HTTP API; CLI | ✅ |
| Investigator agent on Claude or OpenAI (`gpt-5-nano`), with cost per answer | ✅ |
| MCP server for Claude Desktop / Claude Code | ✅ |
| Watch: each new serious incident announced once, daily brief, Slack | ✅ |
| `causa init`: metric definitions proposed for a real warehouse | ✅ |
| Production: failover to a local copy, live refresh, health/readiness probes, container image | ✅ |
| Validated end to end on Exasol SaaS (2025.1); benchmark on 20 random companies | ✅ |

## Setup

Prerequisites: Python 3.12 or newer and [uv](https://docs.astral.sh/uv/). Docker only if you want a
local Exasol (`make exasol-up`). No Exasol is needed to try Causa: by default it runs on a built-in
DuckDB copy of the demo company.

```bash
git clone https://github.com/rani700/causa.git && cd causa
uv sync                            # install (or: make install)
uv run causa sim run               # build the demo company in data/causa.duckdb (~70s)
make test                          # uv run pytest -q
make lint                          # ruff, ruff format --check, mypy
```

## Usage

### Web UI and API

```bash
uv run causa serve                 # web UI at http://127.0.0.1:8000, API docs at /docs
```

Open http://127.0.0.1:8000/?tour=1 for the guided tour. The API serves `/metrics`,
`/metrics/{m}/explain|verdict|recall|brief`, `/health/cases`, `/lineage/{table}` and `POST /ask`;
`/health` and `/ready` are the liveness and readiness probes.

### CLI

`uv run causa --help` lists every command. On the demo company:

```bash
uv run causa sim scenario          # the business events and data incidents that will be injected
uv run causa lineage trace RPT.V_EXEC_REVENUE_DAILY --column NET_REVENUE_EUR
uv run causa lineage impact RAW.SHOP_ORDERS --since 2026-10-06 --until 2026-10-12
uv run causa health scan --since 2026-05-25 --until 2026-10-17
uv run causa eval health           # score the health engine against ground truth
uv run causa explain net_revenue --current 2026-10-06..2026-10-12
uv run causa explain net_revenue --current 2026-10-01..2026-10-05     # a churned whale under a normal total
uv run causa metrics restatements net_revenue --since 2026-05-01 --until 2026-10-17 --dimension REGION
uv run causa verdict net_revenue --current 2026-10-01..2026-10-07    # data incidents + the real story
uv run causa eval verdict          # data impact (replayed) vs the true gap, for every event
uv run causa recall net_revenue --current 2026-10-06..2026-10-12 --out notices.md
uv run causa brief net_revenue --day 2026-10-08          # add --post with CAUSA_SLACK_WEBHOOK_URL set
uv run causa ask "Why did revenue drop last week? Is it real?"   # needs OPENAI_API_KEY or ANTHROPIC_API_KEY in .env
uv run causa mcp                   # MCP server (stdio) for Claude Desktop / Claude Code
uv run causa watch --once          # announce new serious incidents (Slack with CAUSA_SLACK_WEBHOOK_URL)
uv run causa eval bench --seeds 20 # score both engines on 20 randomly drawn companies
```

### MCP: Claude Desktop and Claude Code (no API key)

`causa mcp` serves the agent's tools over the [Model Context Protocol](https://modelcontextprotocol.io).
The client's own Claude does the investigating, on the user's Claude plan, and every figure still
comes from Causa's engines. The tools are the same definitions the agent uses (marked read-only),
the server tells the client how to investigate (start with `judge_change`, never compute figures,
keep data incidents and business change apart), and the warehouse connection opens on the first
tool call. Backend and credentials come from `.env`, as for every other command.

**Claude Code**: the repository's `.mcp.json` registers it; start `claude` in the repository and
approve the `causa` server. Then ask, for example, *"Use causa: why did net revenue move in the week
of 2026-10-06, and who saw wrong numbers?"*

**Claude Desktop**: Settings → Developer → Edit Config, add the server, and restart Claude Desktop:

```json
{
  "mcpServers": {
    "causa": {
      "command": "/absolute/path/to/uv",
      "args": ["--directory", "/absolute/path/to/causa", "run", "causa", "mcp"]
    }
  }
}
```

Use absolute paths (`which uv`): Claude Desktop does not start from your shell. Add
`"env": {"CAUSA_BACKEND": "duckdb"}` to use the local copy instead of Exasol. A DuckDB file can be
open in only one process, so stop `causa serve` while Claude uses it (Exasol has no such limit).

## Running on Exasol

### Against Exasol

Exasol SaaS (free trial at https://cloud.exasol.com/signup): create a database, add your IPv4 to
**Security → allow list**, and take the connection string, user name and a personal access token
from **Connect via tools**. Or run `make exasol-up` for a local docker-db (x86_64 hosts).

```bash
cp .env.example .env                # CAUSA_BACKEND=exasol, DSN host:8563, user, token as password
uv run causa sim run
uv run causa eval health && uv run causa eval verdict
uv run causa lineage trace MART.DAILY_REVENUE --column NET_REVENUE_EUR --query-log audit
```

`--query-log audit` reads Exasol's own `EXA_STATISTICS.EXA_DBA_AUDIT_SQL` (auditing is on by default
on SaaS; needs a DBA or `SELECT ANY DICTIONARY`): no instrumentation of the pipeline is needed.

Validated on an XSmall Exasol SaaS cluster (2025.1, eu-central-1), driven from a laptop in India:

| | DuckDB | Exasol SaaS |
|---|---|---|
| Warehouse after 168 days (order lines / true revenue / query log / snapshots) | 387,750 / €829,668,345.48 / 4,598 / 139,440 | identical (reported revenue within €0.84 of €818M: exact DECIMAL arithmetic) |
| Health engine: incident recall / business calm / alert precision | 100% / 100% / 100% | 100% / 100% / 100% (21 s) |
| Verdict with replay: windows within tolerance, worst error | 11/11, 0.8% | 11/11, 0.8% (3 min) |
| Lineage of `NET_REVENUE_EUR` from the simulated log vs Exasol's audit log | | identical columns and gating predicates |

The simulator itself takes ~35 min there (one ~0.2 s round trip per statement from India to
Frankfurt; small batches go through a prepared `INSERT`, large ones through `IMPORT`).

All SQL is authored in the **Exasol dialect**. The DuckDB backend exists only so the engines can be
developed and tested without a cluster; it transpiles statements with `sqlglot`.

### On your own Exasol

```bash
cp .env.example .env               # CAUSA_BACKEND=exasol, DSN, user, personal access token
uv run causa init                  # proposes causa.yaml: metrics, dimensions; checks the audit log
uv run causa serve --query-log audit
```

Causa needs a read-only user that can read the tables behind the metrics and Exasol's audit log
(`SELECT ANY DICTIONARY`; auditing is on by default on SaaS). It writes only to its own schemas:
`CAUSA` (metric snapshots, notifications) and `CAUSA_REPLAY` (scratch tables of a replay, dropped
after each run). Nothing in the pipeline has to change. The permissions are listed in
[docs/deployment.md](docs/deployment.md#database-permissions).

## Running it in production

Step-by-step instructions (local, Exasol SaaS, Docker, Kubernetes with Argo CD) are in
[docs/deployment.md](docs/deployment.md). In short:

The image `ghcr.io/rani700/causa` is built by CI on every push to `main` (see
`.github/workflows/`); it serves the web UI and API on port 8000 and bakes in the demo company as
the fallback copy.

- **Failover.** With `CAUSA_FALLBACK_DUCKDB_PATH` set, a lost Exasol connection (never an SQL error)
  switches to a local DuckDB copy, retried every two minutes; the UI says so. Lineage, health and
  cached answers are rebuilt on whichever backend is active.
- **Fresh data.** Every five minutes Causa checks the query log's newest statement; new activity
  reloads lineage and health and drops cached answers.
- **Fast on a remote warehouse.** Each incident is replayed once and reused by every week it touches;
  answers are cached per question; the server prepares every incident's investigation at start.
- **Cost per decision.** Every investigation reports the warehouse statements and database seconds
  it took (or that it came from cache), every agent answer its token cost in dollars.
- **Probes.** `GET /health` (process up) and `GET /ready` (which warehouse answers, warm-up state).
- **Concurrency.** One warehouse connection, used by one engine task at a time; the agent holds it
  only while a tool runs, never while the model thinks.

| Setting | Meaning |
|---|---|
| `CAUSA_BACKEND` | `exasol` or `duckdb` |
| `CAUSA_EXASOL_DSN`, `_USER`, `_PASSWORD`, `_ENCRYPTION` | Exasol connection (password: a personal access token on SaaS) |
| `CAUSA_FALLBACK_DUCKDB_PATH` | local copy to answer from while Exasol is away |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | the agent's model (Ask tab, `causa ask`) |
| `CAUSA_AGENT_PROVIDER`, `CAUSA_OPENAI_MODEL` | force a provider; pick an OpenAI model (default `gpt-5-nano`) |
| `CAUSA_SLACK_WEBHOOK_URL` | where alerts and briefs are posted |
| `CAUSA_WATCH_MINUTES` | how often the server looks for new serious incidents (0 = off) |
| `CAUSA_BRIEF_AT`, `CAUSA_BRIEF_METRIC` | daily brief time (`HH:MM`) and metric |

## How it works

### The simulator (`causa.simulator`)

A B2B reseller with four regional order feeds, FX conversion, a RAW → STG → MART → RPT pipeline, and
realistic consumers: Tableau/Looker dashboards, a Monday board pack, an ML churn model whose scores
are synced to Salesforce, a month-end finance close, a stale nightly extract nobody reads, and
ad-hoc analysts.

The simulator generates what *actually* happened (ground truth, `SIM.*`) separately from what
*landed* in the warehouse. Business events and data incidents are injected on top:

| Business (real) | Data (incidents) |
|---|---|
| Market launch, promotion, price change, regional slowdown, whale churn | Schema drift that silently drops a feed, duplicate load, wrong FX rate, null join keys, late arrival, failed job + manual re-run |

Every statement is written to `OPS.QUERY_LOG`, shaped like Exasol's `EXA_DBA_AUDIT_SQL`, with the
client strings real tools send. Causa never reads `SIM.*`; it is only used to score Causa.

### Engine 1: lineage (`causa.lineage`)

Built purely from the query log, with no instrumentation:

- statements are fingerprinted (vectorised in Polars), so each template is parsed once
- `sqlglot` resolves every projection to its source columns through CTEs, subqueries and views
- **gating columns** (JOIN / WHERE / GROUP BY) are tracked separately from derived columns, because
  that is how rows silently vanish (null join keys, a renamed timestamp column)
- actors (connectors, pipelines, dashboards, reports, reverse-ETL syncs, analysts) are identified
  from session client strings
- `impact()` computes the blast radius of a bad table over a time window

### Engine 2: data health (`causa.health`)

Zero-configuration monitoring that uses lineage to decide *what* matters:

- **discovery**: load timestamp, segment column (feed, currency...), event time, and the smallest
  key that is unique on almost every load day (so a duplicate load cannot hide its own key)
- **columns watched**: keys plus every column that gates rows downstream, directly or through
  columns derived from it; numeric columns only if downstream values are computed from them
- **one pushed-down query per table** returns daily volume, null counts, means, lateness and
  duplicate keys per segment; **row conservation** is checked along every pipeline edge
- **robust detection**: trailing-only median/MAD baselines, weekday-aware volume, Poisson and
  small-sample binomial floors, and a minimum practical effect per signal
- **query-log checks**: schema changes (attributed to the actor), failed writes and their re-runs,
  new category values (new market, new currency)
- **lineage-aware de-duplication**: an anomaly echoed downstream is folded into its upstream root;
  related roots are grouped into **cases** whose headline is the most telling incident
- **mix-aware severity**: a shifted mean over a mixed population (e.g. discounts during a promotion)
  is capped at medium; the same shift in a reference table (FX rates) is critical

On the default scenario and two unseen seeds, `causa eval health` gives 100% recall on data
incidents, 100% calm on business changes (none raised above medium) and 100% alert precision.
The simulator is the benchmark, so these numbers say the engine works on these incident types,
not that it will on every warehouse.

### Engine 3: why a metric moved (`causa.metrics`)

Metrics are declared in `causa.yaml` (table, time column, summed measure, optional volume such as
distinct orders, dimensions, high-cardinality "deep" dimensions such as customer).

- **recursive drill-down, pushed down**: one `GROUPING SETS` query per level returns current and
  baseline values for every element of every dimension; Causa drills into each driver with a new
  query (region -> country -> customer). A whole explanation is typically 3-8 queries.
- **choosing the dimension** (after Adtributor): elements must carry at least 10% of the change and
  be *over-represented* (lift >= 1.3 vs their baseline share), so big segments do not "explain" a
  change merely by being big; dimensions are ranked by excess explanation, so a single customer
  that fully explains a change beats its country and region
- **volume vs rate**: each driver's change is split exactly (two-player Shapley value) into a
  volume effect and a value-per-unit effect. Lost rows are pure volume; a wrong FX rate is pure rate.
- **uniform vs concentrated**: a broad segment whose every part moved by the same percentage is
  flagged as uniform (how lost data looks); a vanished segment is reported as "stopped entirely"
- **significance**: the change is compared with the same comparison over the previous ten windows
  (robust z), and the default baseline covers the same weekdays a week earlier
- **snapshots and restatements**: Causa records what every metric *showed* each night, so values
  that changed after they were reported (late batches, failed loads that were re-run) stay visible
  even after the warehouse has silently healed

### Engine 4: real change or broken data? (`causa.verdict`)

`causa verdict` splits a metric's change into data incidents and real business change:

- **relevance and linkage**: a health case counts if lineage says its table feeds the metric. Its
  affected rows are found through a *bridge*: a table on the lineage path that carries the affected
  segment (feed, currency) and shares a key with the metric table. `CURRENCY=JPY` reaches the fact
  table through `STG.ORDERS`.
- **days**: health incidents are dated by load day, metrics by business date; the typical lag of
  each table (measured, not configured) converts one into the other. Incidents in the *baseline*
  count too: they distort what the present is compared with.
- **impact, by mechanism**, each method stated in the output:
  - schema drift, lost feeds: difference-in-differences against the rest of the metric, with a 95%
    band from historical volatility and an alignment z-score
  - wrong reference values (FX): the observed factor applied to the affected rows
  - duplicated keys: observed = true / (1 - duplicate share)
  - partial NULLs in a gating column: row loss, assuming dropped rows were average
  - late batches that arrived, failed loads that were re-run: *healed*, no impact on today's data
    (the restatements show what people saw at the time)
- **evidence in log-odds**, every item printed: severity, integrity signals vs shape-only signals,
  a new category value (business-like), alignment, restatements. A finding counts as data at 60%.
- **no double counting**: findings about the same rows on the same days are merged
- **business remainder**: judged against normal variation and explained again by attribution on
  the days no incident touched (that is where a churned customer surfaces under a broken feed)

`causa eval verdict` compares the data impact with the true gap (reported change minus true change)
for the week starting at every event. With estimates alone: 30 of 33 windows within 10% or inside
the 95% band (the misses: partial-NULL row loss, overestimated ~60% because the dropped rows were
smaller than average). With engine 5's replay: **33 of 33, worst error 0.8%**, and no business
event ever claimed as data.

### Engine 5: counterfactual replay and recall (`causa.replay`, `causa.recall`)

What would the metric have shown without the incident? Causa re-runs the company's **own pipeline**:

1. a **repair** is inferred from the health evidence, never configured:
   - duplicated keys: keep the first load of each inferred key
   - a column that went NULL right after an `ADD COLUMN`: verified rename, `COALESCE(old, new)`
   - NULL join keys: kept under an `(unknown)` member of the dimension they join (found from the
     parsed `ON` conditions), so the value survives and only the category is unknown
   - a wrong reference value: interpolated between the last good value and the next
2. the exact INSERT/MERGE statements the query log recorded on the affected load days, for every
   table between the damage and the metric, are re-run in the `CAUSA_REPLAY` schema with table names
   rewritten by sqlglot: once on the original data (**control**), once on the repaired data
3. the control run must reproduce every logged row count (proof the replay is faithful); the metric
   on repaired minus control is the incident's exact effect

On the simulator the replay matches the true misstatement to the euro for duplicates, NULL keys and
the renamed column, and within 0.2-0.8% for the interpolated FX rate.

`causa recall` then lists everyone exposed to the wrong numbers: exposure window (until the data
healed, or *still open* if the warehouse holds the damage), misstatement per day, and every
dashboard, scheduled report (editions, recipients), ML pipeline, reverse-ETL sync, spreadsheet and
analyst that read affected data, plus the pipeline that carried the incident and the load days to
re-run. Each gets a drafted correction notice; `--out notices.md` writes them to a file.

### The web UI (`causa serve`, then http://127.0.0.1:8000)

A dependency-free single page (`src/causa/web/`, plain HTML/CSS/JS, no build step, no CDN) over the API:

- **Overview**: the metric's daily history with every data incident shaded; pick a week and Causa splits its
  change into *broken data* and *real business change* (with a waterfall and one forwardable sentence), shows each
  incident's cause, replayed impact, confidence and evidence, the real business drivers on clean days, and **who
  saw wrong numbers**, with a ready-to-send correction notice per report, dashboard and sync.
- **Incidents**: every health case on a timeline, with its evidence, the tables it touched, its week, and its
  blast radius.
- **Lineage**: connectors → RAW → STG → MART → reports → consumers, reconstructed from the query log; select
  anything to trace it up and down, see the columns that gate its rows and its schema changes, and light up
  everything a broken table would hit.
- **Ask**: the investigator agent, with every tool call it made shown under its answer (needs `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` in `.env`).
- **Take the tour** (or open `/?tour=1`): six stops through the story, starting at the latest critical
  incident, so it works on any data.

**Speed on a remote warehouse.** Every query is a round trip (about 0.3 s from India to Frankfurt),
so Causa avoids repeating them. A replay depends on the incident, not on the week being judged, so
each incident is replayed once and every week that touches it reuses the result. Verdicts are
remembered per question, so judge, recall and the web UI share one run, and reopening a week is
instant. Measured on Exasol SaaS: first look at a week with a new incident about 17 s, a week whose
incidents were already replayed about 4.5 s, a week seen before 0 s (it was 18 to 34 s).

### The investigator agent, the brief and the API

`causa ask` puts a language model in front of the engines: Claude (`claude-opus-5-5`, high effort)
or OpenAI (`gpt-5-nano` by default, the cheapest OpenAI model with function calling). The model plans
the investigation; **every figure comes from a tool**:

| Tool | Engine |
|---|---|
| `judge_change` | verdict: data incidents (replayed) vs business change, evidence, restatements |
| `explain_metric` | attribution drill-down with volume/rate split and significance |
| `health_cases` | data health cases in a date range |
| `recall_consumers` | exposure, misstatement per day, consumers and draft notices |
| `lineage` | upstream/downstream, loaders, row-gating columns, schema changes |
| `run_sql` | one read-only SELECT (parsed by sqlglot; rows capped; ground-truth schema refused) |

Tools are strict JSON schemas; system prompt and tools are prompt-cached; the conversation is
append-only; refusals fall back server-side (`fallbacks: "default"`); every tool call is recorded and
shown, so each answer has an audit trail. The loop is tested against a scripted client.

**Choosing the model.** Put `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` in `.env`. With both, Claude is
used unless `CAUSA_AGENT_PROVIDER=openai`; `CAUSA_OPENAI_MODEL` picks another OpenAI model. Both
agents share the toolbox, the prompt and the audit trail; the OpenAI one runs on the Responses API
(function calling with the same strict schemas, `store=False`, reasoning passed back encrypted).
Measured on Exasol SaaS with `gpt-5-nano`: a "why did revenue drop, is it real, who saw it?"
question takes 3 tool calls and about 13k input / 8k output tokens, under half a US cent.

`causa brief` is the morning message. It is **deterministic** (no language model): last 7 days vs the
same weekdays before, the data/business split, each incident with its replayed impact and where it
was seen, the real business movement on unaffected days, and new incidents since yesterday. It renders
as text or Slack Block Kit and posts only with `--post` and a configured `CAUSA_SLACK_WEBHOOK_URL`.

`causa serve` exposes the same through FastAPI: `/metrics`, `/metrics/{m}/explain|verdict|recall|brief`,
`/health/cases`, `/lineage/{table}` and `POST /ask` (multi-turn by `conversation_id`).

## Layout

```
src/causa/
  warehouse/     Exasol (pyexasol) and DuckDB backends, failover between them, metering
  sources/       query-log sources (Exasol audit log, table) and the catalog
  lineage/       statement parser, actor identification, lineage graph
  health/        monitor discovery, profiling SQL, detectors, cases
  metrics/       metric definitions, attribution, narrative, snapshots and restatements
  verdict/       linkage, impact estimators, evidence and verdicts
  replay/        repair inference and counterfactual replay of the pipeline
  recall.py      exposure, consumers and correction notices
  agent/         investigator agent (Claude or OpenAI tool-use loop), its tools, pricing
  brief.py       deterministic morning brief (text and Slack Block Kit); slack.py posts it
  api.py         FastAPI service; workspace.py caches what the engines share
  mcp_server.py  the agent's tools over MCP (stdio), for Claude Desktop and Claude Code
  watch.py       new-incident alerts and the daily brief, recorded in CAUSA.NOTIFICATIONS
  onboarding.py  causa init: metric definitions proposed from the catalog
  benchmark.py   randomly drawn companies, scored in parallel (causa eval bench)
  store.py       Causa's own tables (CAUSA schema)
  evaluation.py  scoring against the simulator's ground truth
  simulator/     synthetic company, scenario, incidents, workloads
  cli.py
tests/
docs/            problem statement, deployment guide, benchmark, project write-up, demo script
docker/          docker-compose for a local Exasol (x86_64)
Dockerfile       the production image (built by CI only)
LICENSE          MIT
```

## Licence

MIT, see [LICENSE](LICENSE). Copyright (c) 2026 Shweta Rani.
