"""Downstream workloads of the simulated company.

These produce the reads that Causa later turns into lineage, blast radius and cost-per-decision.
Each tool identifies itself through the session client string (``<tool>; <key>=<value>; ...``),
the same way BI tools and orchestrators do on a real Exasol cluster.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import partial

import numpy as np

from causa.simulator.recorder import QueryRecorder
from causa.simulator.sql import dt
from causa.simulator.world import REGIONS
from causa.warehouse import Warehouse

Job = Callable[[datetime], None]


@dataclass(frozen=True)
class Client:
    client: str
    user: str


EXEC_DASHBOARD = Client("Tableau Server 2025.1; workbook=Executive Revenue", "BI_SVC")
REGION_DASHBOARD = Client("Tableau Server 2025.1; workbook=Regional Performance", "BI_SVC")
MIX_DASHBOARD = Client("Looker 25.6; dashboard=Product Mix", "LOOKER_SVC")
LEGACY_EXTRACT = Client("Tableau Server 2025.1; workbook=Legacy Ops KPIs; extract=daily", "BI_SVC")
BOARD_PACK = Client("Power BI Report Server; report=Weekly Board Pack; recipients=6", "REPORTING_SVC")
CHURN_DAG = Client("Airflow 2.10; dag=churn_forecast", "ML_SVC")
REVERSE_ETL = Client("Hightouch; sync=churn_scores_to_salesforce", "HIGHTOUCH_SVC")
FINANCE_DAG = Client("Airflow 2.10; dag=finance_close", "FIN_SVC")
CLOSE_WORKBOOK = Client("Microsoft Excel 16; workbook=Month-End Close.xlsx", "CONTROLLER_ANNA")
ANALYSTS = ("ANALYST_PRIYA", "ANALYST_JONAS", "ANALYST_MEI")


class Workloads:
    def __init__(self, recorder: QueryRecorder, wh: Warehouse, seed: int) -> None:
        self._rec = recorder
        self._wh = wh
        self._seed = seed

    def _run(self, who: Client, at: datetime, *statements: str, read: bool = True) -> None:
        session = self._rec.session(self._wh, who.client, who.user)
        for offset, sql in enumerate(statements):
            when = at + timedelta(seconds=2 * offset)
            if read and sql.lstrip().upper().startswith("SELECT"):
                session.query(sql, when)
            else:
                session.execute(sql, when)

    def day_jobs(self, day: date) -> list[tuple[datetime, Job]]:
        """Everything that reads the warehouse on calendar day ``day`` (after the nightly ETL)."""
        rng = np.random.default_rng([self._seed, day.toordinal(), 2])
        at = lambda h, m=0: datetime.combine(day, time(h, m))  # noqa: E731
        jobs: list[tuple[datetime, Job]] = [(at(5), lambda t: self.legacy_extract(t))]
        weekday = day.weekday() < 5

        if day.weekday() == 0:
            jobs.append((at(7), lambda t: self.board_pack(t, day)))
        if day.weekday() == 5:
            jobs.append((at(4), lambda t: self.churn_forecast(t, day)))
            jobs.append((at(6, 30), lambda t: self.reverse_etl(t)))
        if day.day == 2:
            jobs.append((at(6), lambda t: self.finance_close(t)))
            jobs.append((at(9, 15), lambda t: self.close_workbook(t)))

        def business_hours(n: int) -> list[datetime]:
            secs = np.sort(rng.integers(8 * 3600, 18 * 3600, size=n))
            return [datetime.combine(day, time()) + timedelta(seconds=int(s)) for s in secs]

        for t in business_hours(rng.poisson(6 if weekday else 1)):
            jobs.append((t, lambda t: self.exec_dashboard(t, day)))
        for t in business_hours(rng.poisson(4 if weekday else 0.5)):
            region = str(rng.choice(REGIONS))
            jobs.append((t, partial(self.region_dashboard, day=day, region=region)))
        for t in business_hours(rng.poisson(2 if weekday else 0.2)):
            jobs.append((t, lambda t: self.product_mix(t, day)))
        for t in business_hours(rng.poisson(3 if weekday else 0)):
            user = str(rng.choice(ANALYSTS))
            template = int(rng.integers(0, 3))
            jobs.append((t, partial(self.analyst, day=day, user=user, template=template)))
        return jobs

    # -- workloads ---------------------------------------------------------------------------

    def exec_dashboard(self, at: datetime, day: date) -> None:
        self._run(
            EXEC_DASHBOARD,
            at,
            f"""SELECT ORDER_DATE, ORDERS, NET_REVENUE_EUR FROM RPT.V_EXEC_REVENUE_DAILY
WHERE ORDER_DATE >= {dt(day - timedelta(days=90))} ORDER BY ORDER_DATE""",
        )

    def region_dashboard(self, at: datetime, day: date, region: str) -> None:
        self._run(
            REGION_DASHBOARD,
            at,
            f"""SELECT ORDER_DATE, CHANNEL, SUM(NET_REVENUE_EUR) AS NET_REVENUE_EUR
FROM RPT.V_REGION_PERFORMANCE
WHERE ORDER_DATE >= {dt(day - timedelta(days=28))} AND REGION = '{region}'
GROUP BY ORDER_DATE, CHANNEL ORDER BY ORDER_DATE""",
        )

    def product_mix(self, at: datetime, day: date) -> None:
        self._run(
            MIX_DASHBOARD,
            at,
            f"""SELECT CATEGORY, SUM(UNITS) AS UNITS, SUM(DISCOUNT_EUR) AS DISCOUNT_EUR,
       SUM(NET_REVENUE_EUR) AS NET_REVENUE_EUR
FROM RPT.V_PRODUCT_MIX WHERE ORDER_DATE >= {dt(day - timedelta(days=30))} GROUP BY CATEGORY""",
        )

    def legacy_extract(self, at: datetime) -> None:
        # Refreshed every night, read by nobody: the cost engine should find it.
        self._run(
            LEGACY_EXTRACT,
            at,
            """CREATE OR REPLACE TABLE RPT.LEGACY_OPS_EXTRACT AS
SELECT ORDER_DATE, CUSTOMER_ID, CUSTOMER_NAME, REGION, CATEGORY,
       SUM(QUANTITY) AS UNITS, SUM(NET_REVENUE_EUR) AS NET_REVENUE_EUR
FROM MART.FCT_ORDER_LINES
GROUP BY ORDER_DATE, CUSTOMER_ID, CUSTOMER_NAME, REGION, CATEGORY""",
        )

    def board_pack(self, at: datetime, day: date) -> None:
        since = dt(day - timedelta(days=28))
        self._run(
            BOARD_PACK,
            at,
            f"SELECT ORDER_DATE, NET_REVENUE_EUR FROM RPT.V_EXEC_REVENUE_DAILY WHERE ORDER_DATE >= {since}",
            f"""SELECT REGION, SUM(NET_REVENUE_EUR) AS NET_REVENUE_EUR FROM RPT.V_REGION_PERFORMANCE
WHERE ORDER_DATE >= {since} GROUP BY REGION""",
        )

    def churn_forecast(self, at: datetime, day: date) -> None:
        self._run(
            CHURN_DAG,
            at,
            f"""CREATE OR REPLACE TABLE ML.CUSTOMER_FEATURES AS
SELECT CUSTOMER_ID, SEGMENT, REGION, COUNT(DISTINCT ORDER_ID) AS ORDERS_90D,
       SUM(NET_REVENUE_EUR) AS REVENUE_90D, MAX(ORDER_DATE) AS LAST_ORDER_DATE
FROM MART.FCT_ORDER_LINES
WHERE ORDER_DATE >= {dt(day - timedelta(days=90))}
GROUP BY CUSTOMER_ID, SEGMENT, REGION""",
            "SELECT CUSTOMER_ID, SEGMENT, REGION, ORDERS_90D, REVENUE_90D, LAST_ORDER_DATE FROM ML.CUSTOMER_FEATURES",
            f"""CREATE OR REPLACE TABLE ML.CHURN_SCORES AS
SELECT CUSTOMER_ID,
       CASE WHEN LAST_ORDER_DATE < {dt(day - timedelta(days=21))} THEN 0.9
            WHEN ORDERS_90D < 3 THEN 0.5 ELSE 0.1 END AS CHURN_RISK
FROM ML.CUSTOMER_FEATURES""",
        )

    def reverse_etl(self, at: datetime) -> None:
        self._run(
            REVERSE_ETL,
            at,
            "SELECT s.CUSTOMER_ID, c.CUSTOMER_NAME, s.CHURN_RISK FROM ML.CHURN_SCORES s "
            "JOIN RAW.CRM_CUSTOMERS c ON c.CUSTOMER_ID = s.CUSTOMER_ID WHERE s.CHURN_RISK >= 0.5",
        )

    def finance_close(self, at: datetime) -> None:
        self._run(
            FINANCE_DAG,
            at,
            """CREATE OR REPLACE TABLE FIN.MONTHLY_REVENUE AS
SELECT DATE_TRUNC('month', ORDER_DATE) AS REVENUE_MONTH, REGION,
       SUM(GROSS_REVENUE_EUR) AS GROSS_REVENUE_EUR, SUM(NET_REVENUE_EUR) AS NET_REVENUE_EUR
FROM MART.DAILY_REVENUE
GROUP BY DATE_TRUNC('month', ORDER_DATE), REGION""",
        )

    def close_workbook(self, at: datetime) -> None:
        self._run(
            CLOSE_WORKBOOK,
            at,
            "SELECT REVENUE_MONTH, REGION, NET_REVENUE_EUR FROM FIN.MONTHLY_REVENUE ORDER BY REVENUE_MONTH, REGION",
        )

    def analyst(self, at: datetime, day: date, user: str, template: int) -> None:
        since = dt(day - timedelta(days=14))
        sql = (
            f"""SELECT CHANNEL, SEGMENT, SUM(NET_REVENUE_EUR) AS NET_REVENUE_EUR
FROM MART.FCT_ORDER_LINES WHERE ORDER_DATE >= {since} GROUP BY CHANNEL, SEGMENT""",
            f"""SELECT c.SEGMENT, c.COUNTRY, COUNT(DISTINCT o.ORDER_ID) AS ORDERS
FROM STG.ORDERS o JOIN RAW.CRM_CUSTOMERS c ON c.CUSTOMER_ID = o.CUSTOMER_ID
WHERE o.ORDER_DATE >= {since} GROUP BY c.SEGMENT, c.COUNTRY""",
            f"""SELECT p.CATEGORY, AVG(l.DISCOUNT_PCT) AS AVG_DISCOUNT
FROM STG.ORDER_LINES l JOIN RAW.ERP_PRODUCTS p ON p.PRODUCT_ID = l.PRODUCT_ID
JOIN STG.ORDERS o ON o.ORDER_ID = l.ORDER_ID
WHERE o.ORDER_DATE >= {since} GROUP BY p.CATEGORY""",
        )[template]
        self._run(Client("DBeaver 25.1", user), at, sql)
