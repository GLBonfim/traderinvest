"""Durable local alert store (git-ignored `data/alerts/`), same discipline as the Phase 12
paper ledger.

    alerts.jsonl      append-only, hash-chained alert records (what / when / why)
    deliveries.jsonl  append-only, hash-chained delivery attempts (channel, attempt, result)
    state.json        versioned: reporting start (`since`), active monitor conditions,
                      channel enablement, operational counters; atomic replace

Deduplication is by `alert_id` over the whole store, so re-processing a bar, refreshing the
dashboard or restarting never stores an alert twice. Corrupt files raise `AlertStoreError`;
nothing is repaired or rewritten.
"""

import hashlib
import json
import os
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from app.alerts.models import SEVERITIES, AlertEvent

STORE_SCHEMA_VERSION = 1


class AlertStoreError(RuntimeError):
    """The alert store is corrupt or incompatible; nothing was modified."""


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _hash(entry: dict[str, Any]) -> str:
    return hashlib.sha256(
        _canonical({k: v for k, v in entry.items() if k != "hash"}).encode()
    ).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class _Chain:
    """Append-only JSONL file with a SHA-256 chain (seq, prev_hash, hash)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: list[dict[str, Any]] = []
        if path.exists():
            prev = ""
            with open(path, encoding="utf-8") as fh:
                for n, line in enumerate(fh, start=1):
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise AlertStoreError(f"{path.name} line {n} is not valid JSON") from exc
                    if e.get("seq") != n or e.get("prev_hash") != prev or e.get("hash") != _hash(e):
                        raise AlertStoreError(f"{path.name} line {n}: hash chain broken")
                    prev = e["hash"]
                    self.entries.append(e)

    def append(self, bodies: list[dict[str, Any]]) -> None:
        if not bodies:
            return
        prev = self.entries[-1]["hash"] if self.entries else ""
        new: list[dict[str, Any]] = []
        for b in bodies:
            e = {"seq": len(self.entries) + len(new) + 1, **b, "prev_hash": prev}
            e["hash"] = _hash(e)
            new.append(e)
            prev = e["hash"]
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(_canonical(e) + "\n" for e in new))
            fh.flush()
            os.fsync(fh.fileno())
        self.entries += new


class AlertStore:
    def __init__(self, root: Path, *, since: date | None = None, create: bool = True) -> None:
        self.root = Path(root)
        self.state_path = self.root / "state.json"
        if not self.state_path.exists():
            if not create:
                raise AlertStoreError(f"no alert store at {self.root}")
            if (self.root / "alerts.jsonl").exists():
                raise AlertStoreError("alerts.jsonl exists without state.json; refusing")
            self.root.mkdir(parents=True, exist_ok=True)
            self.state: dict[str, Any] = {
                "schema_version": STORE_SCHEMA_VERSION,
                "since": None if since is None else since.isoformat(),
                "conditions": {},
                "channels": {},
                "counters": {"evaluations": 0, "deduplicated": 0},
            }
            self._save_state()
        else:
            try:
                self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise AlertStoreError("state.json is not valid JSON") from exc
            if self.state.get("schema_version") != STORE_SCHEMA_VERSION:
                raise AlertStoreError("unsupported alert store schema version")
        self._alerts = _Chain(self.root / "alerts.jsonl")
        self._deliveries = _Chain(self.root / "deliveries.jsonl")
        self._ids = {e["alert"]["alert_id"] for e in self._alerts.entries}

    # ── state ──

    def _save_state(self) -> None:
        _atomic_write(self.state_path, json.dumps(self.state, sort_keys=True, indent=2))

    @property
    def since(self) -> date | None:
        s = self.state["since"]
        return None if s is None else date.fromisoformat(s)

    def set_since(self, since: date) -> None:
        if self.state["since"] is None:
            self.state["since"] = since.isoformat()
            self._save_state()

    # ── alerts ──

    def add(self, alerts: list[AlertEvent], *, recorded_at: str) -> list[AlertEvent]:
        """Stores alerts whose id is new; returns them. Duplicates are counted, not stored."""
        new, seen = [], set()
        for a in alerts:
            if a.severity not in SEVERITIES:
                raise ValueError(f"invalid severity {a.severity!r}")
            if a.alert_id in self._ids or a.alert_id in seen:
                self.state["counters"]["deduplicated"] += 1
                continue
            seen.add(a.alert_id)
            new.append(a)
        self._alerts.append([{"recorded_at": recorded_at, "alert": asdict(a)} for a in new])
        self._ids |= seen
        self.state["counters"]["evaluations"] += 1
        self._save_state()
        return new

    def alerts(self) -> list[dict[str, Any]]:
        """Stored alerts (verified on open), oldest first, with `recorded_at`."""
        return [
            {**e["alert"], "recorded_at": e["recorded_at"], "seq": e["seq"]}
            for e in self._alerts.entries
        ]

    def __contains__(self, alert_id: str) -> bool:
        return alert_id in self._ids

    # ── deliveries ──

    def record_delivery(
        self,
        alert_id: str,
        channel: str,
        *,
        attempt: int,
        ok: bool,
        at: str,
        error: str = "",
        permanent: bool = False,
    ) -> None:
        self._deliveries.append(
            [
                {
                    "alert_id": alert_id,
                    "channel": channel,
                    "attempt": attempt,
                    "ok": ok,
                    "at": at,
                    "error": error,
                    "permanent_failure": permanent,
                }
            ]
        )

    def delivery_log(self) -> list[dict[str, Any]]:
        return list(self._deliveries.entries)

    def delivery_status(self) -> dict[tuple[str, str], dict[str, Any]]:
        """(alert_id, channel) -> {attempts, delivered, permanent_failure, last_at, error}."""
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for d in self._deliveries.entries:
            s = out.setdefault(
                (d["alert_id"], d["channel"]),
                {
                    "attempts": 0,
                    "delivered": False,
                    "permanent_failure": False,
                    "last_at": None,
                    "error": "",
                },
            )
            s["attempts"] = max(s["attempts"], d["attempt"])
            s["delivered"] |= d["ok"]
            s["permanent_failure"] |= d["permanent_failure"]
            s["last_at"], s["error"] = d["at"], d["error"]
        return out

    # ── channels / monitor conditions ──

    def channel_enabled_since(self, channel: str, now: str) -> str:
        """First time a channel was seen enabled; earlier alerts are not sent through it."""
        if channel not in self.state["channels"]:
            self.state["channels"][channel] = now
            self._save_state()
        return str(self.state["channels"][channel])

    def condition(self, name: str) -> dict[str, Any] | None:
        c = self.state["conditions"].get(name)
        return None if c is None else dict(c)

    def set_condition(self, name: str, value: dict[str, Any] | None) -> None:
        if value is None:
            self.state["conditions"].pop(name, None)
        else:
            self.state["conditions"][name] = value
        self._save_state()

    # ── metrics ──

    def metrics(self) -> dict[str, Any]:
        alerts = self.alerts()
        status = self.delivery_status()

        def by(k: str) -> dict[str, int]:
            return {v: sum(1 for a in alerts if a[k] == v) for v in sorted({a[k] for a in alerts})}

        return {
            "alerts_stored": len(alerts),
            "alerts_deduplicated": self.state["counters"]["deduplicated"],
            "evaluations": self.state["counters"]["evaluations"],
            "deliveries_succeeded": sum(1 for s in status.values() if s["delivered"]),
            "deliveries_failed_permanently": sum(
                1 for s in status.values() if s["permanent_failure"]
            ),
            "delivery_attempts_failed": sum(1 for d in self._deliveries.entries if not d["ok"]),
            "by_severity": by("severity"),
            "by_source": by("source"),
        }
