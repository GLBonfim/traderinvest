"""Dashboard `paper_create_account`: no silent parallel account next to an account of the same
strategy, scenario, instrument and timeframe under another identity (ADR-0034 semantic versions),
using the store's single manifest scan (`scan_accounts`). Temporary directories only.
"""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from app.alerts.monitor import read_paper_account
from app.backtest.engine import session_times
from app.dashboard.services import paper as svc
from app.paper.config import MODE, PAPER_VERSION, PaperConfig
from app.paper.engine import build_inputs
from app.paper.store import PaperStore, conflicting_accounts, scan_accounts
from app.paper.trader import account_id_for
from app.risk.config import SCENARIOS
from tests.unit.risk.test_overlay import CAL, flat_indicators, make_bars

DD20 = next(s for s in SCENARIOS if s.name == "drawdown_lock_20")
NEW = {"strategy_id": "sma_trend", "strategy_version": "1.0.0",
       "scenario_name": "drawdown_lock_20", "instrument": "SPY"}  # fmt: skip


def snapshot(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def old_identity_account(root: Path) -> Path:
    """A drawdown_lock_20 account as the pre-ADR-0034 code identified it: written by today's
    store, then given the pre-ADR identity (directory, manifest and state fingerprint)."""
    cfg = PaperConfig(risk=DD20)
    body = {"paper": PAPER_VERSION, "mode": MODE, **asdict(cfg)}
    old_fp = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]
    old_id = hashlib.sha256(f"{old_fp}|sma_trend|1.0.0|SPY|1d".encode()).hexdigest()[:12]
    s = PaperStore(cfg, strategy_id="sma_trend", strategy_version="1.0.0", root=root)
    bars = make_bars([(100.0 + i, 101.0 + i, 99.0 + i, 100.0 + i) for i in range(4)])
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    s.process_many(build_inputs(bars, pd.Series(["LONG"] * 4, index=bars.index),
                                flat_indicators(bars), times))  # fmt: skip
    old = root / old_id
    s.dir.rename(old)
    m = json.loads((old / "manifest.json").read_text(encoding="utf-8"))
    m.update(account_id=old_id, config_fingerprint=old_fp)
    (old / "manifest.json").write_text(json.dumps(m, indent=2), encoding="utf-8")
    st = json.loads((old / "state.json").read_text(encoding="utf-8"))
    st["identity"].update(config_fingerprint=old_fp, account_id=old_id)
    (old / "state.json").write_text(json.dumps(st, indent=2), encoding="utf-8")
    return old


def manifest_only(root: Path, name: str, **meta: Any) -> Path:
    d = root / name
    d.mkdir(parents=True)
    body = {"account_id": name, "strategy_versions": {"sma_trend": "1.0.0"},
            "risk_scenario": "drawdown_lock_20", "instrument": "SPY", "timeframe": "1d",
            **meta}  # fmt: skip
    (d / "manifest.json").write_text(json.dumps(body), encoding="utf-8")
    return d


def new_account_id() -> str:
    return account_id_for(PaperConfig(risk=DD20), "sma_trend", "1.0.0", "SPY", "1d")


# 1-3, 9: an account under the pre-ADR identity blocks a parallel account


def test_old_identity_blocks_creation_and_stays_untouched(tmp_path: Path) -> None:
    old = old_identity_account(tmp_path)
    assert read_paper_account(old).error_kind == "PAPER_ACCOUNT_INCOMPATIBLE"  # (9)
    before = snapshot(tmp_path)
    with pytest.raises(svc.AccountError) as exc:
        svc.paper_create_account(tmp_path, **NEW)
    msg = str(exc.value)
    assert old.name in msg and "another identity" in msg and "semantic version" in msg
    assert "Decide explicitly" in msg and new_account_id() in msg
    assert snapshot(tmp_path) == before  # (2) old account byte for byte; (3) nothing created
    assert sorted(p.name for p in tmp_path.iterdir()) == [old.name]
    assert read_paper_account(old).error_kind == "PAPER_ACCOUNT_INCOMPATIBLE"  # (9) unchanged


# 4-5: other instrument / timeframe / strategy / scenario do not block


@pytest.mark.parametrize(
    "meta",
    [{"instrument": "QQQ"}, {"timeframe": "1h"}, {"risk_scenario": "atr_stop_3"},
     {"strategy_versions": {"rsi_momentum": "1.0.0"}}],
    ids=["instrument", "timeframe", "scenario", "strategy"],
)  # fmt: skip
def test_unrelated_accounts_do_not_block(tmp_path: Path, meta: dict[str, Any]) -> None:
    other = manifest_only(tmp_path, "other0000000", **meta)
    before = snapshot(other)
    store = svc.paper_create_account(tmp_path, **NEW)
    assert store.account_id == new_account_id() and (store.dir / "state.json").exists()
    assert snapshot(other) == before


# 6: no conflict -> created as before


def test_creation_without_conflict_is_unchanged(tmp_path: Path) -> None:
    store = svc.paper_create_account(tmp_path, **NEW)
    assert sorted(p.name for p in tmp_path.iterdir()) == [new_account_id()]
    assert {p.name for p in store.dir.iterdir()} == {"manifest.json", "ledger.jsonl", "state.json"}
    (tmp_path / "not_an_account").mkdir()  # a directory without a manifest is not an account
    store2 = svc.paper_create_account(tmp_path / "second", **NEW)
    assert store2.account_id == store.account_id


# 7: the current identity is still refused by the existing check


def test_duplicate_current_identity_is_still_refused(tmp_path: Path) -> None:
    svc.paper_create_account(tmp_path, **NEW)
    before = snapshot(tmp_path)
    with pytest.raises(svc.AccountError, match="already exists"):
        svc.paper_create_account(tmp_path, **NEW)
    assert snapshot(tmp_path) == before


# 8: an unreadable manifest is not taken as "no conflict"


@pytest.mark.parametrize("text", ["{not json", json.dumps({"account_id": "x"}), "[]"])
def test_unreadable_manifest_blocks_creation(tmp_path: Path, text: str) -> None:
    bad = tmp_path / "bad000000000"
    bad.mkdir()
    (bad / "manifest.json").write_text(text, encoding="utf-8")
    before = snapshot(tmp_path)
    with pytest.raises(svc.AccountError, match="cannot be read") as exc:
        svc.paper_create_account(tmp_path, **NEW)
    assert "bad000000000" in str(exc.value)
    assert snapshot(tmp_path) == before
    scan = scan_accounts(tmp_path, account_id=new_account_id(), strategy_id="sma_trend",
                         scenario="drawdown_lock_20", instrument="SPY", timeframe="1d")  # fmt: skip
    assert scan.unreadable == ["bad000000000"] and scan.conflicts == []


def test_conflicting_accounts_keeps_its_result(tmp_path: Path) -> None:
    """The CLI's helper delegates to the single scan and returns exactly the conflicts."""
    manifest_only(tmp_path, "old000000000")
    bad = tmp_path / "bad000000000"
    bad.mkdir()
    (bad / "manifest.json").write_text("{not json", encoding="utf-8")
    kw = {"account_id": "new000000000", "strategy_id": "sma_trend",
          "scenario": "drawdown_lock_20", "instrument": "SPY", "timeframe": "1d"}  # fmt: skip
    assert conflicting_accounts(tmp_path, **kw) == ["old000000000"]
    assert scan_accounts(tmp_path, **kw).conflicts == ["old000000000"]
