# Causa

**A root-cause engine for business metrics, built on Exasol.**

When a KPI moves, Causa answers the three questions that usually take a data team days:

1. **Why did it move?** Which segments drove the change.
2. **Is it real, or did the data break?** It separates genuine business change from data incidents, with evidence.
3. **Who consumed the wrong number?** Every dashboard, report, ML model and sync that used affected data.

Data observability tools flag broken tables but can't say what that means for revenue. BI "explain"
features break revenue down but assume the data is correct. Causa joins the two.

## Status

| Engine | State |
|---|---|
| Synthetic company simulator (benchmark with ground truth) | ✅ |
| 1. Lineage from the query log (column-level + row-gating predicates + blast radius) | ✅ |
| 2. Data health detectors, lineage-focused, grouped into cases | ✅ |
| 3. Metric attribution (Shapley / Adtributor-style decomposition, pushed down to Exasol) | ⏳ next |
| 4. Business-vs-data classifier | ⏳ |
| 5. Counterfactual replay + recall | ⏳ |
| Investigator agent, API, Slack, UI | ⏳ |

## Quickstart

```bash
uv sync
uv run causa sim scenario          # the business events and data incidents that will be injected
uv run causa sim run               # build 168 days of a simulated company's warehouse (~70s on DuckDB)
uv run causa lineage trace RPT.V_EXEC_REVENUE_DAILY --column NET_REVENUE_EUR
uv run causa lineage impact RAW.SHOP_ORDERS --since 2026-10-06 --until 2026-10-12
uv run causa health scan --since 2026-05-25 --until 2026-10-17
uv run causa eval health           # score the health engine against ground truth
make test
```

### Running against Exasol

```bash
make exasol-up                      # docker-db (x86_64; emulated on Apple Silicon)
cp .env.example .env                # set CAUSA_BACKEND=exasol
uv run causa sim run
uv run causa lineage impact RAW.SHOP_ORDERS --query-log audit   # use Exasol's own audit log
```

All SQL is authored in the **Exasol dialect**. The DuckDB backend exists only so the engines can be
developed and tested without a cluster; it transpiles statements with `sqlglot`.

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

## Layout

```
src/causa/
  warehouse/     Exasol (pyexasol) and DuckDB backends behind one interface
  sources/       query-log sources (Exasol audit log, table) and the catalog
  lineage/       statement parser, actor identification, lineage graph
  health/        monitor discovery, profiling SQL, detectors, cases
  evaluation.py  scoring against the simulator's ground truth
  simulator/     synthetic company, scenario, incidents, workloads
  cli.py
tests/
docker/          docker-compose for a local Exasol
```
