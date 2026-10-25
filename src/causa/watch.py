"""Watching: tell people once about each new serious data incident, and send the brief once a day.

Causa's own record of what it said, when and where is kept in the warehouse (CAUSA.NOTIFICATIONS).
That record is what keeps alerts from repeating across restarts and replicas, and it is the audit
trail for "who was told what, when". Slack is used only when a webhook is configured; without one
the notifications are still recorded and shown in the web UI.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Any

import polars as pl

from causa import slack, store
from causa.brief import morning_brief, to_slack, to_text
from causa.health import Severity
from causa.health.models import HealthCase
from causa.lineage.actors import ActorKind
from causa.workspace import Workspace

log = logging.getLogger(__name__)

TABLE = "CAUSA.NOTIFICATIONS"
ALERTING = (Severity.CRITICAL, Severity.HIGH)
LOOKBACK_DAYS = 3  # incidents that started this recently and were never announced are announced


@dataclass
class Notification:
    key: str
    kind: str  # "incident" | "brief"
    text: str
    channel: str  # "slack" | "recorded"
    sent_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "text": self.text,
            "channel": self.channel,
            "sent_at": self.sent_at.isoformat(timespec="seconds"),
        }


def case_key(case: HealthCase) -> str:
    """Stable across rescans (case ids are renumbered): what broke, where, and since when."""
    p = case.primary
    return f"incident:{p.signal.value}:{p.table}:{p.column or ''}:{p.segment or ''}:{case.start}"


def incident_text(ws: Workspace, case: HealthCase) -> str:
    impact = ws.graph.impact(case.primary.table, since=case.start)
    consumers = sorted((a for a in impact.actors if a.reads > 0), key=lambda a: -a.reads)
    named = ", ".join(a.name for a in consumers[:5]) + (f" and {len(consumers) - 5} more" if len(consumers) > 5 else "")
    shared = [a for a in consumers if a.kind in (ActorKind.REVERSE_ETL, ActorKind.SCHEDULED_REPORT)]
    lines = [
        f"{case.severity.value.upper()} data incident since {case.start}: "
        f"{case.primary.signal.value.replace('_', ' ')} on {case.primary.table}. {case.primary.summary}",
        f"Reaches {len(impact.tables)} downstream tables and {len(consumers)} consumers"
        + (f": {named}." if named else "."),
    ]
    if shared:
        lines.append("Leaves the warehouse through: " + ", ".join(a.name for a in shared) + ".")
    return "\n".join(lines)


class Watcher:
    def __init__(
        self,
        ws: Workspace,
        lock: threading.RLock | None = None,
        webhook_url: str | None = None,
        brief_metric: str | None = None,
        brief_at: dtime | None = None,
    ) -> None:
        self.ws = ws
        self.lock = lock or threading.RLock()
        self.webhook_url = webhook_url
        self.brief_metric = brief_metric
        self.brief_at = brief_at

    # -- the record ---------------------------------------------------------------------------

    def _sent(self, keys: list[str]) -> set[str]:
        if not keys or not store.exists(self.ws.wh, TABLE):
            return set()
        listed = ", ".join("'" + k.replace("'", "''") + "'" for k in keys)
        frame = self.ws.wh.query(f"SELECT NOTIFICATION_KEY FROM {TABLE} WHERE NOTIFICATION_KEY IN ({listed})")
        return set(frame["notification_key"].to_list())

    def _record(self, n: Notification) -> None:
        store.ensure(self.ws.wh, TABLE)
        self.ws.wh.load(
            pl.DataFrame(
                {
                    "NOTIFICATION_KEY": [n.key],
                    "KIND": [n.kind],
                    "CHANNEL": [n.channel],
                    "SENT_AT": [n.sent_at],
                    "MESSAGE": [n.text[:2000]],
                }
            ),
            TABLE,
        )

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.lock:
            if not store.exists(self.ws.wh, TABLE):
                return []
            frame = self.ws.wh.query(
                f"SELECT NOTIFICATION_KEY AS KEY_, KIND, CHANNEL, SENT_AT, MESSAGE FROM {TABLE} "
                f"ORDER BY SENT_AT DESC LIMIT {int(limit)}"
            )
        return [
            Notification(r["key_"], r["kind"], r["message"], r["channel"], r["sent_at"]).as_dict()
            for r in frame.iter_rows(named=True)
        ]

    # -- sending ------------------------------------------------------------------------------

    def _send(self, key: str, kind: str, text: str, payload: dict[str, Any], now: datetime) -> Notification:
        channel = "recorded"
        if self.webhook_url:
            try:
                slack.post(self.webhook_url, payload)
                channel = "slack"
            except Exception as exc:  # recorded anyway; Slack being down must not stop the watch
                log.warning("Slack post failed: %s", exc)
        n = Notification(key, kind, text, channel, now)
        self._record(n)
        return n

    def check(self, now: datetime | None = None) -> list[Notification]:
        """One round: announce new serious incidents, and the brief when it is due. Idempotent."""
        now = now or datetime.now()
        sent: list[Notification] = []
        with self.lock:
            today = self.ws.today
            report = self.ws.health(today - timedelta(days=LOOKBACK_DAYS + 14), today)
            fresh = [c for c in report.cases if c.severity in ALERTING and c.start >= today - timedelta(LOOKBACK_DAYS)]
            done = self._sent([case_key(c) for c in fresh])
            for case in fresh:
                key = case_key(case)
                if key in done:
                    continue
                text = incident_text(self.ws, case)
                sent.append(self._send(key, "incident", text, {"text": text}, now))
            if self.brief_metric and self.brief_at and now.time() >= self.brief_at:
                sent.extend(self._brief(today, now))
        return sent

    def _brief(self, day: date, now: datetime) -> list[Notification]:
        key = f"brief:{self.brief_metric}:{day}"
        if self._sent([key]):
            return []
        assert self.brief_metric is not None
        b = morning_brief(self.ws, self.brief_metric, day)
        return [self._send(key, "brief", to_text(b), to_slack(b), now)]


def watch_forever(watcher: Watcher, every_minutes: float, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            for n in watcher.check():
                log.warning("notified (%s): %s", n.channel, n.text.splitlines()[0])
        except Exception:
            log.exception("watch round failed")
        stop.wait(every_minutes * 60)


def watcher_from_settings(ws: Workspace, lock: threading.RLock | None = None) -> Watcher:
    from causa.config import get_settings

    s = get_settings()
    brief_at = dtime.fromisoformat(s.brief_at) if s.brief_at else None
    return Watcher(
        ws,
        lock,
        webhook_url=s.slack_webhook_url.get_secret_value() if s.slack_webhook_url else None,
        brief_metric=s.brief_metric or (ws.config.metrics[0].name if brief_at else None),
        brief_at=brief_at,
    )
