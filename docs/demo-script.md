# Demo script (about 4 minutes)

Open https://causa.codeshare.co.in before you start (the first load prepares the data). Everything
below also works on a laptop with `uv run causa serve`.

## 0:00 The problem (30 s)

> Monday morning, the revenue dashboard says **down 34% week on week**. Is the business in trouble,
> or did the data break? Today a data analyst spends a day or two finding out, nobody can say how
> much of the drop the bug explains, and by then the board pack has already gone out with the wrong
> number. Causa answers all three questions in seconds, on Exasol.

## 0:30 Overview: real or broken (60 s)

Click **Take the tour** (top right), or open `/?tour=1`.

1. **The chart.** Daily net revenue for half a year; the red and amber bands are data incidents.
   *Causa found these on its own: it only reads the warehouse and Exasol's query log.*
2. **The split.** For 6 to 12 October: reported **-€13.68M**, of which **-€11.69M is broken data**
   and **-€1.99M is real**, within normal variation. *So the business is fine; the data is not.*
3. **What broke.** An upstream schema change on `RAW.SHOP_ORDERS` by the EMEA Fivetran connector left
   `ORDER_TS` empty, so six days of EMEA orders never reached the fact table. Open **How the impact
   was measured**: *Causa repaired the data and re-ran the company's own pipeline SQL; the control
   run reproduced every logged row count. This is a measurement, not a guess.* Confidence 95%, with
   the evidence listed.

## 1:30 Who saw wrong numbers (40 s)

4. The **Weekly Board Pack** (6 recipients), the Executive Revenue and Regional dashboards, the churn
   model and its **Salesforce sync**, three analysts. Click **Notice** on the board pack: a
   correction notice, ready to send. *This is the part nobody does today.*

## 2:10 Blast radius (30 s)

5. **Lineage**: the pipeline rebuilt from the query log, connectors to consumers. The highlighted
   path is everything the broken table reaches: 11 tables, 9 consumers. Click any table to trace it.

## 2:40 Ask (40 s)

6. **Ask**: *"Why did net revenue change in the week of 6 October? Is it real, and who saw wrong
   numbers?"* The answer shows every tool call under it, and what it cost: about **$0.004** on
   `gpt-5-nano`, plus the warehouse statements its tools ran.

## 3:20 Why believe it, why Exasol (40 s)

- **Benchmark**: 20 randomly drawn companies, 220 events: every data incident alerted, **218 of 220**
  verdicts within 10% of the true gap ([docs/benchmark.md](benchmark.md)).
- **Exasol**: lineage comes free from `EXA_DBA_AUDIT_SQL`; replays run as real SQL inside Exasol;
  every explanation is a handful of pushed-down `GROUPING SETS` queries. Validated end to end on an
  XSmall SaaS cluster.
- **Production**: Docker image built by CI, failover to a local copy when the cluster sleeps (the
  pill in the header says so), live refresh, alerts recorded in `CAUSA.NOTIFICATIONS`, MCP for
  Claude Desktop, `causa init` for a new warehouse.

## Backup answers

- *Does it need the simulator?* No: `causa init` proposes the metric definitions for any Exasol
  warehouse and `--query-log audit` reads Exasol's own audit log. The simulator is the benchmark.
- *What if the header says "Built-in copy"?* The SaaS trial cluster stops itself when idle; Causa
  answers from a copy of the same data and switches back when Exasol returns.
- *Which LLM?* Claude or OpenAI. The engines produce every number; the model only plans and writes.
