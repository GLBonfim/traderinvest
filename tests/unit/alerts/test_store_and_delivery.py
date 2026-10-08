"""Alert store (dedup, idempotency, restart, corruption) and delivery (bounded retries,
failure isolation, secrecy, no history replay, explicit test notifications)."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.alerts.channels import (
    MAX_ATTEMPTS,
    ConsoleChannel,
    WebhookChannel,
    configured_channels,
    deliver_pending,
)
from app.alerts.models import WARNING, AlertEvent, make_alert
from app.alerts.store import AlertStore, AlertStoreError

T0 = datetime(2024, 7, 1, 21, 0, tzinfo=UTC)
SECRET_URL = "https://hooks.example.invalid/T0K3N-SECRET"


def alert(n: int, etype: str = "REGIME_TREND_CHANGED", severity: str | None = None) -> AlertEvent:
    return make_alert(
        etype,
        key_parts=("SPY", f"2024-07-{n:02d}", "a", "b"),
        occurred_at=f"2024-07-{n:02d}T20:00:00+00:00",
        title=f"t{n}",
        message="m",
        payload={"n": n},
        severity=severity,
    )


def test_dedup_idempotency_and_restart(tmp_path: Path) -> None:
    s = AlertStore(tmp_path)
    assert [a.alert_id for a in s.add([alert(1), alert(2), alert(1)], recorded_at="r1")] == [
        alert(1).alert_id,
        alert(2).alert_id,
    ]
    assert s.add([alert(1), alert(2)], recorded_at="r2") == []  # process(event) twice
    s2 = AlertStore(tmp_path)  # restart
    assert s2.add([alert(2), alert(3)], recorded_at="r3") == [alert(3)]
    assert len(s2.alerts()) == 3 and s2.metrics()["alerts_deduplicated"] == 4
    assert s2.alerts()[0]["recorded_at"] == "r1"  # first recording kept, never rewritten


def test_corruption_is_detected_and_never_repaired(tmp_path: Path) -> None:
    s = AlertStore(tmp_path)
    s.add([alert(1), alert(2)], recorded_at="r")
    path = tmp_path / "alerts.jsonl"
    lines = path.read_text().splitlines()
    e = json.loads(lines[0])
    e["alert"]["title"] = "edited"
    path.write_text("\n".join([json.dumps(e), lines[1]]) + "\n")
    before = path.read_bytes()
    with pytest.raises(AlertStoreError, match="hash chain"):
        AlertStore(tmp_path)
    assert path.read_bytes() == before
    (tmp_path / "state.json").write_text("{not json")
    with pytest.raises(AlertStoreError, match=r"state\.json"):
        AlertStore(tmp_path)
    with pytest.raises(AlertStoreError, match="no alert store"):
        AlertStore(tmp_path / "missing", create=False)


class Flaky:
    """Fake webhook transport: fails `fail` times, then succeeds; records calls."""

    def __init__(self, fail: int, status_ok: int = 200) -> None:
        self.fail, self.calls, self.status_ok = fail, [], status_ok

    def __call__(self, url: str, body: bytes, timeout: float) -> int:
        self.calls.append((url, json.loads(body)))
        if len(self.calls) <= self.fail:
            raise ConnectionError(f"cannot reach {url}")
        return self.status_ok


def test_console_and_webhook_delivery_and_severity_filter(tmp_path: Path) -> None:
    s = AlertStore(tmp_path)
    t = Flaky(0)
    hook = WebhookChannel(SECRET_URL, min_severity=WARNING, transport=t)
    deliver_pending(s, [ConsoleChannel(), hook], T0)  # enables channels before the alerts
    s.add(
        [alert(1), alert(2, "REGIME_VOLATILITY_EXTREME")],
        recorded_at=(T0 + timedelta(1)).isoformat(),
    )
    rep = deliver_pending(s, [ConsoleChannel(), hook], T0 + timedelta(days=1))
    assert (rep.attempted, rep.delivered, rep.failed) == (3, 3, 0)  # 2 console + 1 webhook
    assert [c[1]["alert"]["event_type"] for c in t.calls] == ["REGIME_VOLATILITY_EXTREME"]
    again = deliver_pending(s, [ConsoleChannel(), hook], T0 + timedelta(days=2))
    assert again.attempted == 0  # delivered alerts are not delivered again
    s2 = AlertStore(tmp_path)  # restart
    assert deliver_pending(s2, [ConsoleChannel(), hook], T0 + timedelta(days=3)).attempted == 0


def test_bounded_retry_with_backoff_and_permanent_failure(tmp_path: Path) -> None:
    s = AlertStore(tmp_path)
    t = Flaky(fail=10)
    hook = WebhookChannel(SECRET_URL, min_severity="INFO", transport=t)
    deliver_pending(s, [hook], T0)
    s.add([alert(1)], recorded_at=(T0 + timedelta(seconds=1)).isoformat())
    now = T0 + timedelta(seconds=2)
    r1 = deliver_pending(s, [hook], now)
    assert (r1.attempted, r1.failed, r1.permanent_failures) == (1, 1, 0)
    assert deliver_pending(s, [hook], now + timedelta(seconds=30)).skipped_not_due == 1  # backoff
    r2 = deliver_pending(s, [hook], now + timedelta(seconds=61))
    r3 = deliver_pending(s, [hook], now + timedelta(seconds=61 + 301))
    assert (r2.failed, r3.permanent_failures) == (1, 1)
    assert deliver_pending(s, [hook], now + timedelta(days=30)).attempted == 0  # never forever
    assert len(t.calls) == MAX_ATTEMPTS == 3
    st = AlertStore(tmp_path).delivery_status()[(alert(1).alert_id, "webhook")]
    assert st["permanent_failure"] and not st["delivered"] and st["attempts"] == 3
    # the secret URL never reaches the store, the error text or repr
    blob = "".join(p.read_text() for p in tmp_path.iterdir() if p.is_file())
    assert "T0K3N-SECRET" not in blob and "hooks.example" not in blob
    assert "<url redacted>" in st["error"] and "T0K3N" not in repr(hook)
    assert s.metrics()["deliveries_failed_permanently"] == 1


def test_failed_delivery_never_touches_alerts_or_other_state(tmp_path: Path) -> None:
    s = AlertStore(tmp_path)
    hook = WebhookChannel(SECRET_URL, min_severity="INFO", transport=Flaky(fail=99))
    deliver_pending(s, [hook], T0)
    s.add([alert(1)], recorded_at=(T0 + timedelta(1)).isoformat())
    before = hashlib.sha256((tmp_path / "alerts.jsonl").read_bytes()).hexdigest()
    deliver_pending(s, [hook], T0 + timedelta(days=1))  # raises nothing
    assert hashlib.sha256((tmp_path / "alerts.jsonl").read_bytes()).hexdigest() == before


def test_new_channel_does_not_replay_history_and_tests_are_explicit(tmp_path: Path) -> None:
    s = AlertStore(tmp_path)
    s.add([alert(1, severity="CRITICAL")], recorded_at=T0.isoformat())
    t = Flaky(0)
    hook = WebhookChannel(SECRET_URL, min_severity="INFO", transport=t)
    assert deliver_pending(s, [hook], T0 + timedelta(hours=1)).attempted == 0
    test = make_alert(
        "TEST_NOTIFICATION",
        key_parts=("webhook", "x"),
        occurred_at="x",
        title="Test notification (not a market event)",
        message="m",
        payload={},
    )
    s.add([test], recorded_at=(T0 + timedelta(hours=2)).isoformat())
    assert deliver_pending(s, [hook], T0 + timedelta(hours=3)).attempted == 0  # not implicit
    r = deliver_pending(s, [hook], T0 + timedelta(hours=3), only={test.alert_id})
    assert r.delivered == 1 and t.calls[0][1]["alert"]["event_type"] == "TEST_NOTIFICATION"


def test_console_delivers_alerts_stored_without_delivery(tmp_path: Path) -> None:
    s = AlertStore(tmp_path)
    s.add([alert(1)], recorded_at=T0.isoformat())  # e.g. `alerts run --no-deliver`
    later = deliver_pending(s, [ConsoleChannel()], T0 + timedelta(hours=1))
    assert later.delivered == 1  # always-on channel: pending alerts are not skipped


def test_webhook_only_from_environment() -> None:
    assert [c.name for c in configured_channels({})] == ["console"]
    chans = configured_channels({"ALERT_WEBHOOK_URL": SECRET_URL})
    assert [c.name for c in chans] == ["console", "webhook"] and chans[1].min_severity == WARNING
