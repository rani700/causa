# Problem statement

## Who has the problem

Anyone who reports a business metric and has to stand behind it:

- **Analysts** who are asked "why did revenue drop last week?" on a Monday morning.
- **Data engineers** who own the pipelines and get pulled in whenever a number looks odd.
- **Finance and operations leaders** who put the number in a board pack, a forecast or a month-end
  close, and make decisions on it.

## What happens today

When a metric moves, two very different things can be behind it: the business changed, or the data
broke. A feed stalled after a schema change, a load ran twice, a currency rate was wrong, a batch
arrived late.

Telling them apart is slow, manual work. An analyst opens tables, compares row counts, reads
pipeline logs and asks other teams, often for a day or more. The tools in place each answer half of
the question:

- **Data observability tools** stop at "this table looks wrong". They cannot say what that means for
  revenue.
- **BI explain features** break a change down by segment, but assume the data is right.

Nobody joins the two, so even after the checking, three questions usually stay open.

## Why it matters

By the time someone finds the broken load, the wrong number has already travelled. It was on the
revenue dashboard, in the Monday board pack, in a forecast, in the scores an ML model synced to
Salesforce. Decisions were made on broken data, and each such episode makes people trust the
numbers a little less. Fixing the table does not fix any of that: someone has to work out how wrong
each number was and tell everyone who used it.

## What Causa answers

For any metric and period, Causa answers the three open questions directly:

1. **How much of the change is broken data, in euros?** It finds the data incidents behind the
   metric and measures each one's exact effect by re-running the company's own logged pipeline SQL
   on repaired data inside Exasol.
2. **What is the real business change?** What remains once the incidents are removed, judged
   against normal variation and explained by segment (region, country, customer).
3. **Who already used the wrong numbers?** Every dashboard, scheduled report, ML model, reverse-ETL
   sync and analyst that read affected data in the exposure window, each with a drafted correction
   notice.

It needs only read access to the warehouse and its query log: no instrumentation, no pipeline
changes, no hand-drawn lineage. On a benchmark of 20 randomly drawn simulated companies it raised
100% of data incidents as alerts and left 97% of business changes calm, and 218 of 220 verdicts put
the data impact within 10% of the change ([benchmark](benchmark.md)).

Live demo: https://causa.codeshare.co.in (guided tour: https://causa.codeshare.co.in/?tour=1).
