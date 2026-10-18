"""v2 agents: causal vectorised signals, their defining conditions, production == research,
time stops, and the v2 coordinator (approved agents only, conflicts, vetoes, ranking)."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.fx.agents import (
    MeanReversionAgent,
    MomentumAgent,
    PortfolioCoordinator,
    RegimeAgent,
    TrendAgent,
)
from app.fx.agents.base import MarketView
from app.fx.agents.signals import (
    DEFAULT_MEAN_REVERSION,
    DEFAULT_MOMENTUM,
    DEFAULT_TREND,
    features,
    mean_reversion_signals,
    momentum_signals,
    regime_frame,
    session_flags,
    trend_signals,
)
from app.fx.models import Bar, DataOrigin, Direction
from app.fx.proposals import AgentProposal, AgentRole, ProposalAction
from app.indicators.indicators import ema

# Wednesday 2026-10-14 15:00 UTC: London and New York both open
END = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)


def frame_from(closes: np.ndarray, end: datetime = END, wick: float = 0.0004) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range(end=end, periods=n, freq="15min", tz="UTC")
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, closes) + wick,
            "low": np.minimum(opens, closes) - wick,
            "close": closes,
        },
        index=idx,
    )


def random_walk(n: int = 900, seed: int = 1, drift: float = 0.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return 1.10 + np.cumsum(drift + rng.normal(0, 0.0006, n))


def uptrend(n: int = 900) -> np.ndarray:
    t = np.arange(n)
    return 1.10 + 0.0002 * t + 0.003 * np.sin(t / 6)  # a trend with regular pullbacks


def ranging(n: int = 900, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    x = 1.10 + 0.002 * np.sin(t / 6) + rng.normal(0, 0.0003, n)
    x[rng.choice(np.arange(300, n - 2), 25, replace=False)] -= 0.006  # sharp excursions
    return x


def view_of(
    df: pd.DataFrame, units: float = 0.0, age: int = 0, spread: float = 0.0001
) -> MarketView:
    bars = [
        Bar(
            "EURUSD",
            ts.to_pydatetime(),
            r.open,
            r.high,
            r.low,
            r.close,
            spread,
            DataOrigin.SYNTHETIC,
        )
        for ts, r in df.iterrows()
    ]
    return MarketView("EURUSD", "M15", bars, END, units, spread, age)


# --- causality -----------------------------------------------------------------------------


@pytest.mark.parametrize("fn", [trend_signals, momentum_signals, mean_reversion_signals])
def test_signals_are_causal_prefix_invariant(fn: object) -> None:
    df = frame_from(random_walk(900))
    full = fn(features(df))  # type: ignore[operator]
    k = 600
    prefix = fn(features(df.iloc[:k]))  # type: ignore[operator]
    pd.testing.assert_frame_equal(full.iloc[:k], prefix, check_dtype=False)


def test_session_flags() -> None:
    idx = pd.DatetimeIndex(
        ["2026-10-14 09:00", "2026-10-14 02:00", "2026-10-14 20:50", "2026-10-14 21:00"],
        tz="UTC",
    )
    f = session_flags(idx)
    assert f["liquid"].tolist() == [True, False, True, False]  # 20:50 UTC = 16:50 New York
    assert f["near_rollover"].tolist() == [False, False, True, True]


# --- defining conditions --------------------------------------------------------------------


def test_trend_signals_respect_their_rules() -> None:
    df = frame_from(uptrend())
    f = features(df)
    s = trend_signals(f, DEFAULT_TREND)
    c = f["close"]
    fast, slow, htf = ema(c, 20), ema(c, 50), ema(c, 200)
    longs = s.index[s["dir"] == 1]
    assert len(longs) > 0 and (s["dir"] == -1).sum() == 0  # an uptrend gives no shorts
    assert (c[longs] > htf[longs]).all() and (fast[longs] > slow[longs]).all()
    assert (c[longs] > fast[longs]).all()
    ratio = s.loc[longs, "target_dist"] / s.loc[longs, "stop_dist"]
    assert np.allclose(ratio, DEFAULT_TREND.target_atr / DEFAULT_TREND.stop_atr)


def test_momentum_signals_are_breakouts_with_expanding_volatility() -> None:
    df = frame_from(random_walk(1500, seed=4, drift=0.0001))
    f = features(df)
    s = momentum_signals(f, DEFAULT_MOMENTUM)
    hi = f["high"].rolling(DEFAULT_MOMENTUM.channel).max().shift(1)
    longs = s.index[s["dir"] == 1]
    assert len(longs) > 0
    assert (f.loc[longs, "close"] > hi[longs]).all()
    assert (f.loc[longs, "atr"] > f.loc[longs, "atr_sma"]).all()
    assert f.loc[longs, "rsi"].between(DEFAULT_MOMENTUM.rsi_min, DEFAULT_MOMENTUM.rsi_max).all()


def test_mean_reversion_only_in_ranges_after_confirmation_with_rr() -> None:
    df = frame_from(ranging())
    f = features(df)
    s = mean_reversion_signals(f, DEFAULT_MEAN_REVERSION)
    sig = s.index[s["dir"] != 0]
    assert len(sig) > 0
    assert (f.loc[sig, "er"] <= DEFAULT_MEAN_REVERSION.max_er).all()
    assert (
        s.loc[sig, "target_dist"] >= DEFAULT_MEAN_REVERSION.min_rr * s.loc[sig, "stop_dist"] - 1e-12
    ).all()


def test_regime_vetoes_illiquid_hours_and_wide_spreads() -> None:
    df = frame_from(random_walk(900))
    f = features(df, pd.Series(0.0001, index=df.index))
    r = regime_frame(f)
    assert r.loc[~f["tradable"], "veto"].all()
    wide = regime_frame(features(df, pd.Series(0.01, index=df.index)))
    assert wide["veto"].iloc[-1]


# --- production agents == research signals ------------------------------------------------


@pytest.mark.parametrize(
    ("agent", "fn", "closes"),
    [
        (TrendAgent(), trend_signals, uptrend(700)),
        (MomentumAgent(), momentum_signals, random_walk(700, seed=4, drift=0.0001)),
        (MeanReversionAgent(), mean_reversion_signals, ranging(700)),
    ],
)
def test_production_agent_uses_the_validated_signal(
    agent: object, fn: object, closes: np.ndarray
) -> None:
    df = frame_from(closes)
    sig = fn(features(df, pd.Series(0.0001, index=df.index)), agent.params)  # type: ignore[operator, attr-defined]
    # check the last 40 bars one by one, as the live agent would see them
    for k in range(len(df) - 40, len(df) + 1):
        window = df.iloc[:k]
        p = agent.analyze(view_of(window))  # type: ignore[attr-defined]
        row = sig.iloc[k - 1]
        if row["dir"] == 0:
            assert p.action is ProposalAction.HOLD
        else:
            assert p.action is ProposalAction.OPEN
            assert (p.direction is Direction.LONG) == (row["dir"] > 0)
            assert p.evidence["stop_dist"] == pytest.approx(row["stop_dist"], abs=1e-6)


def test_time_stop_and_insufficient_data() -> None:
    df = frame_from(uptrend(700))
    agent = TrendAgent()
    p = agent.analyze(view_of(df, units=10_000, age=DEFAULT_TREND.max_hold))
    assert p.action is ProposalAction.CLOSE and "tempo máximo" in p.rationale
    assert agent.analyze(view_of(df, units=10_000, age=1)).action is ProposalAction.HOLD
    short = agent.analyze(view_of(df.iloc[:100]))
    assert short.action is ProposalAction.HOLD and "insuficientes" in short.rationale


def test_regime_agent_vetoes_outside_liquid_hours() -> None:
    night = frame_from(random_walk(700), end=datetime(2026, 10, 14, 2, 0, tzinfo=UTC))
    p = RegimeAgent().analyze(view_of(night))
    assert p.action is ProposalAction.VETO and "horas líquidas" in p.rationale
    day = RegimeAgent().analyze(view_of(frame_from(random_walk(700))))
    assert day.evidence["tradable_hours"] is True


# --- coordinator v2 ---------------------------------------------------------------------------

NOW = END


def prop(
    role: AgentRole,
    action: ProposalAction,
    d: Direction,
    score: float = 0.7,
    regime: str | None = None,
) -> AgentProposal:
    stop = 1.07 if d is Direction.LONG else 1.09 if d is Direction.SHORT else None
    tp = 1.10 if d is Direction.LONG else 1.06 if d is Direction.SHORT else None
    ev = {"regime": regime} if regime else {}
    return AgentProposal(
        f"{role.value}-1",
        role,
        "2.0.0",
        "EURUSD",
        "M15",
        NOW,
        action,
        d,
        score,
        1.08,
        stop,
        tp,
        None,
        "t",
        ev,
    )


def regime(veto: bool = False) -> AgentProposal:
    return prop(
        AgentRole.REGIME,
        ProposalAction.VETO if veto else ProposalAction.HOLD,
        Direction.FLAT,
        0.5,
        "trending",
    )


OPEN, HOLD, CLOSE = ProposalAction.OPEN, ProposalAction.HOLD, ProposalAction.CLOSE
L, S, F = Direction.LONG, Direction.SHORT, Direction.FLAT
ALL = frozenset({"trend", "momentum", "mean_reversion"})


def test_coordinator_only_approved_agents_open() -> None:
    props = [prop(AgentRole.TREND, OPEN, L)]
    none = PortfolioCoordinator(approved=frozenset()).decide_pair(
        "EURUSD", regime(), props, 0, None
    )
    assert none.action is HOLD and "observação" in none.summary
    yes = PortfolioCoordinator(approved=frozenset({"trend"})).decide_pair(
        "EURUSD", regime(), props, 0, None
    )
    assert yes.action is OPEN and yes.lead_agent is AgentRole.TREND
    assert yes.request is not None and yes.request.stop_loss == 1.07 and yes.request.score == 0.7


def test_coordinator_conflict_veto_and_strongest_lead() -> None:
    c = PortfolioCoordinator(approved=ALL)
    conflict = [prop(AgentRole.TREND, OPEN, L), prop(AgentRole.MEAN_REVERSION, OPEN, S)]
    assert c.decide_pair("EURUSD", regime(), conflict, 0, None).action is HOLD
    assert (
        c.decide_pair("EURUSD", regime(veto=True), [prop(AgentRole.TREND, OPEN, L)], 0, None).action
        is ProposalAction.VETO
    )
    both = [prop(AgentRole.TREND, OPEN, L, 0.6), prop(AgentRole.MOMENTUM, OPEN, L, 0.9)]
    d = c.decide_pair("EURUSD", regime(), both, 0, None)
    assert d.lead_agent is AgentRole.MOMENTUM and d.score == 0.9
    weak = [prop(AgentRole.TREND, OPEN, L, 0.4)]
    assert c.decide_pair("EURUSD", regime(), weak, 0, None).action is HOLD


def test_coordinator_closes_only_on_the_owners_request() -> None:
    c = PortfolioCoordinator(approved=ALL)
    others = [prop(AgentRole.TREND, HOLD, F), prop(AgentRole.MOMENTUM, OPEN, S, 0.95)]
    keep = c.decide_pair("EURUSD", regime(), others, 10_000, AgentRole.TREND)
    assert keep.action is HOLD  # a strong opposite signal is not a validated exit
    owner = [prop(AgentRole.TREND, CLOSE, F)]
    d = c.decide_pair("EURUSD", regime(veto=True), owner, 10_000, AgentRole.TREND)
    assert d.action is CLOSE and d.request is not None and d.request.direction is F


def test_rank_puts_crowded_currencies_last_without_lowering_scores() -> None:
    c = PortfolioCoordinator(approved=ALL, max_new_entries_per_cycle=1)
    eur = c.decide_pair("EURUSD", regime(), [prop(AgentRole.TREND, OPEN, L, 0.9)], 0, None)
    gbp = c.decide_pair("GBPUSD", regime(), [prop(AgentRole.TREND, OPEN, L, 0.6)], 0, None)
    gbp.pair = "GBPUSD"
    chosen = c.rank([eur, gbp], exposure={"EUR": 20_000.0, "JPY": -20_000.0})
    assert [d.pair for d in chosen] == ["GBPUSD"]
    assert eur.request is not None and eur.request.score == 0.9  # never lowered


def test_unapproved_agents_trade_only_in_a_synthetic_demo(tmp_path: object) -> None:
    from pathlib import Path

    from tests.unit.fx.helpers import orchestrator, small_config

    base = orchestrator(Path(str(tmp_path)) / "a")
    assert base.coordinator.approved == frozenset()  # nothing approved before validation
    cfg = small_config()
    demo = cfg.model_copy(
        update={"simulation": cfg.simulation.model_copy(update={"allow_unapproved_agents": True})}
    )
    o = orchestrator(Path(str(tmp_path)) / "b", demo)
    assert o.coordinator.approved == ALL


def test_positions_go_flat_before_the_weekend(tmp_path: object) -> None:
    from pathlib import Path

    from app.fx.risk import TradeRequest
    from tests.unit.fx.helpers import orchestrator

    o = orchestrator(Path(str(tmp_path)))
    q = o.market.quotes["EURUSD"]
    o.process(
        TradeRequest("EURUSD", L, round(q.mid - 0.003, 5), round(q.mid + 0.006, 5), 0.9, "w-1"),
        "trend",
    )
    assert o.broker.positions()
    friday = datetime(2026, 10, 16, 20, 50, tzinfo=UTC)  # 16:50 New York, Friday
    assert o._before_weekend(friday) and not o._before_weekend(friday - timedelta(hours=1))
    o.market.now = friday - timedelta(minutes=1)
    o.tick()
    assert o.broker.positions() == []
    assert any(e["kind"] == "flatten_all" for e in o.store.recent(50))
