"""Posting to Slack through an incoming webhook (configured, never implied)."""

from __future__ import annotations

import json
import urllib.request
from typing import Any


def post(webhook_url: str, payload: dict[str, Any], timeout: float = 10.0) -> int:
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return int(response.status)
