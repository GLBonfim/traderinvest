"""Dashboard paper-account service (local simulation only) and the safety boundary."""

import ast
import json
import re
import tomllib
from pathlib import Path

import pandas as pd
import pytest

from app.core.config import TradingMode
from app.core.safety import RealMoneyExecutionBlockedError, refuse_real_money_order
from app.dashboard import charts
from app.dashboard.config import PAPER_LABEL, REPO_ROOT
from app.dashboard.services import paper
from app.paper.engine import PaperTradingEngine, build_inputs
from app.paper.trader import BarInput
from tests.unit.paper.test_incremental import CAL, OHLCV
from tests.unit.strategies.helpers import session_walk, small_engine

DASH = REPO_ROOT / "app" / "dashboard"


@pytest.fixture(scope="module")
def inputs() -> list[BarInput]:
    from app.backtest.engine import session_times
    from app.indicators.engine import IndicatorEngine

    bars = session_walk(200, seed=41)
    sig = small_engine().run(bars).signals
    g = sig[sig["strategy_id"] == "rsi_momentum"]
    st = pd.Series(g["state"].to_numpy(), index=pd.DatetimeIndex(g["bar_ts"]))
    return build_inputs(
        bars,
        st,
        IndicatorEngine().analyze(bars[OHLCV]).values,
        session_times(pd.DatetimeIndex(bars.index), CAL),
    )


def create(root: Path, scenario: str = "volatility_target_10"):  # type: ignore[no-untyped-def]
    return paper.paper_create_account(
        root,
        strategy_id="rsi_momentum",
        strategy_version="1.0.0",
        scenario_name=scenario,
        instrument="SPY",
    )


def test_account_view_matches_the_paper_engine(tmp_path: Path, inputs: list[BarInput]) -> None:
    store = create(tmp_path)
    counts = paper.paper_process_sessions(store, inputs)
    assert counts["processed"] == len(inputs)
    (ref,) = paper.list_accounts(tmp_path)
    view = paper.account_view(paper.open_account(ref, tmp_path))
    run = PaperTradingEngine(paper.paper_config("volatility_target_10")).replay_inputs(
        inputs, strategy_id="rsi_momentum"
    )
    last = run.portfolio.iloc[-1]
    for k in ("cash", "equity", "realized_pnl", "unrealized_pnl", "total_pnl", "cumulative_costs"):
        assert view[k] == last[k], k
    assert view["market_value"] == last["position_market_value"]
    assert view["exposure"] == last["gross_exposure"]
    assert view["position_quantity"] == last["position_quantity"]
    if view["position_quantity"] > 0:
        assert view["average_cost"] == last["cost_basis"] / last["position_quantity"]
    assert view["approved_exposure"] == run.decisions["risk_approved_target"].iloc[-1]
    assert view["ledger_events"] == len(run.events)
    assert view["ledger_last_hash"] == run.events[-1]["hash"]
    tables = paper.ledger_tables(paper.open_account(ref, tmp_path), last_n_sessions=5)
    assert set(tables) == set(paper.LEDGER_TYPES) and len(tables["decision"]) == 5
    assert tables["decision"]["seq"].is_monotonic_decreasing  # newest first


def test_processing_is_idempotent_and_create_is_not_repeated(
    tmp_path: Path, inputs: list[BarInput]
) -> None:
    store = create(tmp_path)
    paper.paper_process_sessions(store, inputs[:100])
    again = paper.paper_process_sessions(store, inputs)
    assert again == {"processed": len(inputs) - 100, "already_processed": 100, "recovered": 0}
    assert paper.paper_process_sessions(store, inputs)["processed"] == 0
    with pytest.raises(paper.AccountError, match="already exists"):
        create(tmp_path)


def test_corrupt_ledger_and_unknown_accounts_fail_clearly(
    tmp_path: Path, inputs: list[BarInput]
) -> None:
    store = create(tmp_path)
    paper.paper_process_sessions(store, inputs[:40])
    (ref,) = paper.list_accounts(tmp_path)
    lines = store.ledger_path.read_text().splitlines()
    ev = json.loads(lines[3])
    ev["record"]["equity"] = 1e9
    lines[3] = json.dumps(ev, sort_keys=True)
    store.ledger_path.write_text("\n".join(lines) + "\n")
    with pytest.raises(paper.AccountError, match="hash chain"):
        paper.open_account(ref, tmp_path)
    missing = paper.AccountRef(
        "nope", "x", "1", "control_no_overlay", "SPY", "1d", "", tmp_path / "nope"
    )
    with pytest.raises(paper.AccountError, match="no state file"):
        paper.open_account(missing, tmp_path)
    assert not (tmp_path / "nope").exists()  # opening never creates
    with pytest.raises(paper.AccountError, match="unknown risk scenario"):
        paper.paper_config("live")


def test_list_accounts_is_read_only(tmp_path: Path) -> None:
    root = tmp_path / "accounts"
    assert paper.list_accounts(root) == [] and not root.exists()


# ── safety boundary ──

FORBIDDEN = {
    "requests",
    "httpx",
    "httpx2",
    "urllib",
    "urllib3",
    "http",
    "socket",
    "ssl",
    "aiohttp",
    "websocket",
    "websockets",
    "grpc",
    "yfinance",
    "alpaca",
    "alpaca_trade_api",
    "ib_insync",
    "ib_async",
    "ibapi",
    "ccxt",
    "anthropic",
    "openai",
    "google",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


def test_dashboard_has_no_network_broker_or_llm_code() -> None:
    files = sorted(DASH.rglob("*.py"))
    assert len(files) >= 10
    for f in files:
        roots = {n.split(".")[0] for n in _imports(f)}
        assert not roots & FORBIDDEN, f
        assert "app.data.providers" not in " ".join(_imports(f)), f  # never fetches data
        assert "app.data.ingestion" not in " ".join(_imports(f)), f


def test_services_are_streamlit_free() -> None:
    for f in (DASH / "services").rglob("*.py"):
        assert not {n.split(".")[0] for n in _imports(f)} & {"streamlit", "plotly"}, f


def test_no_live_trading_concepts_in_the_ui() -> None:
    text = "\n".join(p.read_text(encoding="utf-8") for p in DASH.rglob("*.py")).lower()
    for phrase in (
        "go live",
        "trade real money",
        "live account",
        "broker account",
        "real order",
        "tradingmode.live",
        "api_key",
        "place_order",
        "submit_order",
    ):
        assert phrase not in text, phrase


def test_every_mutating_control_is_labelled_paper_simulation() -> None:
    src = (DASH / "ui" / "pages.py").read_text(encoding="utf-8")
    buttons = re.findall(r"\.button\((.*?)\)", src, re.S)  # st.button and column.button
    mutating = [b for b in buttons if "pp_create" in b or "pp_process" in b]
    assert len(mutating) == 2
    assert all("PAPER_LABEL" in b for b in mutating)
    assert PAPER_LABEL == "PAPER SIMULATION"
    # the only mutating service functions are the two paper_* actions
    tree = ast.parse((DASH / "services" / "paper.py").read_text(encoding="utf-8"))
    funcs = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {f for f in funcs if f.startswith("paper_")} == {
        "paper_config",
        "paper_create_account",
        "paper_process_sessions",
    }


def test_trading_modes_and_blocker_unchanged() -> None:
    assert {m.value for m in TradingMode} == {"disabled", "paper"}
    with pytest.raises(RealMoneyExecutionBlockedError):
        refuse_real_money_order()


def test_streamlit_server_is_local_and_silent() -> None:
    cfg = tomllib.loads((REPO_ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert cfg["server"]["address"] == "127.0.0.1"
    assert cfg["browser"]["gatherUsageStats"] is False


def test_entry_point_checks_the_trading_mode() -> None:
    src = (DASH / "streamlit_app.py").read_text(encoding="utf-8")
    assert "assert_safe_trading_mode(get_settings())" in src


def test_charts_plot_only_what_they_are_given() -> None:
    bars = session_walk(30, seed=1)
    swings = pd.DataFrame(
        {
            "kind": ["high"],
            "pivot_ts": [bars.index[5]],
            "price": [101.0],
            "label": ["HH"],
            "confirmed_at": [bars.index[9]],
        }
    )
    fig = charts.candlestick_figure(bars, swings=swings)
    marks = [t for t in fig.data if t.name and t.name.startswith("confirmed swing")]
    assert sum(len(t.x) for t in marks) == 1
    assert charts.candlestick_figure(bars).data[0].type == "candlestick"
