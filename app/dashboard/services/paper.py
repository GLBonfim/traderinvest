"""Paper accounts for the dashboard: discovery, read-only views, and the only two mutating
actions the dashboard has — both LOCAL PAPER SIMULATION operations delegated to `PaperStore`
(create an account, process available completed sessions). No other execution path exists.

Account state is read from disk on every call (never cached), and every read re-verifies the
ledger hash chain, so the UI cannot show stale or tampered account information silently.
"""

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from app.paper.config import PaperConfig
from app.paper.ledger import LedgerError
from app.paper.state import StateError
from app.paper.store import DataRevisionError, PaperStore, scan_accounts
from app.paper.trader import BarInput, account_id_for
from app.risk.config import SCENARIOS


class AccountError(RuntimeError):
    """An account cannot be opened safely (missing, corrupt, unsupported); nothing changed."""


@dataclass(frozen=True)
class AccountRef:
    account_id: str
    strategy_id: str
    strategy_version: str
    scenario: str
    instrument: str
    timeframe: str
    created_at: str
    path: Path


def paper_config(scenario_name: str) -> PaperConfig:
    for s in SCENARIOS:
        if s.name == scenario_name:
            return PaperConfig(risk=s)
    raise AccountError(f"unknown risk scenario {scenario_name!r}")


def list_accounts(root: Path) -> list[AccountRef]:
    """Accounts with a readable manifest under `root` (read-only; creates nothing)."""
    out: list[AccountRef] = []
    if not root.exists():
        return out
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        m = d / "manifest.json"
        if not m.exists():
            continue
        try:
            meta = json.loads(m.read_text(encoding="utf-8"))
            ((sid, version),) = meta["strategy_versions"].items()
            out.append(
                AccountRef(
                    meta.get("account_id", d.name),
                    sid,
                    version,
                    meta["risk_scenario"],
                    meta["instrument"],
                    meta["timeframe"],
                    meta.get("created_at", ""),
                    d,
                )
            )
        except (ValueError, KeyError, TypeError):
            out.append(AccountRef(d.name, "?", "?", "?", "?", "?", "", d))
    return out


def open_account(ref: AccountRef, root: Path) -> PaperStore:
    """Opens an EXISTING account (verifies state and ledger); never creates one."""
    if not (ref.path / "state.json").exists():
        raise AccountError(f"account {ref.account_id} has no state file")
    cfg = paper_config(ref.scenario)
    expected = account_id_for(
        cfg, ref.strategy_id, ref.strategy_version, ref.instrument, ref.timeframe
    )
    if expected != ref.path.name:
        raise AccountError(
            "account configuration is not one of the pre-declared scenarios (or was created "
            "under another risk-mechanism semantic version: ADR-0034)"
        )
    try:
        return PaperStore(
            cfg,
            strategy_id=ref.strategy_id,
            strategy_version=ref.strategy_version,
            instrument=ref.instrument,
            timeframe=ref.timeframe,
            root=root,
        )
    except (StateError, LedgerError) as exc:
        raise AccountError(f"{type(exc).__name__}: {exc}") from exc


def account_view(store: PaperStore) -> dict[str, Any]:
    """Account, position and current state from the verified ledger and stored state."""
    events = store.events()  # verifies the hash chain
    st, acct = store.trader.state, store.trader.state.account
    snaps = [e for e in events if e["event_type"] == "snapshot"]
    decisions = [e for e in events if e["event_type"] == "decision"]
    last = snaps[-1]["record"] if snaps else None
    dec = decisions[-1]["record"] if decisions else None
    pending = store.trader.pending_action()
    qty = acct.shares
    return {
        "account_id": store.account_id,
        "strategy_id": st.strategy_id,
        "strategy_version": st.strategy_version,
        "scenario": store.config.risk.name,
        "instrument": st.instrument,
        "initial_capital": acct.initial_capital,
        "cash": acct.cash,
        "position_quantity": qty,
        "average_cost": acct.cost_basis / qty if acct.holding else None,
        "cost_basis": acct.cost_basis,
        "mark_price": last["mark_price"] if last else None,
        "market_value": last["position_market_value"] if last else 0.0,
        "equity": last["equity"] if last else acct.cash,
        "exposure": last["gross_exposure"] if last else 0.0,
        "realized_pnl": last["realized_pnl"] if last else 0.0,
        "unrealized_pnl": last["unrealized_pnl"] if last else 0.0,
        "total_pnl": last["total_pnl"] if last else 0.0,
        "cumulative_costs": acct.cumulative_costs,
        "risk_state": st.risk.risk_state,
        "last_session": st.last_session,
        "sessions_processed": st.session_seq,
        "latest_strategy_state": dec["strategy_state"] if dec else None,
        "requested_exposure": dec["strategy_requested_target"] if dec else None,
        "approved_exposure": dec["risk_approved_target"] if dec else None,
        "intervention_reason": dec["intervention_reason"] if dec else None,
        "pending_kind": None if pending is None else pending.kind,
        "pending_target": None if pending is None else pending.risk_approved_target,
        "next_effective_session": st.pending.scheduled_for if st.pending else None,
        "ledger_events": len(events),
        "ledger_last_hash": events[-1]["hash"] if events else "",
        "ledger_status": "hash chain verified",
        "uncommitted_ledger_events": store.status()["uncommitted_ledger_events"],
    }


LEDGER_TYPES = ("decision", "order", "fill", "reconciliation", "snapshot")


# Display-only session types of the "Account snapshots" table (nothing is persisted)
SNAPSHOT_TRADE = "trade"
SNAPSHOT_NO_TRADE = "no trade"
SNAPSHOT_REFERENCE = "window-start reference (indicative)"
SNAPSHOT_INDETERMINATE = "indeterminate"
SNAPSHOT_TYPES_NOTE = (
    "Session type is derived for display only; no ledger field records it. A trade is shown "
    "only when a fill event exists in the same session. No record marks a window-start "
    "reference snapshot (ADR-0035): it is identical to a flat session that schedules an entry, "
    "so it is shown as indicative, inferred from the account's committed snapshot count being "
    "one more than its sessions processed. When the data cannot separate the types, the row "
    "is indeterminate."
)


def classify_snapshots(
    events: list[dict[str, Any]], sessions_processed: int, initial_capital: float
) -> dict[int, tuple[str, str]]:
    """seq of every committed snapshot event -> (session type, basis), for display only.

    - trade: a `fill` event exists in the same session (never inferred from position or equity);
    - no trade: no fill in the session, and not the account's first snapshot (only the first
      snapshot of an account can be a window-start reference, ADR-0035);
    - first snapshot without a fill: the record cannot tell; the committed snapshot count
      against `sessions_processed` gives an indication only (one more: reference; equal:
      counted session), and a reference must be flat with the initial capital; anything else
      is indeterminate."""
    fills = {e["session"] for e in events if e["event_type"] == "fill"}
    snaps = [e for e in events if e["event_type"] == "snapshot"]
    extra = len(snaps) - sessions_processed
    out: dict[int, tuple[str, str]] = {}
    for i, e in enumerate(snaps):
        rec = e["record"]
        if e["session"] in fills:
            kind = (SNAPSHOT_TRADE, "fill event recorded in the same session")
        elif i > 0:
            kind = (
                SNAPSHOT_NO_TRADE,
                "no fill event in this session; not the account's first snapshot, so not a "
                "window-start reference",
            )
        elif extra == 1 and rec["position_quantity"] == 0 and rec["equity"] == initial_capital:
            kind = (
                SNAPSHOT_REFERENCE,
                "indicative, not recorded: first snapshot, the ledger holds one snapshot more "
                "than the sessions processed, flat with the initial capital; no record field "
                "marks a reference",
            )
        elif extra == 0:
            kind = (
                SNAPSHOT_NO_TRADE,
                "no fill event in this session; counted as a processed session (indicative: "
                "snapshots equal the sessions processed)",
            )
        else:
            kind = (
                SNAPSHOT_INDETERMINATE,
                "first snapshot without a fill; the snapshot count and sessions processed do "
                "not allow a classification",
            )
        out[e["seq"]] = kind
    return out


def ledger_tables(store: PaperStore, last_n_sessions: int = 20) -> dict[str, pd.DataFrame]:
    """Recent ledger events per type (verified), newest first, with seq/event_id/hash. The
    snapshot table also shows a display-only session type (`classify_snapshots`), computed
    over the whole committed ledger."""
    events = store.events()
    sessions = sorted({e["session"] for e in events})[-last_n_sessions:]
    keep = set(sessions)
    st = store.trader.state
    types = classify_snapshots(events, st.session_seq, st.account.initial_capital)
    out = {}
    for t in LEDGER_TYPES:
        rows = [
            {
                "seq": e["seq"],
                "event_id": e["event_id"],
                **({"session_type": types[e["seq"]][0]} if t == "snapshot" else {}),
                **e["record"],
                "hash": e["hash"][:16],
                **({"session_type_basis": types[e["seq"]][1]} if t == "snapshot" else {}),
            }
            for e in events
            if e["event_type"] == t and e["session"] in keep
        ]
        out[t] = pd.DataFrame(rows).iloc[::-1].reset_index(drop=True)
    return out


# ── the only mutating actions: LOCAL PAPER SIMULATION ──


def paper_create_account(
    root: Path,
    *,
    strategy_id: str,
    strategy_version: str,
    scenario_name: str,
    instrument: str,
    timeframe: str = "1d",
) -> PaperStore:
    cfg = paper_config(scenario_name)
    acct = account_id_for(cfg, strategy_id, strategy_version, instrument, timeframe)
    if (root / acct / "state.json").exists():
        raise AccountError(f"paper account {acct} already exists")
    # never a silent parallel account next to an existing one (checked before anything is
    # written; the existing directories are only read)
    scan = scan_accounts(
        root,
        account_id=acct,
        strategy_id=strategy_id,
        scenario=scenario_name,
        instrument=instrument,
        timeframe=timeframe,
    )
    if scan.conflicts:
        raise AccountError(
            f"paper account(s) {scan.conflicts} already exist for {strategy_id} / "
            f"{scenario_name} / {instrument} / {timeframe} under another identity (an earlier "
            "configuration, risk-mechanism semantic version — ADR-0034 — or strategy "
            f"version); account {acct} was not created. Decide explicitly what to do with the "
            "existing account(s) first; they were not opened or changed."
        )
    if scan.unreadable:
        raise AccountError(
            f"account directories {scan.unreadable} have a manifest that cannot be read, so an "
            f"identity conflict cannot be ruled out; account {acct} was not created. They were "
            "not opened or changed."
        )
    return PaperStore(
        cfg,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        instrument=instrument,
        timeframe=timeframe,
        root=root,
    )


def paper_process_sessions(
    store: PaperStore, inputs: list[BarInput], start: date | None = None
) -> dict[str, int]:
    """Processes every completed session the account has not processed yet (idempotent).
    `start` applies only to a new account (its first result session; the decision at the close
    of the session before it is recorded first, `PaperStore.process_many`)."""
    try:
        return store.process_many(inputs, start=start)
    except (LedgerError, DataRevisionError) as exc:
        raise AccountError(f"{type(exc).__name__}: {exc}") from exc
