"""Pluggable delivery channels. Delivery is a downstream side effect: it reads stored alerts and
records the result; it never touches research or paper-trading state, and it imports nothing
that can trade (tested).

Channels
- console  : structured log line (structlog JSON); always enabled; delivers every pending alert.
- dashboard: the Streamlit Alerts page reads the store directly (no delivery step).
- webhook  : optional generic HTTP POST (JSON) to `ALERT_WEBHOOK_URL` (environment only; the
             URL may contain a token and is never logged or stored). Disabled unless set.

Retry policy (bounded, non-blocking): at most `MAX_ATTEMPTS` = 3 attempts per (alert, channel);
attempt n+1 is made on a LATER run only once `BACKOFF_SECONDS[n]` have passed since attempt n;
after the last failed attempt the delivery is marked as a permanent failure. Nothing sleeps or
waits: a run delivers what is due and returns.
"""

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from urllib.request import Request, urlopen

from app.alerts.models import SEVERITY_RANK, WARNING
from app.alerts.store import AlertStore
from app.core.logging import get_logger

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (60, 300)  # wait before attempt 2, attempt 3
WEBHOOK_ENV = "ALERT_WEBHOOK_URL"
WEBHOOK_MIN_SEVERITY_ENV = "ALERT_WEBHOOK_MIN_SEVERITY"


class Channel(Protocol):
    name: str
    min_severity: str
    always_on: bool

    def send(self, alert: dict[str, Any]) -> None:
        """Deliver one stored alert; raise on failure."""


class ConsoleChannel:
    name = "console"
    min_severity = "INFO"
    always_on = True  # delivers every pending alert, including ones stored with --no-deliver

    def __init__(self) -> None:
        self.log = get_logger("alerts")

    def send(self, alert: dict[str, Any]) -> None:
        self.log.info(
            "alert",
            alert_id=alert["alert_id"],
            alert_type=alert["event_type"],
            severity=alert["severity"],
            source=alert["source"],
            instrument=alert["instrument"],
            strategy_id=alert["strategy_id"],
            account_id=alert["account_id"],
            occurred_at=alert["occurred_at"],
            title=alert["title"],
        )


Transport = Callable[[str, bytes, float], int]  # (url, body, timeout) -> HTTP status


def urllib_transport(url: str, body: bytes, timeout: float) -> int:
    if not url.startswith(("https://", "http://")):
        raise ValueError("webhook URL must be http(s)")
    req = Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})  # noqa: S310
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - scheme checked above
        return int(resp.status)


class WebhookChannel:
    """Generic JSON webhook (no vendor SDK). The URL is kept private to this object."""

    name = "webhook"
    always_on = False  # opt-in: never replays alerts recorded before it was first enabled

    def __init__(
        self,
        url: str,
        *,
        min_severity: str = WARNING,
        transport: Transport = urllib_transport,
        timeout: float = 5.0,
    ) -> None:
        self._url = url
        self.min_severity = min_severity
        self._transport = transport
        self._timeout = timeout

    def __repr__(self) -> str:  # never reveal the URL
        return f"WebhookChannel(min_severity={self.min_severity!r})"

    def send(self, alert: dict[str, Any]) -> None:
        body = json.dumps({"kind": "quant-platform-alert", "alert": alert}, default=str).encode()
        status = self._transport(self._url, body, self._timeout)
        if not 200 <= status < 300:
            raise RuntimeError(f"webhook returned HTTP {status}")


def configured_channels(env: dict[str, str] | None = None) -> list[Channel]:
    env = dict(os.environ) if env is None else env
    channels: list[Channel] = [ConsoleChannel()]
    url = env.get(WEBHOOK_ENV, "").strip()
    if url:
        sev = env.get(WEBHOOK_MIN_SEVERITY_ENV, WARNING).upper()
        channels.append(WebhookChannel(url, min_severity=sev if sev in SEVERITY_RANK else WARNING))
    return channels


@dataclass(frozen=True)
class DeliveryReport:
    attempted: int
    delivered: int
    failed: int
    permanent_failures: int
    skipped_not_due: int


def _safe_error(exc: Exception) -> str:
    """Exception type + short message, never a URL or credential."""
    msg = str(exc)
    for marker in ("http://", "https://"):
        if marker in msg:
            msg = msg.split(marker)[0] + "<url redacted>"
    return f"{type(exc).__name__}: {msg[:200]}"


def deliver_pending(
    store: AlertStore, channels: list[Channel], now: datetime, only: set[str] | None = None
) -> DeliveryReport:
    """One bounded, non-blocking delivery pass. Failures are recorded and never raised."""
    now_iso = now.isoformat()
    status = store.delivery_status()
    attempted = delivered = failed = permanent = not_due = 0
    for ch in channels:
        enabled_since = store.channel_enabled_since(ch.name, now_iso)
        if getattr(ch, "always_on", False):
            enabled_since = ""
        for alert in store.alerts():
            test = alert["event_type"] == "TEST_NOTIFICATION"
            if only is not None and alert["alert_id"] not in only:
                continue
            if test and only is None:
                continue  # test notifications are sent only by an explicit channel test
            if not test and SEVERITY_RANK[alert["severity"]] < SEVERITY_RANK[ch.min_severity]:
                continue
            if alert["recorded_at"] < enabled_since and not test:
                continue  # a newly enabled channel does not replay history
            s = status.get((alert["alert_id"], ch.name))
            if s and (s["delivered"] or s["permanent_failure"]):
                continue
            attempts = s["attempts"] if s else 0
            if attempts:
                due = datetime.fromisoformat(s["last_at"]) + timedelta(  # type: ignore[index]
                    seconds=BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS)) - 1]
                )
                if now < due:
                    not_due += 1
                    continue
            attempted += 1
            try:
                ch.send(alert)
                store.record_delivery(
                    alert["alert_id"], ch.name, attempt=attempts + 1, ok=True, at=now_iso
                )
                delivered += 1
            except Exception as exc:
                final = attempts + 1 >= MAX_ATTEMPTS
                store.record_delivery(
                    alert["alert_id"],
                    ch.name,
                    attempt=attempts + 1,
                    ok=False,
                    at=now_iso,
                    error=_safe_error(exc),
                    permanent=final,
                )
                failed += 1
                permanent += int(final)
    return DeliveryReport(attempted, delivered, failed, permanent, not_due)


def as_dict(report: DeliveryReport) -> dict[str, int]:
    return asdict(report)
