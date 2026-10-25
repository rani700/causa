# Causa: is the number real, or did the data break?

**Theme: The Work Behind the Work.** Live: https://causa.codeshare.co.in · Code:
https://github.com/rani700/causa · Tour: https://causa.codeshare.co.in/?tour=1

## The problem

When a business metric moves, two very different things can be behind it: the business changed, or
the data broke (a feed stalled after a schema change, a load ran twice, a currency rate was wrong).
Telling them apart is invisible, slow work: an analyst opens tables, compares row counts, reads
pipeline logs and asks engineers, often for a day or more. Even then three questions stay open:

1. How much of the change does the incident explain, in euros?
2. What is the real business change once the incident is removed?
3. Who already used the wrong number: which dashboards, board packs, models and syncs?

Data observability tools stop at "this table looks wrong". BI explain features assume the data is
right. Nobody joins the two, so the wrong number reaches the board.

## What Causa does

For any metric and week, Causa splits the change into **broken data** and **real business change**,
names each incident's cause with a confidence and the evidence for it, **measures** its exact impact
by re-running the company's own pipeline on repaired data, explains the real remainder by segment,
and lists **everyone who consumed the wrong numbers** with a correction notice for each.

It needs read access to the warehouse and its query log, nothing else: no instrumentation, no
pipeline changes, no hand-drawn lineage.

## How it works (five engines)

1. **Lineage from the query log.** Every statement in Exasol's `EXA_DBA_AUDIT_SQL` is parsed with
   sqlglot into column-level lineage, including the columns that decide which rows survive a join or
   filter, and the tools behind each session (Fivetran, Airflow, Tableau, Power BI, Hightouch, Excel).
2. **Data health.** Zero-configuration monitors chosen by lineage, one pushed-down query per table;
   robust baselines; schema changes, failed writes and re-runs from the log; anomalies echoed
   downstream are folded into one case.
3. **Attribution.** Recursive drill-down with `GROUPING SETS`, a Shapley split into volume and rate
   effects, and significance against comparable past weeks.
4. **Verdict.** Links each case to the metric through lineage, estimates its impact by mechanism,
   weighs evidence in log-odds, and explains the business remainder on unaffected days.
5. **Counterfactual replay and recall.** Infers a repair from the evidence (dedupe, coalesce a
   renamed column, quarantine null keys, interpolate a bad rate), re-runs the logged INSERT/MERGE
   statements inside Exasol on original and repaired data, and requires the control run to reproduce
   every logged row count. Then it finds every reader of the affected data in the exposure window.

On top: a web UI with a guided tour, an HTTP API, a CLI, an investigator agent (Claude or OpenAI;
every figure comes from a tool), an MCP server for Claude Desktop and Claude Code, a deterministic
morning brief, and a watcher that announces each new serious incident once.

## Why Exasol

- **Lineage for free.** Exasol's audit log records every statement with its session and client, so
  Causa rebuilds the pipeline map with no setup. On our SaaS cluster the audit-log lineage matches
  the simulated log table for table once Causa's own and Exasol's service sessions are left out.
- **Replay as real SQL.** Measuring instead of estimating means re-running parts of the pipeline per
  incident; Exasol runs those statements where the data lives.
- **Pushed-down analytics.** An explanation is 3 to 8 `GROUPING SETS` queries; a health scan is one
  query per table. Causa never copies raw data out.

## Results

**Benchmark on 20 randomly drawn companies** (each a half-year with its own incidents and business
changes, scored against the simulator's ground truth, `causa eval bench`):

| | |
|---|---|
| Data incidents raised as alerts | 100% (120 / 120) |
| Business changes left calm | 97% |
| Alerts matching a real incident | 93% |
| Verdicts within 10% of the change | 218 / 220, median error 0.0% |

Replayed incidents match the true misstatement to the euro; estimated ones (FX rates) within 1.5%.
The two misses and every false alarm are listed in [benchmark.md](benchmark.md). In one, a big
promotion was partly read as a data problem; in the other, a late batch showed up as two cases, and
the one about missing order lines was not recognised as healed once the batch arrived.

**On Exasol SaaS** (XSmall, eu-central-1, driven from India): identical results to the local engine
on the 168-day company; a week's first investigation about 15 s (dominated by the replay's ~30
statements), a week sharing an already replayed incident about 4 s, a week seen before instant; all
16 incident investigations prepared in the background in under 3 minutes at start-up.

**Cost per decision**: each investigation reports its warehouse statements and database seconds;
an agent answer on `gpt-5-nano` costs about $0.004.

## Production readiness

Container image built by CI on every push, deployed with GitOps; liveness and readiness probes;
failover to a local copy while the warehouse is unreachable, with the UI saying so; refresh when the
query log shows new activity; one warehouse connection shared safely, never held while a model
thinks; alerts and briefs recorded in `CAUSA.NOTIFICATIONS`; `causa init` proposes metric
definitions for a new warehouse; 100+ tests, ruff and mypy clean.

## What's next

Column-level blast radius in the UI, ratio metrics (conversion, average order value), delivering
notices directly (email, Slack) and tracking acknowledgements, and backends for Snowflake and
BigQuery, whose query histories carry the same information.
