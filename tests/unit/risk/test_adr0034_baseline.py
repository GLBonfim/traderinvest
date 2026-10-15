"""ADR-0034 step 0: deterministic baselines of the CURRENT behaviour, recorded before any change.

What is fixed here is what the code does at commit 3c3738c — not the behaviour ADR-0034 wants.
The behaviour ADR-0034 specifies for invalid / zero equity (D-A5) is implemented since step 4a:
its `test_future_*` tests (formerly `xfail(strict=True)`) pass without a marker, and the tests
that fixed the former silent behaviour (NaN accepted, ZeroDivisionError) were removed. The
baseline hashes are unchanged by step 4a.

Only synthetic bars, in-memory engines (`RiskOverlay`, `PaperTradingEngine.replay*`) and pure
functions are used: no database, no paper account, no ledger file, no network.

Baseline hash (`frame_hash`): SHA-256 (hex) of UTF-8 text made of
  - a header line `adr0034-baseline-v1|<column>|<column>...` (the frozen column list, in order;
    the index is the first field, named `__index__`, when included);
  - one line per row, fields joined by `|`, rows in frame order, lines joined by `\\n`.
Field encoding (bit-exact, no rounding): floats (Python or numpy) as `float.hex` (`nan`, `inf`,
`-inf`, `-0x0.0p+0` distinguished); booleans as `T`/`F`; integers as decimal text; timestamps
as UTC ISO-8601 (`NaT` for missing); `None` as `None`; strings as JSON strings. Any other type
fails the hash (never silently stringified). Only the frozen columns are hashed, so columns
added later (ADR-0034 is additive) do not invalidate a baseline; removing or changing the
values of a frozen column does.
"""

import hashlib
import json
import math
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.backtest.config import BacktestConfig
from app.backtest.engine import session_times
from app.paper.config import PaperConfig
from app.paper.engine import PaperTradingEngine
from app.paper.trader import account_id_for
from app.risk.config import SCENARIOS, DrawdownSpec, SessionSpec
from app.risk.engine import RiskOverlay, RiskRun
from app.risk.limits import LOCKED, NORMAL, DrawdownMonitor, SessionMonitor
from tests.unit.alerts.test_rules import _paper_alerts
from tests.unit.risk.test_overlay import CAL, FREE, flat_indicators, make_bars, run
from tests.unit.strategies.helpers import session_walk

CONTROL, DD20, ATR3 = SCENARIOS[0], SCENARIOS[3], SCENARIOS[4]
L, F, I = "LONG", "FLAT", "INSUFFICIENT_DATA"  # noqa: E741
ENTRY = [(100, 101, 99, 100), (100, 101, 99, 100)]  # LONG at bar 0 -> buy at the open of bar 1

# ── canonical hashing ──


def _enc(v: Any) -> str:
    if v is None:
        return "None"
    if isinstance(v, bool | np.bool_):
        return "T" if v else "F"
    if isinstance(v, int | np.integer):
        return str(int(v))
    if isinstance(v, float | np.floating):
        return float(v).hex()
    if isinstance(v, pd.Timestamp | datetime | np.datetime64):
        ts = pd.Timestamp(v)
        if pd.isna(ts):
            return "NaT"
        return (ts.tz_convert("UTC") if ts.tzinfo else ts).isoformat()
    if isinstance(v, str):
        return json.dumps(v)
    raise TypeError(f"unhashable baseline value of type {type(v).__name__}")


def frame_hash(frame: pd.DataFrame, columns: list[str], *, index: bool = False) -> str:
    missing = [c for c in columns if c not in frame.columns]
    assert not missing, f"frozen columns missing: {missing}"
    names = (["__index__"] if index else []) + list(columns)
    lines = ["|".join(["adr0034-baseline-v1", *names])]
    for idx, row in zip(frame.index, frame[columns].itertuples(index=False), strict=True):
        fields = ([_enc(idx)] if index else []) + [_enc(v) for v in row]
        lines.append("|".join(fields))
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


# Frozen column lists (the columns these frames have at commit 3c3738c).
DECISION_COLS = [
    "bar_ts", "observed_at", "effective_at", "strategy_state", "requested_exposure",
    "sized_exposure", "approved_exposure", "target_exposure", "intervention", "reasons",
    "risk_state", "drawdown", "high_water_mark", "stop_level", "stop_triggered",
    "stop_intraday_touch", "realized_vol", "atr",
]  # fmt: skip
EQUITY_COLS = [
    "state", "position", "shares", "open", "close", "executed", "traded_notional", "costs",
    "cash", "equity", "risk_state", "daily_return", "cumulative_return", "running_peak",
    "drawdown", "gross_equity",
]  # fmt: skip
FILL_COLS = [
    "ts", "side", "price", "notional", "commission", "spread_cost", "slippage_cost",
    "total_cost", "target_exposure", "signal_observed_at",
]  # fmt: skip
TRADE_COLS = [
    "entry_time", "exit_time", "entry_price", "exit_price", "net_pnl", "net_return", "costs",
    "holding_sessions", "exit_reason",
]  # fmt: skip
PAPER_DECISION_COLS = [
    "decision_id", "account_id", "strategy_id", "strategy_version", "instrument", "timeframe",
    "bar_ts", "observed_at", "strategy_state", "strategy_requested_target",
    "risk_approved_target", "intervention", "intervention_reason", "risk_state",
    "scheduled_execution_at", "pending_kind", "config_fingerprint", "input_fingerprint",
]  # fmt: skip
PAPER_ORDER_COLS = [
    "order_id", "decision_id", "order_type", "side", "action", "requested_quantity",
    "approved_quantity", "scheduled_execution_at", "status", "rejection_reason",
]  # fmt: skip
PAPER_FILL_COLS = [
    "fill_id", "order_id", "executed_at", "side", "action", "price", "quantity", "notional",
    "commission", "spread_cost", "slippage_cost", "total_cost", "realized_pnl", "cash_after",
    "position_after",
]  # fmt: skip
PAPER_PORTFOLIO_COLS = [
    "snapshot_id", "ts", "valued_at", "cash", "position_quantity", "mark_price",
    "position_market_value", "gross_exposure", "equity", "cost_basis", "realized_pnl",
    "unrealized_pnl", "total_pnl", "cumulative_costs", "risk_state", "pending_kind",
]  # fmt: skip

BASELINE: dict[str, dict[str, str]] = {
    "coincident_stop_strategy_exit": {
        "decisions": "af14f9f63f76a7cd1421d74a950246936689bf3ae9d92ff2858f61d2b3d9a839",
        "equity": "83c222cb9db6f0d02a2387d55a65c997964ea6a8b29363083a3b17feb0e8eb2d",
        "fills": "e7e9080c9eaf5b9f0c2f9e68286ef19c04c2328fc9b6de99e891641fe64cc5a1",
        "trades": "27de080d7f5881c1a6e12e0e5daa1f11b5f5fe3ddd5c8300a3f5eb66bea856e9",
    },
    "drawdown_exact_threshold": {
        "decisions": "c00a874c4fa8c85a35e0be10bce82311588175c65313de823f11b5b281dabfa4",
        "equity": "8a7475002de64bf87bb2d41d247d5af3055f7cea19228b492ab856cb05658839",
        "fills": "8f51b8a63bdfe1ce0ca7559ca0529dbde9b834eeeb76e7bbcb877b1d210b6b3d",
        "trades": "dbdc10d1d25d6e55a41fac05ae78c9f17c697b2fb4f32577fa1c6402e20113d5",
    },
    "drawdown_path_control": {
        "decisions": "6db33749719f8fb33b2c5b7ecc4dc5726090debb0c05693f1a4a102a4bafabb1",
        "equity": "01f0261e8f2fad56cf6bb7633afe56153d5e42075111c729ac96466d0da2703a",
        "fills": "f3e4e7662554cd422c3efc38f8d436b9aa4a79437e5a18ce3a45cb09314aca50",
        "trades": "35d292936642ea4dc0f27b9cc5318417828d90d08be35d1acc5a44ab0ad36ad9",
    },
    "drawdown_path_dd20": {
        "decisions": "d109cb652aa69f2d8f5f7c779f6ca5fce3a628ae6481e797e9778675ce08316b",
        "equity": "2866b75e10e86ea9497205e69f2decf8a1820915ceae0d5c844191e96bda3d91",
        "fills": "1329cafb1bf17a7019451e6c5d1dcb279fd37b85f3f8fd9103713f5fd87b4018",
        "trades": "84112aad1b35cd0f22c3f574478f6945738953cbbc14d26609256d34bf62605d",
    },
    "lockout_L_I_L_F": {
        "decisions": "55d98929e64a3449f780ccaa0fadf065616576410666e9ff6e63e2fbdbdb2aea",
        "equity": "15fda50cf764b1aa90090e2269eff23a7defb81b21604168364d9c54389f140f",
        "fills": "01175e92147d8571700bb9df6546295939a17411d0171af6bc6c79d3af1954af",
        "trades": "8bf2785c6d04e9ece54930747c490452809af07627ab64ea3ec0336311c3d358",
    },
    "paper_rejected_exit": {
        "decisions": "70bd029600605d07a0be706fa6d222e987d34ab861d1e63dc76734f5bfbad420",
        "orders": "8a062c60e25c0ad27f88994ad16e10ac2e563dfae49129b93c7811f7b4aa6f3a",
        "fills": "1aab15140b32657e0c9c4e7bb9a9d07f539e56980137c893170b74ecc97615f8",
        "portfolio": "7f8793e991a216475f87d1ccf0fe3f43a6013975fb222787c856d2a8cd750fb9",
    },
    "stop_path_atr3": {
        "decisions": "6a23c0c22b1af645a1a27359e76830d07a34f9b35d9cf1da9245ea6dead0f302",
        "equity": "1eed9db7c6faaa34ce9418206ab6259bf356456be446155da7327837437457d0",
        "fills": "e7e9080c9eaf5b9f0c2f9e68286ef19c04c2328fc9b6de99e891641fe64cc5a1",
        "trades": "0867d2c4824e7a5bc86dc339c62abdcff6c383d03a72df19ad3886bb8dfa0255",
    },
    "walk_atr_stop_3": {
        "decisions": "f81f20d9d00c43337a4be276c4dd37f1af4feced5dbc9a28d4d22eca47598a68",
        "equity": "8fac70def5e23672403cc45489fd0cfe87a07cf7e47336286f49d5a0202e83e2",
        "fills": "f8129a43358ea884149dd942fb0f947965be2220cc6e46fece28aa77f36009e3",
        "trades": "26cfe5ccd5e2e0b1889989221bde7a512ee3af13a40728d4e35559807ac21e15",
    },
    "walk_control": {
        "decisions": "52e15edab4d5ee1d4f7ba89481149163caa6f2706f005678bf0d3576b2ce6544",
        "equity": "8fac70def5e23672403cc45489fd0cfe87a07cf7e47336286f49d5a0202e83e2",
        "fills": "f8129a43358ea884149dd942fb0f947965be2220cc6e46fece28aa77f36009e3",
        "trades": "26cfe5ccd5e2e0b1889989221bde7a512ee3af13a40728d4e35559807ac21e15",
    },
    "walk_drawdown_lock_20": {
        "decisions": "7ef6efb6f290d1efc64d4998c03da4f5df987eac85c316ae1b2b506e6fe35863",
        "equity": "29d83b59b0ac2374f208f3dbac31f695eb16c54fba9f3cdbde191c8febf8c6e2",
        "fills": "f6db8db34afbdad8af8def79a278cd797e08954cc1b5138bc03ba6401430f898",
        "trades": "777ba63f0237fb50f9833139bd8a74643ef08b10995bbcc6f81d889d732932b9",
    },
    "window_started_long": {
        "decisions": "c653e5c9f9175d95c7a2030ab6c156dac2d9bab6d0c93c6e57044a0a843ce339",
        "equity": "d2eee921edf6432c9f2ace969238849ab832870767bafcccc8b3d7e1623b42f5",
        "fills": "45410d9bb5daf401baf93f748b8f0b16ea0959cf851d4419debfd07a7b3ebd56",
        "trades": "35d292936642ea4dc0f27b9cc5318417828d90d08be35d1acc5a44ab0ad36ad9",
    },
}


# ADR-0034 changes that are INTENDED, recorded deliberately in the step that makes them. The
# BASELINE above stays the record of the pre-ADR behaviour (never rewritten): a frame listed
# here must DIFFER from its BASELINE hash and equal the value below; every other frame of every
# scenario must still equal BASELINE.
ADR0034_INTENDED: dict[str, dict[str, str]] = {
    # D-A2: equity exactly at the lock level (8000 = 80% of 10000) now locks at that close
    "drawdown_exact_threshold": {
        "decisions": "2da70fd3296c8d0621b40b837343001dd67e8cb97c17754ca99e3a1a7e5387f8",
        "equity": "f6212bf40dc1b16438414c6d53e43f8231f702401a8ca6227a4d284651efd95a",
        "fills": "994e70a879424f7236bafe2b9deb126fc5ad371bf7b5871a5cbf190d44a697a8",
        "trades": "09a285e603c4b6bebe5dfad0be040df17b39466818e6933cc5579bd9c43e39d7",
    },
    # Step H (STOP_SEMANTICS 2.0.0 in the atr_stop_3 payload): only the identifiers change
    # (ids derive from the account id, which derives from the paper fingerprint); with the
    # pre-ADR identity substituted, every frame hashes to BASELINE (test below)
    "paper_rejected_exit": {
        "decisions": "804c9f1edcc0144ebd785e51d55618e3010022ce119465c8ad493358f2daa55b",
        "orders": "a8a4ac9777a6e6697c5a150e9fff05c0f84d205ce4c9e9f90940d7d59ba85b24",
        "fills": "3c058428da69bdf10e9536790ce86fd8cfbabaeded48cbe640321d8d2195951d",
        "portfolio": "6c5da3502d8b9fd4ca687a48d53b57321a9e1f4ebb764303fc2471cdf17ac154",
    },
}


def run_hashes(r: RiskRun) -> dict[str, str]:
    return {
        "decisions": frame_hash(r.decisions, DECISION_COLS),
        "equity": frame_hash(r.equity, EQUITY_COLS, index=True),
        "fills": frame_hash(r.fills, FILL_COLS),
        "trades": frame_hash(r.trades, TRADE_COLS),
    }


# ── synthetic scenarios (deterministic, small) ──


def walk_bars_and_states() -> tuple[pd.DataFrame, list[str]]:
    """300-session seeded walk; states from a causal 20-session mean of the close."""
    bars = session_walk(300, seed=21).assign(adj_close=lambda b: b["close"])
    mean = bars["close"].rolling(20).mean()
    states = [
        I if pd.isna(m) else (L if c > m else F) for c, m in zip(bars["close"], mean, strict=True)
    ]
    return bars, states


def walk_run(risk: Any) -> RiskRun:
    bars, states = walk_bars_and_states()
    return run(bars, states, risk, bt=BacktestConfig())  # Phase 8 default costs (D)


def drawdown_path_bars() -> pd.DataFrame:
    """Entry at 100, slide to 70 (lock at -20%), 25 flat sessions, recovery to 90."""
    path = [100, 100, 97, 93, 89, 85, 81, 78, 75, 72, 70] + [70] * 25 + [72, 75, 78, 81, 84, 87, 90]
    return make_bars([(p, p + 0.5, p - 0.5, p) for p in path])


def stop_path_bars() -> pd.DataFrame:
    """Entry at 100 with ATR 2 (stop 94); close 93 triggers; exit at the next open."""
    return make_bars(
        [*ENTRY, (99, 99, 93, 93.5), (92, 93, 90, 91), (91, 92, 90, 91), (91, 92, 90, 92)]
    )


def coincident_bars() -> pd.DataFrame:
    return make_bars([*ENTRY, (99, 99, 92, 93), (92, 93, 90, 91)])


def threshold_bars() -> pd.DataFrame:
    """FREE costs: 100 shares bought at 100; close 80 gives equity 8000 = exactly 80% of the HWM
    (10000); then 79."""
    return make_bars(
        [*ENTRY, (95, 96, 89, 90), (90, 90, 79.5, 80), (80, 80, 78.5, 79), (79, 80, 78, 79)]
    )


def lockout_bars() -> pd.DataFrame:
    return make_bars(
        [
            *ENTRY,
            (99, 99, 92, 93),
            (93, 95, 92, 94),
            (94, 96, 93, 95),
            (95, 96, 94, 95),
            (95, 97, 94, 96),
            (96, 97, 95, 96),
        ]
    )


LOCKOUT_STATES = [L, L, L, I, L, F, L, L]  # trigger at bar 2; I, L blocked; F releases; L re-enters


def paper_rejected_exit_run() -> Any:
    """Stop triggers at the close of bar 3; the exit's scheduled session (bar 4) is missing
    from the data, so the exit order is rejected at bar 5 and re-planned."""
    full = make_bars(
        [
            (100, 101, 99, 100),
            (100, 101, 99, 100),
            (100, 101, 99, 100),
            (99, 99, 92, 93),
            (93, 94, 92, 93),
            (93, 94, 92, 93),
            (93, 94, 92, 93),
            (93, 94, 92, 93),
        ]
    )
    bars = full.drop(full.index[4])
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    states = pd.Series([L] * len(bars), index=bars.index)
    engine = PaperTradingEngine(PaperConfig(risk=ATR3, backtest=FREE))
    return engine.replay(bars, states, flat_indicators(bars), times, strategy_id="x")


def window_long_run() -> tuple[pd.DataFrame, RiskRun]:
    """Window starting at bar 3 while the strategy is already LONG (warm-up bars 0-2)."""
    bars = make_bars([(100 + i, 101 + i, 99 + i, 100 + i) for i in range(10)])
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    r = RiskOverlay(ATR3, FREE).run(
        bars, pd.Series([L] * 10, index=bars.index), flat_indicators(bars), times,
        strategy_id="x", start_index=3,
    )  # fmt: skip
    return bars, r


def paper_hashes(run_: Any) -> dict[str, str]:
    return {
        "decisions": frame_hash(run_.decisions, PAPER_DECISION_COLS),
        "orders": frame_hash(run_.orders, PAPER_ORDER_COLS),
        "fills": frame_hash(run_.fills, PAPER_FILL_COLS),
        "portfolio": frame_hash(run_.portfolio, PAPER_PORTFOLIO_COLS),
    }


SCENARIOS_UNDER_TEST: dict[str, Any] = {
    "walk_control": lambda: run_hashes(walk_run(CONTROL)),
    "walk_drawdown_lock_20": lambda: run_hashes(walk_run(DD20)),
    "walk_atr_stop_3": lambda: run_hashes(walk_run(ATR3)),
    "drawdown_path_dd20": lambda: run_hashes(run(drawdown_path_bars(), [L] * 43, DD20)),
    "drawdown_path_control": lambda: run_hashes(run(drawdown_path_bars(), [L] * 43, CONTROL)),
    "stop_path_atr3": lambda: run_hashes(run(stop_path_bars(), [L] * 6, ATR3)),
    "coincident_stop_strategy_exit": lambda: run_hashes(run(coincident_bars(), [L, L, F, F], ATR3)),
    "drawdown_exact_threshold": lambda: run_hashes(run(threshold_bars(), [L] * 6, DD20)),
    "lockout_L_I_L_F": lambda: run_hashes(run(lockout_bars(), LOCKOUT_STATES, ATR3)),
    "window_started_long": lambda: run_hashes(window_long_run()[1]),
    "paper_rejected_exit": lambda: paper_hashes(paper_rejected_exit_run()),
}


# ── compatibility values (acceptance criteria of ADR-0034) ──


def test_control_research_fingerprint() -> None:
    assert CONTROL.name == "control_no_overlay"
    assert CONTROL.fingerprint() == "3d5570e854d0e703"


def test_control_paper_fingerprint() -> None:
    assert PaperConfig().fingerprint() == "411f10ae26cb281f"


def test_control_account_id() -> None:
    assert account_id_for(PaperConfig(), "sma_trend", "1.0.0", "SPY", "1d") == "3f95a6964b8e"


# ── hashing is well defined ──


def test_hash_encoding_is_bit_exact_and_typed() -> None:
    assert _enc(0.1) == (0.1).hex() and _enc(-0.0) != _enc(0.0)
    assert _enc(float("nan")) == "nan" and _enc(float("inf")) == "inf"
    assert _enc(np.float64(1.5)) == _enc(1.5) and _enc(True) == "T" and _enc(np.int64(3)) == "3"
    assert _enc(pd.Timestamp("2024-07-01 09:30", tz="America/New_York")) == (
        "2024-07-01T13:30:00+00:00"
    )
    with pytest.raises(TypeError):
        _enc(object())
    frame = pd.DataFrame({"a": [1.0, 2.0]})
    assert frame_hash(frame, ["a"]) != frame_hash(
        frame.assign(a=[1.0, math.nextafter(2.0, 3)]), ["a"]
    )


@pytest.mark.parametrize("name", sorted(SCENARIOS_UNDER_TEST))
def test_scenarios_are_deterministic(name: str) -> None:
    assert SCENARIOS_UNDER_TEST[name]() == SCENARIOS_UNDER_TEST[name]()


# ── baselines of the current behaviour ──


@pytest.mark.parametrize("name", sorted(SCENARIOS_UNDER_TEST))
def test_current_behaviour_matches_baseline(name: str) -> None:
    got = SCENARIOS_UNDER_TEST[name]()
    assert set(got) == set(BASELINE[name])
    intended = ADR0034_INTENDED.get(name, {})
    for frame, before in BASELINE[name].items():
        if frame in intended:  # an intended ADR-0034 change: differs, and equals its record
            assert got[frame] != before and got[frame] == intended[frame], (name, frame)
        else:
            assert got[frame] == before, (name, frame)


def test_paper_rejected_exit_differs_only_by_the_atr3_identity() -> None:
    """With the pre-ADR identity (the paper payload without `semantics`) put back into the
    identifier columns, every paper frame is exactly its step-0 baseline."""
    from dataclasses import asdict

    from app.paper.config import MODE, PAPER_VERSION

    p = paper_rejected_exit_run()
    cfg = PaperConfig(risk=ATR3, backtest=FREE)
    body = {"paper": PAPER_VERSION, "mode": MODE, **asdict(cfg)}
    raw = json.dumps(body, sort_keys=True, default=str).encode()
    old_fp = hashlib.sha256(raw).hexdigest()[:16]
    old_acct = hashlib.sha256(f"{old_fp}|x|1.0.0|SPY|1d".encode()).hexdigest()[:12]
    assert old_fp != cfg.fingerprint()
    assert p.account_id == account_id_for(cfg, "x", "1.0.0", "SPY", "1d") != old_acct
    frames = {
        "decisions": (PAPER_DECISION_COLS, ("decision_id", "account_id", "config_fingerprint")),
        "orders": (PAPER_ORDER_COLS, ("order_id", "decision_id")),
        "fills": (PAPER_FILL_COLS, ("fill_id", "order_id")),
        "portfolio": (PAPER_PORTFOLIO_COLS, ("snapshot_id",)),
    }
    for name, (cols, ids) in frames.items():
        f = getattr(p, name).copy()
        for c in ids:
            f[c] = f[c].str.replace(cfg.fingerprint(), old_fp).str.replace(p.account_id, old_acct)
        assert frame_hash(f, cols) == BASELINE["paper_rejected_exit"][name], name


def test_intended_changes_are_limited_to_listed_scenarios_and_frames() -> None:
    assert set(ADR0034_INTENDED) <= set(BASELINE)
    for name, frames in ADR0034_INTENDED.items():
        assert set(frames) <= set(BASELINE[name])


# ── explicit facts of the current behaviour (readable complements of the hashes) ──


def test_current_coincident_stop_and_strategy_exit() -> None:
    bars = coincident_bars()
    r = run(bars, [L, L, F, F], ATR3)
    d = r.decisions
    assert bool(d.iloc[2]["stop_triggered"]) and d.iloc[2]["reasons"] == ""
    assert len(r.fills) == 2 and r.fills.iloc[1]["ts"] == bars.index[3]
    assert r.trades.iloc[0]["exit_reason"] == "strategy_or_sizing"  # ADR-0034 D-A1: additive fix


def test_drawdown_exactly_at_threshold_locks() -> None:
    """ADR-0034 D-A2 (implemented): equality with the lock level locks; the reported ratio is
    unchanged (-0.19999999999999996). Before ADR-0034 this close was WARNING and the lock came
    one close later (recorded by BASELINE["drawdown_exact_threshold"])."""
    r = run(threshold_bars(), [L] * 6, DD20)
    eq = r.equity
    assert eq["equity"].iloc[3] == 8000.0 and eq["running_peak"].iloc[3] == 10_000.0
    assert r.decisions.iloc[3]["drawdown"] == -0.19999999999999996
    assert eq["risk_state"].iloc[3] == LOCKED  # exactly 80% of the HWM: locked
    assert "drawdown_lock_flat" in r.decisions.iloc[3]["reasons"]
    m = DrawdownMonitor(DrawdownSpec(enabled=True), hwm=100.0)
    m.update(80.0)
    assert m.state == LOCKED


def test_current_lockout_sequence() -> None:
    bars = lockout_bars()
    r = run(bars, LOCKOUT_STATES, ATR3)
    reasons = r.decisions["reasons"].tolist()
    assert "stop_triggered" in reasons[2]
    assert "stop_lockout_until_signal_reset" in reasons[3]  # INSUFFICIENT_DATA keeps it
    assert "stop_lockout_until_signal_reset" in reasons[4]  # LONG still blocked
    assert reasons[5] == ""  # FLAT releases
    assert r.fills["side"].tolist() == ["buy", "sell", "buy"]
    # FLAT at bar 5 releases; the LONG decision at bar 6 re-enters at the open of bar 7
    assert r.fills.iloc[2]["ts"] == bars.index[7]


def test_current_window_started_long() -> None:
    bars, r = window_long_run()
    assert r.decisions.iloc[0]["bar_ts"] == bars.index[2]  # decision on the bar before the window
    assert r.equity.index[0] == bars.index[3]
    first = r.fills.iloc[0]
    assert first["side"] == "buy" and first["ts"] == bars.index[3] and first["price"] == 103.0
    assert r.decisions.iloc[1]["stop_level"] == 97.0  # entry 103 - 3 x ATR 2
    assert r.decisions.iloc[1]["high_water_mark"] == 10_000.0


def test_current_drawdown_lock_cooldown_rows() -> None:
    r = run(drawdown_path_bars(), [L] * 43, DD20)
    states = r.equity["risk_state"].tolist()
    first = states.index(LOCKED)
    assert states.count(LOCKED) == 21 and states[first : first + 21] == [LOCKED] * 21
    assert states[first + 21] == NORMAL
    assert "drawdown_lock_flat" in r.decisions.iloc[first]["reasons"]


def test_current_runs_are_isolated() -> None:
    alone = run_hashes(run(stop_path_bars(), [L] * 6, ATR3))
    run(drawdown_path_bars(), [L] * 43, DD20)  # an unrelated run in between
    assert run_hashes(run(stop_path_bars(), [L] * 6, ATR3)) == alone


def test_current_paper_rejected_exit() -> None:
    """Fills, orders and conditions as recorded in step 0 (hashed baseline unchanged); since
    ADR-0034 D-A1 the alert follows the stop ACTIVATION: one per episode (it was one per
    triggering close, i.e. 2, before)."""
    p = paper_rejected_exit_run()
    assert p.orders["status"].tolist() == ["FILLED", "REJECTED", "FILLED"]
    assert p.orders.iloc[1]["rejection_reason"] == "scheduled_session_missing_in_data"
    assert p.decisions["intervention_reason"].tolist().count("stop_triggered") == 2  # conditions
    assert p.decisions["stop_activation"].tolist().count(True) == 1  # one episode
    alerts = _paper_alerts(p)  # type: ignore[no-untyped-call]
    stops = [a for a in alerts if a.event_type == "RISK_STOP_TRIGGERED"]
    assert len(stops) == 1  # ADR-0034 D-A1: one per episode (2 before ADR-0034)
    assert p.final_state.account.shares == 0.0


# ── zero / invalid financial state: helpers ──

DD_SPEC = DrawdownSpec(enabled=True)


def zero_close_bars(last: float) -> pd.DataFrame:
    """FREE costs, fully invested at 100; the LAST close is `last` (0 or NaN)."""
    return make_bars([*ENTRY, (95, 96, 89, 90), (90, 91, 0.0, last)])


def paper_zero_close_replay(last: float) -> Any:
    bars = zero_close_bars(last)
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    engine = PaperTradingEngine(PaperConfig(risk=DD20, backtest=FREE))
    states = pd.Series([L] * 4, index=bars.index)
    return engine.replay(bars, states, flat_indicators(bars), times, strategy_id="x")


# ── zero / invalid financial state: ADR-0034 D-A5 specification (implemented in step 4a) ──


@pytest.mark.parametrize("equity", [-5.0, math.nan, math.inf, -math.inf])
def test_future_drawdown_monitor_rejects_invalid_equity(equity: float) -> None:
    with pytest.raises(ValueError):
        DrawdownMonitor(DD_SPEC, hwm=100.0).update(equity)


@pytest.mark.parametrize("hwm", [0.0, -10.0, math.nan, math.inf])
def test_future_drawdown_monitor_rejects_invalid_hwm(hwm: float) -> None:
    with pytest.raises(ValueError):
        DrawdownMonitor(DD_SPEC, hwm=hwm).update(50.0)


def test_spec_already_met_zero_equity_locks_without_error() -> None:
    """Equity 0 is valid and locks: met today at the monitor level (kept as a guard)."""
    m = DrawdownMonitor(DD_SPEC, hwm=100.0)
    m.update(0.0)
    assert m.state == LOCKED


def test_future_session_monitor_zero_previous_equity_is_not_computed() -> None:
    m = SessionMonitor(SessionSpec(enabled=True), last_equity=0.0)
    loss = m.update(10.0)  # no division, no substitute number
    assert loss is None and m.locked  # protection kept: increases blocked


def test_future_research_engine_zero_equity_position_is_zero() -> None:
    with np.errstate(all="raise"):  # any division by zero would surface
        r = run(zero_close_bars(0.0), [L] * 4, DD20)
    last = r.equity.iloc[-1]
    assert last["position"] == 0.0 and last["risk_state"] == LOCKED


def test_future_research_engine_nan_close_fails_explicitly() -> None:
    with pytest.raises(ValueError):
        run(zero_close_bars(math.nan), [L] * 4, DD20)


def test_future_paper_zero_equity_exposure_is_zero() -> None:
    p = paper_zero_close_replay(0.0)
    last = p.portfolio.iloc[-1]
    assert last["gross_exposure"] == 0.0 and last["risk_state"] == LOCKED


def test_future_paper_nan_close_fails_explicitly() -> None:
    with pytest.raises(ValueError):
        paper_zero_close_replay(math.nan)
