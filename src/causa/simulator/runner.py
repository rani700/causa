"""Runs a scenario end to end against a warehouse.

Per simulated day ``d``:
  * night of d+1, 00:30-02:00: connectors land FX rates and the order feeds (RAW)
  * night of d+1, 02:30: the ``sales_mart`` DAG builds STG -> MART for logical date ``d``
  * calendar day d+1: dashboards, reports, ML and analysts read the warehouse
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import polars as pl

from causa.simulator import sql
from causa.simulator.consumers import Client, Workloads
from causa.simulator.generator import FEED_LOAD_OFFSET_MIN, CompanyGenerator
from causa.simulator.recorder import QueryRecorder, RecordingSession
from causa.simulator.scenario import JobFailure, Scenario, SchemaDriftStall
from causa.warehouse import Warehouse

ETL = Client("Airflow 2.10; dag=sales_mart", "ETL_SVC")
ETL_MANUAL = Client("Airflow 2.10; dag=sales_mart; trigger=manual", "ETL_SVC")
DEPLOY = Client("dbt 1.9; job=deploy", "DEPLOY_SVC")
FEEDS = tuple(FEED_LOAD_OFFSET_MIN)
SIMULATED_FAILURE = (
    "[Code: 0, SQL State: 40001] GlobalTransactionRollback msg: Transaction collision: automatic transaction rollback."
)


def connector(name: str) -> Client:
    return Client(f"Fivetran; connector={name}", "FIVETRAN_SVC")


@dataclass
class SimulationResult:
    scenario: Scenario
    days: int = 0
    true_net_revenue: float = 0.0
    reported_net_revenue: float = 0.0
    query_log_rows: int = 0
    pipeline_runs: int = 0
    failed_tasks: list[str] = field(default_factory=list)


class _Pipeline:
    def __init__(self, wh: Warehouse, gen: CompanyGenerator, recorder: QueryRecorder) -> None:
        self.wh = wh
        self.gen = gen
        self.rec = recorder
        self.runs: list[dict[str, object]] = []
        self.pending_orders = pl.DataFrame()
        self.pending_lines = pl.DataFrame()
        self.pending_fx = pl.DataFrame()

    def _run_record(
        self,
        dag: str,
        task: str,
        logical: date,
        target: str,
        started: datetime,
        finished: datetime,
        state: str,
        rows: int | None,
        error: str | None = None,
    ) -> None:
        attempt = sum(
            1 for r in self.runs if r["dag_id"] == dag and r["task_id"] == task and r["logical_date"] == logical
        )
        self.runs.append(
            {
                "run_id": f"{dag}__{task}__{logical:%Y%m%d}__{attempt + 1}",
                "dag_id": dag,
                "task_id": task,
                "logical_date": logical,
                "target_table": target,
                "started_at": started,
                "finished_at": finished,
                "state": state,
                "rows_affected": rows,
                "error_message": error,
            }
        )

    def setup(self, scenario: Scenario) -> None:
        deployed = datetime.combine(scenario.start - timedelta(days=1), time(12))
        for schema in sql.SCHEMAS:
            self.wh.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            self.wh.execute(f"CREATE SCHEMA {schema}")
        deploy = self.rec.session(self.wh, DEPLOY.client, DEPLOY.user)
        for i, stmt in enumerate((*sql.DDL, *sql.VIEWS)):
            if " SIM." in stmt:
                self.wh.execute(stmt)  # evaluation tables are not part of the company's warehouse
            else:
                deploy.execute(stmt, deployed + timedelta(seconds=i))

        customers = self.gen.customers.drop("weight").with_columns(updated_at=pl.lit(deployed))
        products = self.gen.products.drop("popularity").with_columns(updated_at=pl.lit(deployed))
        for name, table, df in (
            ("salesforce_crm", "RAW.CRM_CUSTOMERS", customers),
            ("sap_erp", "RAW.ERP_PRODUCTS", products),
        ):
            self._load(self.rec.session(self.wh, *_c(connector(name))), table, df, deployed, f"{name}_full.csv")

    def _load(self, session: RecordingSession, table: str, df: pl.DataFrame, at: datetime, file_name: str) -> int:
        rows = self.wh.load(df, table)
        session.log(sql.import_stmt(table, df.columns, file_name), at, duration=0.05 + rows / 200_000, row_count=rows)
        return rows

    def ingest(self, night: date) -> None:
        lo = datetime.combine(night, time())
        hi = lo + timedelta(days=1)
        stamp = f"{night:%Y%m%d}"

        for drift in self.gen.s.of(SchemaDriftStall):
            if drift.start + timedelta(days=1) == night:
                session = self.rec.session(self.wh, *_c(connector(drift.feed)))
                session.execute(sql.SCHEMA_DRIFT_DDL, lo + timedelta(minutes=55))

        due_fx, self.pending_fx = _split_due(self.pending_fx, hi)
        if due_fx.height:
            at = lo + timedelta(minutes=30)
            session = self.rec.session(self.wh, *_c(connector("ecb_fx_rates")))
            rows = self._load(session, "RAW.FX_RATES", due_fx, at, f"fx_rates_{stamp}.csv")
            self._run_record("fivetran", "fx_rates", night - timedelta(days=1), "RAW.FX_RATES", at, at, "success", rows)

        due_orders, self.pending_orders = _split_due(self.pending_orders, hi)
        due_lines, self.pending_lines = _split_due(self.pending_lines, hi)
        for feed in FEEDS:
            at = lo + timedelta(hours=1, minutes=FEED_LOAD_OFFSET_MIN[feed])
            session = self.rec.session(self.wh, *_c(connector(feed)))
            orders = due_orders.filter(pl.col("source_feed") == feed)
            lines = due_lines.filter(pl.col("source_feed") == feed)
            n_orders = self._load(
                session, "RAW.SHOP_ORDERS", _drop_all_null(orders, "order_ts_utc"), at, f"{feed}_orders_{stamp}.csv"
            )
            n_lines = self._load(
                session, "RAW.SHOP_ORDER_LINES", lines, at + timedelta(minutes=2), f"{feed}_lines_{stamp}.csv"
            )
            # Connectors report success whatever the content looks like; that is the point.
            self._run_record(
                "fivetran",
                feed,
                night - timedelta(days=1),
                "RAW.SHOP_ORDERS",
                at,
                at + timedelta(minutes=3),
                "success",
                n_orders + n_lines,
            )

    def transform(self, logical: date, failures: dict[date, JobFailure]) -> None:
        lo = datetime.combine(logical + timedelta(days=1), time())
        hi = lo + timedelta(days=1)
        at = lo + timedelta(hours=2, minutes=30)
        session = self.rec.session(self.wh, ETL.client, ETL.user)
        tasks = (
            ("stg_orders", "STG.ORDERS", sql.stg_orders(lo, hi)),
            ("stg_order_lines", "STG.ORDER_LINES", sql.stg_order_lines(lo, hi)),
            ("fct_order_lines", "MART.FCT_ORDER_LINES", sql.fct_order_lines(lo, hi)),
        )
        for i, (task, target, stmt) in enumerate(tasks):
            start = at + timedelta(minutes=4 * i)
            failure = failures.get(logical)
            if failure and failure.task_id == task:
                session.log(stmt, start, duration=41.7, row_count=None, error=SIMULATED_FAILURE)
                self._run_record(
                    "sales_mart",
                    task,
                    logical,
                    target,
                    start,
                    start + timedelta(seconds=42),
                    "failed",
                    None,
                    SIMULATED_FAILURE,
                )
                continue
            rows = session.execute(stmt, start)
            self._run_record("sales_mart", task, logical, target, start, start + timedelta(minutes=1), "success", rows)
        self.rebuild_daily_revenue(session, logical, at + timedelta(minutes=15))

    def rebuild_daily_revenue(self, session: RecordingSession, logical: date, at: datetime) -> None:
        rows = 0
        for i, stmt in enumerate(sql.DAILY_REVENUE_REBUILD):
            rows = session.execute(stmt, at + timedelta(seconds=5 * i))
        self._run_record(
            "sales_mart", "daily_revenue", logical, "MART.DAILY_REVENUE", at, at + timedelta(minutes=1), "success", rows
        )

    def recover(self, failure: JobFailure, at: datetime) -> None:
        lo = datetime.combine(failure.start + timedelta(days=1), time())
        session = self.rec.session(self.wh, ETL_MANUAL.client, ETL_MANUAL.user)
        rows = session.execute(sql.fct_order_lines(lo, lo + timedelta(days=1)), at)
        self._run_record(
            "sales_mart",
            failure.task_id,
            failure.start,
            "MART.FCT_ORDER_LINES",
            at,
            at + timedelta(minutes=1),
            "success",
            rows,
        )
        self.rebuild_daily_revenue(session, failure.start, at + timedelta(minutes=2))


def run_simulation(
    scenario: Scenario,
    wh: Warehouse,
    on_day: Callable[[date], None] | None = None,
    on_loaded: Callable[[date], None] | None = None,
) -> SimulationResult:
    """``on_loaded(day)`` runs right after the nightly ETL for ``day`` (before anyone reads the data);
    ``on_day(day)`` runs once the following business day is over."""
    gen = CompanyGenerator(scenario)
    rec = QueryRecorder()
    pipe = _Pipeline(wh, gen, rec)
    workloads = Workloads(rec, wh, scenario.seed)
    failures = {f.start: f for f in scenario.of(JobFailure)}
    recoveries = {f.recovered_on: f for f in scenario.of(JobFailure) if f.recovered_on}
    truths: list[pl.DataFrame] = []

    pipe.setup(scenario)
    for day in scenario.dates():
        true_day = gen.generate(day)
        truths.append(true_day.revenue)
        landing = gen.land(true_day)
        pipe.pending_orders = pl.concat([pipe.pending_orders, landing.orders], how="diagonal_relaxed")
        pipe.pending_lines = pl.concat([pipe.pending_lines, landing.lines], how="diagonal_relaxed")
        pipe.pending_fx = pl.concat([pipe.pending_fx, landing.fx], how="diagonal_relaxed")

        night = day + timedelta(days=1)
        pipe.ingest(night)
        pipe.transform(day, failures)
        if on_loaded:
            on_loaded(day)

        jobs = workloads.day_jobs(night)
        if night in recoveries:
            failure = recoveries[night]
            jobs.append((datetime.combine(night, time(10, 12)), lambda t, f=failure: pipe.recover(f, t)))
        for at, job in sorted(jobs, key=lambda j: j[0]):
            job(at)
        if on_day:
            on_day(day)

    query_log = rec.to_frame()
    wh.load(query_log, "OPS.QUERY_LOG")
    runs = pl.DataFrame(pipe.runs, infer_schema_length=None)
    wh.load(runs, "OPS.PIPELINE_RUNS")
    wh.load(_events_frame(scenario), "SIM.EVENTS")
    truth = pl.concat(truths)
    wh.load(truth, "SIM.TRUE_DAILY_REVENUE")

    reported = wh.scalar("SELECT SUM(NET_REVENUE_EUR) FROM MART.DAILY_REVENUE")
    return SimulationResult(
        scenario=scenario,
        days=scenario.days,
        true_net_revenue=float(truth["net_revenue_eur"].sum()),
        reported_net_revenue=float(reported or 0.0),  # type: ignore[arg-type]
        query_log_rows=query_log.height,
        pipeline_runs=runs.height,
        failed_tasks=[str(r["run_id"]) for r in pipe.runs if r["state"] == "failed"],
    )


def _c(client: Client) -> tuple[str, str]:
    return client.client, client.user


def _split_due(df: pl.DataFrame, hi: datetime) -> tuple[pl.DataFrame, pl.DataFrame]:
    if df.is_empty():
        return df, df
    due = pl.col("loaded_at") < hi
    return df.filter(due), df.filter(~due)


def _drop_all_null(df: pl.DataFrame, column: str) -> pl.DataFrame:
    """Rows landed before the upstream schema change must not reference the new column."""
    if column in df.columns and df[column].null_count() == df.height:
        return df.drop(column)
    return df


def _events_frame(scenario: Scenario) -> pl.DataFrame:
    rows = []
    for ev in scenario.events:
        params = ev.model_dump(mode="json", exclude={"event_id", "kind", "nature", "start", "end", "description"})
        rows.append(
            {
                "event_id": ev.event_id,
                "kind": ev.kind,
                "nature": ev.nature.value,
                "start_date": ev.start,
                "end_date": ev.end,
                "description": ev.description,
                "params_json": json.dumps(params),
            }
        )
    return pl.DataFrame(rows, schema_overrides={"end_date": pl.Date})
