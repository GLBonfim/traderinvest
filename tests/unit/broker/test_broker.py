"""Broker SANDBOX integration without any network: endpoint rejection, credential secrecy,
adapter mapping, every executor gate, idempotency and restart recovery, reconciliation, and the
safety boundary (no manual order surface, no configurable endpoint, no production path)."""

import ast
import inspect
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from app.backtest.config import BacktestConfig
from app.backtest.engine import session_times
from app.broker import alpaca as alpaca_mod
from app.broker.alpaca import AlpacaPaperGateway, _NoRedirect
from app.broker.config import (
    CAPITAL_REFERENCE_PRICE_UNGUARANTEED,
    DAY_FRACTIONAL,
    OPG_WHOLE_SHARES,
    SANDBOX_BASE_URL,
    BrokerConfig,
)
from app.broker.executor import SandboxExecutor, target_quantity
from app.broker.gateway import (
    BrokerAccount,
    BrokerOrder,
    BrokerPosition,
    BrokerRequestError,
    BrokerSafetyError,
    OrderRequest,
    ProductionEndpointError,
    assert_sandbox_url,
)
from app.broker.reconcile import reconcile
from app.broker.store import BrokerStore
from app.core.config import Settings, TradingMode
from app.core.safety import RealMoneyExecutionBlockedError, refuse_real_money_order
from app.dashboard.services import paper as paper_svc
from app.data.calendar import TradingCalendar
from app.paper.engine import build_inputs
from tests.unit.risk.test_overlay import flat_indicators, make_bars

REPO = Path(__file__).resolve().parents[3]
KEY, SECRET = "PKTESTKEYID000", "sandbox-secret-value-123"


class Crash(BaseException):
    pass


# ── endpoint safety ──


@pytest.mark.parametrize(
    "url",
    [
        "https://paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets/v2/orders?client_order_id=x",
        "https://PAPER-API.alpaca.markets/v2/account",
    ],
)
def test_sandbox_url_accepted(url: str) -> None:
    assert assert_sandbox_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://api.alpaca.markets",
        "https://api.alpaca.markets/v2/orders",
        "http://paper-api.alpaca.markets/v2/orders",
        "https://paper-api.alpaca.markets.evil.example/v2/orders",
        "https://evil.example/paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets:8443/v2/orders",
        "https://user:pw@paper-api.alpaca.markets/v2/orders",
        "https://data.alpaca.markets/v2/stocks",
        "https://broker-api.alpaca.markets/v1/accounts",
    ],
)
def test_every_other_endpoint_is_rejected(url: str) -> None:
    with pytest.raises(ProductionEndpointError):
        assert_sandbox_url(url)


def test_production_host_rejected_explicitly() -> None:
    with pytest.raises(ProductionEndpointError, match=r"api.alpaca.markets is a production"):
        assert_sandbox_url("https://api.alpaca.markets/v2/account")


def test_no_way_to_configure_an_endpoint() -> None:
    params = inspect.signature(AlpacaPaperGateway).parameters
    assert set(params) == {"key_id", "secret", "transport", "timeout"}
    assert "url" not in " ".join(f.name for f in BrokerConfig.__dataclass_fields__.values())
    src = "\n".join(p.read_text(encoding="utf-8") for p in (REPO / "app" / "broker").glob("*.py"))
    assert 'BASE_URL")' not in src and 'environ.get("ALPACA_API_BASE' not in src
    assert src.count('"api.alpaca.markets"') == 1  # only the named, rejected constant


# ── adapter (fake transport; no network) ──


class FakeHTTP:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str], Any]] = []
        self.routes: dict[tuple[str, str], tuple[int, Any]] = {}

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
    ) -> tuple[int, bytes]:
        assert url.startswith(SANDBOX_BASE_URL + "/")
        self.calls.append((method, url, headers, None if body is None else json.loads(body)))
        path = url[len(SANDBOX_BASE_URL) :].split("?")[0]
        status, data = self.routes.get((method, path), (404, {"message": "not found"}))
        return status, json.dumps(data).encode()


ORDER_JSON = {
    "id": "b-1",
    "client_order_id": "qp-a-D1",
    "symbol": "SPY",
    "side": "buy",
    "qty": "12",
    "status": "accepted",
    "filled_qty": "0",
    "filled_avg_price": None,
    "submitted_at": "2026-10-09T00:00:00Z",
    "filled_at": None,
}


def test_adapter_requests_and_mapping() -> None:
    http = FakeHTTP()
    http.routes[("POST", "/v2/orders")] = (200, ORDER_JSON)
    http.routes[("GET", "/v2/account")] = (
        200,
        {
            "account_number": "PA1",
            "status": "ACTIVE",
            "currency": "USD",
            "cash": "100000",
            "equity": "100000",
            "buying_power": "200000",
        },
    )
    gw = AlpacaPaperGateway(KEY, SECRET, transport=http)
    o = gw.submit(OrderRequest("qp-a-D1", "SPY", "buy", 12.0, "opg"))
    assert o.broker_order_id == "b-1" and o.status == "accepted" and o.qty == 12
    method, url, headers, body = http.calls[-1]
    assert (method, url) == ("POST", f"{SANDBOX_BASE_URL}/v2/orders")
    assert body == {
        "symbol": "SPY",
        "qty": "12",
        "side": "buy",
        "type": "market",
        "time_in_force": "opg",
        "client_order_id": "qp-a-D1",
    }
    assert headers["APCA-API-KEY-ID"] == KEY and headers["APCA-API-SECRET-KEY"] == SECRET
    assert gw.account().equity == 100000.0
    assert gw.position("SPY") is None  # 404 -> no position
    assert gw.order_by_client_id("missing") is None
    assert gw.submit.__name__ == "submit" and not hasattr(gw, "cancel")


def test_adapter_errors_secrets_and_ambiguity() -> None:
    http = FakeHTTP()
    http.routes[("POST", "/v2/orders")] = (403, {"message": "forbidden"})
    gw = AlpacaPaperGateway(KEY, SECRET, transport=http)
    with pytest.raises(BrokerRequestError) as e:
        gw.submit(OrderRequest("x", "SPY", "buy", 1, "opg"))
    assert e.value.status == 403 and SECRET not in str(e.value) and KEY not in str(e.value)
    assert SECRET not in repr(gw) and KEY not in repr(gw)

    def down(*a: Any) -> tuple[int, bytes]:
        raise TimeoutError("timed out")

    with pytest.raises(BrokerRequestError) as e2:
        AlpacaPaperGateway(KEY, SECRET, transport=down).submit(
            OrderRequest("x", "SPY", "buy", 1, "opg")
        )
    assert e2.value.ambiguous  # a POST that may have reached the broker
    with pytest.raises(BrokerSafetyError):
        AlpacaPaperGateway.from_env({})
    with pytest.raises(BrokerSafetyError, match="128"):
        AlpacaPaperGateway(KEY, SECRET, transport=http).submit(
            OrderRequest("x" * 129, "SPY", "buy", 1, "opg")
        )
    assert _NoRedirect().redirect_request() is None  # redirects are never followed


# ── executor ──


class FakeGateway:
    name, environment = "fake", "sandbox"

    def __init__(self) -> None:
        self.positions: dict[str, float] = {}
        self.orders: dict[str, BrokerOrder] = {}
        self.submits: list[OrderRequest] = []
        self.crash_after_post = False
        self.crash_before_post = False
        self.acct = BrokerAccount(
            "PA1", "ACTIVE", "USD", 100_000.0, 100_000.0, 400_000.0, False, False
        )

    def account(self) -> BrokerAccount:
        return self.acct

    def position(self, symbol: str) -> BrokerPosition | None:
        q = self.positions.get(symbol)
        return None if q is None else BrokerPosition(symbol, q, None, None)

    def order_by_client_id(self, cid: str) -> BrokerOrder | None:
        return self.orders.get(cid)

    def submit(self, r: OrderRequest) -> BrokerOrder:
        if self.crash_before_post:
            self.crash_before_post = False
            raise Crash("crash before the request left")
        self.submits.append(r)
        o = BrokerOrder(
            f"b-{len(self.submits)}",
            r.client_order_id,
            r.symbol,
            r.side,
            r.qty,
            "accepted",
            0.0,
            None,
            None,
            None,
        )
        self.orders[r.client_order_id] = o
        if self.crash_after_post:
            self.crash_after_post = False
            raise Crash("crash after the broker accepted")
        return o


CAL = TradingCalendar("XNYS")
# Submission mechanics are tested with market-on-open orders and the explicit, non-default
# capital policy; the defaults (limit-on-open buys, block_unbounded) are tested in
# test_opg_and_capital.py and test_loo.py.
MECH = BrokerConfig(
    order_policy=OPG_WHOLE_SHARES, capital_policy=CAPITAL_REFERENCE_PRICE_UNGUARANTEED
)


def account_with_pending_entry(root: Path, states: list[str] | None = None) -> tuple[str, datetime]:
    """Paper account whose last decision (close of Mon 2026-10-05) is pending for the open of
    Tue 2026-10-06; returns (account id, a time inside the OPG submission window)."""
    states = states or ["FLAT"] * 5 + ["LONG"]
    bars = make_bars([(500, 501, 499, 500)] * len(states), start=date(2026, 9, 28))
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    inputs = build_inputs(bars, pd.Series(states, index=bars.index), flat_indicators(bars), times)
    st = paper_svc.paper_create_account(
        root,
        strategy_id="sma_trend",
        strategy_version="1.0.0",
        scenario_name="control_no_overlay",
        instrument="SPY",
    )
    st.process_many(inputs)
    last_close = pd.Timestamp(times["observed_at"].iloc[-1]).to_pydatetime()
    # 16:00 ET close + 4 h = 20:00 ET: after the 19:00 ET OPG queue opening, before the next open
    return st.account_id, last_close + timedelta(hours=4)


def settings(mode: str = "paper") -> Settings:
    s = Settings(_env_file=None, postgres_password="x")  # type: ignore[call-arg]
    object.__setattr__(s, "trading_mode", TradingMode(mode))
    return s


def executor(
    tmp: Path,
    gw: FakeGateway | None,
    now: datetime,
    *,
    mode: str = "paper",
    armed: bool = True,
    cfg: BrokerConfig | None = None,
) -> SandboxExecutor:
    store = BrokerStore(tmp / "broker")
    acct = next(p.name for p in (tmp / "paper").iterdir())
    if acct not in store.linked_accounts:
        store.link(acct)
    store.set_armed(armed)
    return SandboxExecutor(
        cfg or MECH, store, settings(mode), tmp / "paper", gw, clock=lambda: now, calendar=CAL
    )


def outcomes(rep: Any) -> list[str]:
    return [i.outcome for i in rep.items]


def test_default_state_is_unarmed_and_unlinked(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    store = BrokerStore(tmp_path / "broker")
    assert not store.armed and not store.kill_switch and store.linked_accounts == []
    gw = FakeGateway()
    rep = SandboxExecutor(
        BrokerConfig(), store, settings(), tmp_path / "paper", gw, clock=lambda: now
    ).sync(dry_run=False)
    assert rep.items == [] and gw.submits == []  # nothing linked -> nothing derived


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"armed": False}, "not_armed"),
        ({"mode": "disabled"}, "trading_mode_disabled_is_not_paper"),
    ],
)
def test_gates_block_without_sending(tmp_path: Path, kw: dict[str, Any], reason: str) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    gw = FakeGateway()
    rep = executor(tmp_path, gw, now, **kw).sync(dry_run=False)
    assert outcomes(rep) == ["blocked"] and rep.items[0].detail == reason and gw.submits == []


def test_missing_credentials_and_kill_switch(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    rep = executor(tmp_path, None, now).sync(dry_run=False)
    assert rep.items[0].detail == "sandbox_credentials_missing"
    gw = FakeGateway()
    ex = executor(tmp_path, gw, now)
    ex.store.set_kill_switch(True, "test")
    rep = ex.sync(dry_run=False)
    assert rep.items[0].detail == "kill_switch_engaged" and gw.submits == []
    assert [a.event_type for a in rep.alerts] == ["BROKER_ORDER_BLOCKED"]
    assert all("no real money" in a.message for a in rep.alerts)


def test_dry_run_then_single_submission_and_idempotent_resync(tmp_path: Path) -> None:
    acct, now = account_with_pending_entry(tmp_path / "paper")
    gw = FakeGateway()
    dry = executor(tmp_path, gw, now).sync(dry_run=True)
    assert outcomes(dry) == ["would_submit"] and gw.submits == []
    order = dry.items[0].order
    assert order is not None
    assert order["time_in_force"] == "opg" and order["qty"] == 20.0  # floor(1 x 10,000 / 500)
    assert order["client_order_id"].startswith(f"qp-{acct}-D")
    rep = executor(tmp_path, gw, now).sync(dry_run=False)
    assert outcomes(rep) == ["submitted"] and len(gw.submits) == 1
    assert [a.event_type for a in rep.alerts] == ["BROKER_ORDER_SUBMITTED"]
    again = executor(tmp_path, gw, now).sync(dry_run=False)  # restart + same instruction
    assert outcomes(again) == ["already_submitted"] and len(gw.submits) == 1


def test_crash_after_post_is_recovered_without_a_second_order(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    gw = FakeGateway()
    gw.crash_after_post = True
    with pytest.raises(Crash):
        executor(tmp_path, gw, now).sync(dry_run=False)
    rep = executor(tmp_path, gw, now).sync(dry_run=False)  # restart
    assert len(gw.submits) == 1 and outcomes(rep) == ["already_submitted"]
    assert rep.reconciled and rep.reconciled[0]["status"] == "accepted"


def test_crash_before_post_retries_once_with_the_same_id(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    gw = FakeGateway()
    gw.crash_before_post = True
    with pytest.raises(Crash):
        executor(tmp_path, gw, now).sync(dry_run=False)
    rep = executor(tmp_path, gw, now).sync(dry_run=False)
    assert outcomes(rep) == ["submitted"] and len(gw.submits) == 1
    assert executor(tmp_path, gw, now).sync(dry_run=False).items[0].outcome == "already_submitted"


def test_stale_instruction_is_never_sent(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    gw = FakeGateway()
    rep = executor(tmp_path, gw, now + timedelta(days=2)).sync(dry_run=False)
    assert outcomes(rep) == ["no_pending_instruction"] and gw.submits == []


def test_caps_symbol_exit_and_no_short(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    gw = FakeGateway()
    small = replace(MECH, max_order_notional=1_000.0)
    assert (
        executor(tmp_path, gw, now, cfg=small)
        .sync(dry_run=True)
        .items[0]
        .detail.startswith("notional")
    )
    other = replace(MECH, allowed_symbols=("QQQ",))
    assert executor(tmp_path, gw, now, cfg=other).sync(dry_run=True).items[0].detail == (
        "symbol_not_allowed"
    )
    gw.positions["SPY"] = 20.0  # already at target -> nothing to send
    assert outcomes(executor(tmp_path, gw, now).sync(dry_run=True)) == ["no_order_required"]


def test_exit_sells_exactly_the_held_quantity(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper", ["LONG"] * 5 + ["FLAT"])
    gw = FakeGateway()
    gw.positions["SPY"] = 13.0
    rep = executor(tmp_path, gw, now).sync(dry_run=True)
    assert rep.items[0].order is not None
    assert (rep.items[0].order["side"], rep.items[0].order["qty"]) == ("sell", 13.0)
    gw.positions.pop("SPY")
    assert outcomes(executor(tmp_path, gw, now).sync(dry_run=True)) == ["no_order_required"]


def test_order_policies() -> None:
    assert target_quantity(0.5, 10_000, 333.0, "opg_whole_shares") == 15.0
    assert target_quantity(0.5, 10_000, 333.0, DAY_FRACTIONAL) == pytest.approx(15.015015015)
    assert target_quantity(-1, 10_000, 100.0, "opg_whole_shares") == 0.0


def test_reconciliation(tmp_path: Path) -> None:
    acct, now = account_with_pending_entry(tmp_path / "paper", ["LONG"] * 6)
    store = BrokerStore(tmp_path / "broker")
    store.link(acct)
    gw = FakeGateway()
    gw.positions["SPY"] = 20.0  # local exposure ~1.0 at 500 -> expected 19 or 20 shares
    rec = reconcile(MECH, store, gw, tmp_path / "paper", now)  # type: ignore[arg-type]
    assert rec.accounts[0]["status"] == "ok" and rec.mismatches == 0
    gw.positions["SPY"] = 3.0
    rec2 = reconcile(MECH, store, gw, tmp_path / "paper", now)  # type: ignore[arg-type]
    assert rec2.mismatches == 1 and rec2.alerts[0].event_type == "BROKER_RECONCILIATION_MISMATCH"


def test_store_records_contain_no_credentials(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    executor(tmp_path, FakeGateway(), now).sync(dry_run=False)
    blob = "".join(p.read_text() for p in (tmp_path / "broker").iterdir())
    assert KEY not in blob and SECRET not in blob and "APCA" not in blob


# ── safety boundary ──


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            out.add(node.module or "")
    return out


def test_only_the_adapter_touches_the_network_and_no_vendor_sdk() -> None:
    for f in sorted((REPO / "app").rglob("*.py")):
        roots = {i.split(".")[0] for i in _imports(f)}
        assert not roots & {"alpaca", "alpaca_trade_api", "ib_insync", "ibapi", "ccxt"}, f
        rel = f.relative_to(REPO).as_posix()
        if rel.startswith("app/broker/"):
            net = {
                i
                for i in _imports(f)
                if i.startswith(("urllib.request", "urllib.error", "http", "socket", "requests"))
            }
            assert not net or rel == "app/broker/alpaca.py", rel


def test_orders_are_only_built_from_paper_decisions() -> None:
    calls = []
    for f in sorted((REPO / "app").rglob("*.py")):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "submit"
                and "gateway" in ast.unparse(node.func)
            ):
                calls.append(f.relative_to(REPO).as_posix())
    assert set(calls) == {"app/broker/executor.py"}
    public = {
        n
        for n, _ in inspect.getmembers(SandboxExecutor, inspect.isfunction)
        if not n.startswith("_")
    }
    assert public == {"sync"}  # no manual-order method


def test_trading_modes_and_blocker_unchanged() -> None:
    assert {m.value for m in TradingMode} == {"disabled", "paper"}
    with pytest.raises(RealMoneyExecutionBlockedError):
        refuse_real_money_order()
    assert alpaca_mod.AlpacaPaperGateway.environment == "sandbox"
    assert (
        replace(BrokerConfig(), allocated_capital=1.0, order_cost_allowance=0.0).vendor == "alpaca"
    )
    assert BacktestConfig().initial_capital == 100_000.0
