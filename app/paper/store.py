"""Incremental, restart-safe paper trading: one durable account directory per paper account.

    <root>/<account_id>/manifest.json   provenance, written once at creation
    <root>/<account_id>/ledger.jsonl    append-only, hash-chained events (app.paper.ledger)
    <root>/<account_id>/state.json      versioned account state + pointer to the last
                                        committed ledger event (seq, hash)
`<root>` defaults to the git-ignored `data/paper/accounts/`.

Commit protocol for one completed session T:
  1. reject T unless T > last processed session (T <= last: already processed -> no effect);
  2. PaperTrader.process(T) on the in-memory state;
  3. append the session's events to the ledger, flush + fsync;
  4. write state.json atomically (temp file, fsync, os.replace).
If the process dies between 3 and 4, the ledger holds events after the committed pointer.
On reopen they are kept aside (never deleted): re-processing T must reproduce exactly those
events (processing is deterministic), which then completes the commit without appending; any
difference raises LedgerError. A corrupt/edited ledger or an invalid state file raises — the
store never repairs or rewrites history.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.paper.config import MODE, PaperConfig
from app.paper.ledger import LedgerError, provenance, step_events, verify_chain
from app.paper.state import StateError, dumps, loads
from app.paper.trader import BarInput, PaperTrader, account_id_for

DEFAULT_ROOT = Path("data/paper/accounts")


class DataRevisionError(RuntimeError):
    """An already processed session arrived with different input data; nothing was changed."""


@dataclass
class ProcessOutcome:
    session: str
    status: str  # processed | already_processed | recovered
    events: int


def _fsync_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class PaperStore:
    """Durable paper account. Not thread/process safe: one writer per account directory."""

    def __init__(
        self,
        config: PaperConfig,
        *,
        strategy_id: str,
        strategy_version: str,
        instrument: str = "SPY",
        timeframe: str = "1d",
        root: Path = DEFAULT_ROOT,
    ) -> None:
        self.config = config
        self.account_id = account_id_for(
            config, strategy_id, strategy_version, instrument, timeframe
        )
        self.dir = Path(root) / self.account_id
        self.state_path = self.dir / "state.json"
        self.ledger_path = self.dir / "ledger.jsonl"
        self.manifest_path = self.dir / "manifest.json"
        self._ident = (strategy_id, strategy_version, instrument, timeframe)
        self._tail: list[dict[str, Any]] = []
        self._broken = False
        self.trader = self._open()

    # ── open / validate ──

    def _open(self) -> PaperTrader:
        strategy_id, strategy_version, instrument, timeframe = self._ident
        if not self.state_path.exists():
            if self.ledger_path.exists() and self.ledger_path.stat().st_size > 0:
                raise LedgerError("ledger exists without a state file; refusing to continue")
            self.dir.mkdir(parents=True, exist_ok=True)
            trader = PaperTrader.new(
                self.config,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                instrument=instrument,
                timeframe=timeframe,
            )
            meta = provenance(
                self.config,
                mode=f"{MODE}:incremental",
                strategy_versions={strategy_id: strategy_version},
                instrument=instrument,
                timeframe=timeframe,
            )
            meta["account_id"] = self.account_id
            _fsync_write(self.manifest_path, json.dumps(meta, indent=2, default=str))
            self.ledger_path.touch()
            self.seq, self.last_hash = 0, ""
            _fsync_write(self.state_path, dumps(trader.state, self._pointer()))
            return trader

        state, pointer = loads(self.state_path.read_text(encoding="utf-8"), self.config)
        ident = (state.strategy_id, state.strategy_version, state.instrument, state.timeframe)
        if state.account_id != self.account_id or ident != self._ident:
            raise StateError("stored account identity differs from the requested account")
        events = self._read_ledger()
        verify_chain(events, self.account_id)
        self.seq, self.last_hash = int(pointer["seq"]), str(pointer["last_hash"])
        if len(events) < self.seq:
            raise LedgerError("ledger is shorter than the committed state (events missing)")
        if self.seq and events[self.seq - 1]["hash"] != self.last_hash:
            raise LedgerError("ledger does not match the committed state pointer")
        self._tail = events[self.seq :]  # written but not committed (interrupted session)
        return PaperTrader(self.config, state)

    def _read_ledger(self) -> list[dict[str, Any]]:
        events = []
        with open(self.ledger_path, encoding="utf-8") as fh:
            for n, line in enumerate(fh, start=1):
                if not line.strip():
                    raise LedgerError(f"ledger line {n} is empty")
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise LedgerError(f"ledger line {n} is not valid JSON") from exc
        return events

    def _pointer(self) -> dict[str, Any]:
        return {"seq": self.seq, "last_hash": self.last_hash}

    # ── processing ──

    def process(self, bar: BarInput) -> ProcessOutcome:
        if self._broken:
            raise RuntimeError("a previous commit failed; reopen the PaperStore to recover")
        try:
            return self._process(bar)
        except BaseException:
            # the in-memory state may be ahead of the files: never use this object again
            self._broken = True
            raise

    def _process(self, bar: BarInput) -> ProcessOutcome:
        st = self.trader.state
        tag = bar.bar_ts.isoformat()
        if st.last_session is not None and bar.bar_ts <= st.last_session:
            if bar.bar_ts == st.last_session and bar.fingerprint() != st.last_input_fingerprint:
                raise DataRevisionError(
                    f"session {tag} was processed with different input data; not re-processed"
                )
            return ProcessOutcome(tag, "already_processed", 0)

        step = self.trader.process(bar)  # in-memory only until committed below
        events = step_events(step, self.account_id, self.seq + 1, self.last_hash)
        status = "processed"
        if self._tail:
            if self._tail[: len(events)] != events:
                raise LedgerError(
                    "uncommitted ledger events differ from the reprocessed session; refusing"
                )
            self._tail = self._tail[len(events) :]
            status = "recovered"
        else:
            self._append(events)
        self.seq, self.last_hash = events[-1]["seq"], events[-1]["hash"]
        self._write_state()
        return ProcessOutcome(tag, status, len(events))

    def process_many(self, bars: list[BarInput]) -> dict[str, int]:
        counts = {"processed": 0, "already_processed": 0, "recovered": 0}
        for b in bars:
            counts[self.process(b).status] += 1
        return counts

    def _append(self, events: list[dict[str, Any]]) -> None:
        text = "".join(json.dumps(e, sort_keys=True, allow_nan=False) + "\n" for e in events)
        with open(self.ledger_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())

    def _write_state(self) -> None:
        _fsync_write(self.state_path, dumps(self.trader.state, self._pointer()))

    # ── reading ──

    def events(self) -> list[dict[str, Any]]:
        """Committed ledger events (verified)."""
        events = self._read_ledger()
        verify_chain(events, self.account_id)
        return events[: self.seq]

    def status(self) -> dict[str, Any]:
        st, acct = self.trader.state, self.trader.state.account
        pending = self.trader.pending_action()
        return {
            "account_id": self.account_id,
            "strategy_id": st.strategy_id,
            "last_session": st.last_session.isoformat() if st.last_session is not None else None,
            "sessions_processed": st.session_seq,
            "cash": acct.cash,
            "position_quantity": acct.shares,
            "realized_pnl": acct.realized_pnl,
            "cumulative_costs": acct.cumulative_costs,
            "risk_state": st.risk.risk_state,
            "pending": None
            if pending is None
            else {
                "kind": pending.kind,
                "approved_target": pending.risk_approved_target,
                "scheduled_for": pending.scheduled_execution_at.isoformat(),
            },
            "ledger_events": self.seq,
            "uncommitted_ledger_events": len(self._tail),
        }
