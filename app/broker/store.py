"""Broker sandbox records (git-ignored `data/broker/`), same discipline as Phases 12/14/14.5.

    orders.jsonl  append-only, hash-chained events per client_order_id: intent, blocked,
                  submit_attempted (written BEFORE the request — write-ahead), submitted,
                  status, failed, unknown (ambiguous network outcome)
    state.json    versioned; armed flag (default False), kill switch, linked paper accounts,
                  counters; atomic read-modify-write

Nothing here contains credentials: records hold ids, symbols, quantities, prices and statuses.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.alerts.store import _atomic_write, _Chain

SCHEMA_VERSION = 1


class BrokerStoreError(RuntimeError):
    pass


def _empty() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "armed": False,
        "armed_at": None,
        "kill_switch": {"engaged": False, "at": None, "reason": ""},
        "linked_accounts": [],
        "counters": {"syncs": 0, "submitted": 0, "blocked": 0, "failed": 0},
    }


class BrokerStore:
    def __init__(self, root: Path, *, no_write: bool = False) -> None:
        self.root = Path(root)
        self.no_write = no_write
        self.state_path = self.root / "state.json"
        if self.state_path.exists():
            self.reload()
        else:
            self.state = _empty()
            if not no_write:
                self.root.mkdir(parents=True, exist_ok=True)
                self._save()
        try:
            self._orders = _Chain(self.root / "orders.jsonl")
        except Exception as exc:
            raise BrokerStoreError(str(exc)) from exc

    def reload(self) -> None:
        try:
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BrokerStoreError("broker state.json unreadable") from exc
        if self.state.get("schema_version") != SCHEMA_VERSION:
            raise BrokerStoreError("unsupported broker state schema")

    def _save(self) -> None:
        if self.no_write:
            raise BrokerStoreError("broker store opened without write access")
        _atomic_write(self.state_path, json.dumps(self.state, sort_keys=True, indent=2))

    def _update(self, **changes: Any) -> None:
        if self.state_path.exists():
            self.reload()
        self.state.update(changes)
        self._save()

    # ── controls (default: unarmed, kill switch released, nothing linked) ──

    @property
    def armed(self) -> bool:
        return bool(self.state["armed"])

    @property
    def kill_switch(self) -> bool:
        return bool(self.state["kill_switch"]["engaged"])

    @property
    def linked_accounts(self) -> list[str]:
        return list(self.state["linked_accounts"])

    def set_armed(self, armed: bool) -> None:
        self._update(armed=armed, armed_at=datetime.now(UTC).isoformat() if armed else None)

    def set_kill_switch(self, engaged: bool, reason: str = "") -> None:
        self._update(
            kill_switch={"engaged": engaged, "at": datetime.now(UTC).isoformat(), "reason": reason}
        )

    def link(self, account_id: str) -> None:
        self._update(linked_accounts=sorted({*self.linked_accounts, account_id}))

    def unlink(self, account_id: str) -> None:
        self._update(linked_accounts=[a for a in self.linked_accounts if a != account_id])

    def bump(self, counter: str) -> None:
        if self.state_path.exists():
            self.reload()
        self.state["counters"][counter] = int(self.state["counters"].get(counter, 0)) + 1
        self._save()

    # ── order events ──

    def record(self, event: str, client_order_id: str, **data: Any) -> None:
        if self.no_write:
            raise BrokerStoreError("broker store opened without write access")
        self._orders.append(
            [
                {
                    "event": event,
                    "client_order_id": client_order_id,
                    "at": datetime.now(UTC).isoformat(),
                    **data,
                }
            ]
        )

    def events(self) -> list[dict[str, Any]]:
        return list(self._orders.entries)

    def orders(self) -> dict[str, dict[str, Any]]:
        """client_order_id -> latest known state (folded from the event log)."""
        out: dict[str, dict[str, Any]] = {}
        for e in self._orders.entries:
            o = out.setdefault(
                e["client_order_id"], {"client_order_id": e["client_order_id"], "status": "intent"}
            )
            data = {k: v for k, v in e.items() if k not in ("seq", "prev_hash", "hash", "event")}
            o.update(data)
            o["last_event"] = e["event"]
            if e["event"] in ("blocked", "submit_attempted", "unknown", "failed"):
                o["status"] = e["event"]
            if e["event"] in ("submitted", "status"):
                o["status"] = e.get("broker_status", o["status"])
        return out

    def submitted_today(self, day: str) -> int:
        return sum(
            1 for e in self._orders.entries if e["event"] == "submitted" and e["at"][:10] == day
        )
