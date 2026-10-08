"""Streamlit pages. Thin: every number comes from `app.dashboard.services` (engine outputs);
pages only select, format and plot. Mutating controls exist only on the Paper Trading page and
are LOCAL PAPER SIMULATION actions."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
import streamlit as st

from app.backtest.engine import BENCHMARK_PRICE, BENCHMARK_TOTAL
from app.core.config import get_settings
from app.dashboard import charts
from app.dashboard.config import DISCLAIMERS, PAPER_LABEL, PAPER_ROOT, RESEARCH_ROOT
from app.dashboard.services import alerts as alerts_svc
from app.dashboard.services import analysis, explain, health, market, paper, research
from app.dashboard.services import operations as ops_svc
from app.dashboard.ui.context import CONFIG_KEY, Context, cached_paper_inputs, cached_risk_table
from app.database.session import get_engine, get_session_factory
from app.risk.config import SCENARIOS

# ── formatting helpers ──


def num(v: Any, fmt: str = "{:,.2f}") -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return fmt.format(v)


def pct(v: Any, signed: bool = False) -> str:
    return num(v, "{:+.2%}" if signed else "{:.2%}")


def note(key: str) -> None:
    st.info(DISCLAIMERS[key])


def window(bars: pd.DataFrame, end: pd.Timestamp, sessions: int) -> pd.DataFrame:
    upto = bars.loc[:end]
    return upto.iloc[-sessions:]


RANGES = {
    "3 months": 63,
    "6 months": 126,
    "1 year": 252,
    "3 years": 756,
    "10 years": 2520,
    "All": 10**9,
}


def range_picker(key: str, default: str = "1 year") -> int:
    choice = st.segmented_control("Visible range", list(RANGES), default=default, key=key)
    return RANGES[str(choice or default)]


def paper_account_for(strategy_id: str, scenario: str) -> dict[str, Any] | None:
    for ref in paper.list_accounts(PAPER_ROOT):
        if ref.strategy_id == strategy_id and ref.scenario == scenario:
            try:
                return paper.account_view(paper.open_account(ref, PAPER_ROOT))
            except paper.AccountError:
                return None
    return None


# ── 1. Market Overview ──


def market_overview(ctx: Context) -> None:
    st.title("Market Overview")
    ov = market.market_overview(ctx.bars)
    fresh = market.freshness(ctx.ident.last_ts, datetime.now(UTC))
    c = st.columns(6)
    c[0].metric("Latest completed session", str(ov["session"]))
    c[1].metric(
        "Close (raw)",
        num(ov["close"]),
        f"{ov['change']:+.2f} ({ov['change_pct']:+.2%})",
        delta_color="off",
    )
    c[2].metric("Previous close", num(ov["previous_close"]))
    c[3].metric("Open / High / Low", f"{ov['open']:.2f} / {ov['high']:.2f} / {ov['low']:.2f}")
    c[4].metric("Volume", num(ov["volume"], "{:,.0f}"))
    c[5].metric("Exchange status (calendar)", fresh.market_status.split(" (")[0])
    st.caption(f"Exchange calendar: {fresh.market_status} · checked {fresh.now:%Y-%m-%d %H:%M} UTC")
    if fresh.stale:
        st.warning(
            f"**Stale data.** The last stored session is {fresh.last_bar_session}; the latest "
            f"completed XNYS session is {fresh.expected_session} ({fresh.missing_sessions} "
            "session(s) not ingested). The dashboard never fetches data: run the ingestion CLI."
        )
    else:
        st.success(
            f"Data is current through the latest completed session ({fresh.expected_session})."
        )
    with get_session_factory()() as s:
        ing = market.latest_ingestion(s, ctx.ident.instrument_id)
    d = st.columns(4)
    d[0].metric("Dataset", f"{ov['first_session']} → {ov['session']}")
    d[1].metric("Bars", f"{ov['bars']:,}")
    d[2].metric("Provider", ctx.ident.provider)
    d[3].metric(
        "Last ingestion", "—" if ing is None else f"{ing['finished_at']:%Y-%m-%d %H:%M} UTC"
    )
    if ing is not None:
        st.caption(
            f"Ingestion run {ing['run_id']}: {ing['status']}, {ing['provider_version']}, "
            f"inserted {ing['bars_inserted']}, issues {ing['issues_count']}."
        )

    n = range_picker("ov_range")
    w = window(ctx.bars, ctx.bars.index[-1], n)
    st.plotly_chart(
        charts.candlestick_figure(w, marker_ts=ctx.ts, title="SPY daily (raw OHLC)"),
        width="stretch",
    )
    st.subheader(f"Session summary — {ctx.session}")
    st.caption(
        "Assembled deterministically from engine outputs; every line lists its sources. "
        "No language model is involved."
    )
    for line in session_summary(ctx):
        st.markdown(
            f"**{line.section}.** {line.text}  \n<small>sources: {', '.join(line.sources)}</small>",
            unsafe_allow_html=True,
        )


def session_summary(ctx: Context) -> list[explain.Line]:
    b = ctx.engines()
    pos = market.position(ctx.bars.index, ctx.ts)
    row = ctx.bars.iloc[pos]
    prev = float(ctx.bars["close"].iloc[pos - 1]) if pos else None
    price = {
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "change_pct": None if prev is None else row["close"] / prev - 1,
    }
    states = analysis.strategy_states(b, ctx.ts).set_index("strategy_id")
    strat = {
        "strategy_id": ctx.strategy_id,
        **{str(k): v for k, v in states.loc[ctx.strategy_id].to_dict().items()},
    }
    risk = research.risk_decision_at(ctx.risk_run(), ctx.ts)
    risk = {
        **risk,
        "requested_exposure": risk["requested_exposure"],
        "approved_exposure": risk["approved_exposure"],
    }
    acct = paper_account_for(ctx.strategy_id, ctx.scenario)
    return explain.explain_session(
        session=ctx.session,
        price=price,
        candle=analysis.candle_view(b, ctx.ts),
        price_action=analysis.price_action_view(b, ctx.ts),
        regime=analysis.regime_view(b, ctx.ts),
        strategy=strat,
        risk=risk,
        paper=acct,
    )


# ── 2. Technical Analysis ──

OVERLAYS = ("sma_20", "sma_50", "sma_200", "ema_20", "ema_50", "ema_200")


def technical_analysis(ctx: Context) -> None:
    st.title("Technical Analysis")
    b = ctx.engines()
    ind = b.indicators.values
    left, right = st.columns([3, 1])
    with right:
        chosen = st.multiselect("Overlays", OVERLAYS, default=["sma_200"], key="ta_overlays")
        bb = st.toggle("Bollinger Bands (20, 2)", value=False, key="ta_bb")
    with left:
        n = range_picker("ta_range")
    w = window(ctx.bars, ctx.ts, n)
    iw = ind.loc[w.index]
    st.plotly_chart(
        charts.candlestick_figure(
            w,
            overlays={c: iw[c] for c in chosen},
            bollinger=iw[["bb_upper", "bb_middle", "bb_lower"]] if bb else None,
            marker_ts=ctx.ts,
            title=f"SPY through {ctx.session}",
        ),
        width="stretch",
    )

    st.subheader(f"Candlestick Engine — {ctx.session}")
    note("candles")
    cv = analysis.candle_view(b, ctx.ts)
    g = cv["geometry"]
    c = st.columns(5)
    c[0].metric("Body ratio", num(g["body_ratio"], "{:.3f}"))
    c[1].metric("Upper wick ratio", num(g["upper_wick_ratio"], "{:.3f}"))
    c[2].metric("Lower wick ratio", num(g["lower_wick_ratio"], "{:.3f}"))
    c[3].metric("Close position", num(g["close_position"], "{:.3f}"))
    c[4].metric("Prior trend (context)", str(g["prior_trend"]))
    if cv["patterns"]:
        st.dataframe(pd.DataFrame(cv["patterns"]), hide_index=True, width="stretch")
    else:
        st.write("No pattern detected on this session.")
    st.caption(
        f"Candlestick Engine {cv['engine_version']} · config `{cv['config_fingerprint']}`. "
        "`strength` is geometric quality in [0, 1] with no predictive meaning."
    )

    st.subheader(f"Indicators — values as of {ctx.session}")
    tbl = analysis.indicator_view(b, ctx.ts)
    st.dataframe(
        tbl,
        hide_index=True,
        width="stretch",
        column_config={"value": st.column_config.NumberColumn(format="%.4f")},
    )
    st.caption(
        "`warm-up`: not enough history yet; `undefined`: the formula is undefined on this "
        "bar (e.g. RSI with no price change). No value is filled in."
    )
    tabs = st.tabs(["Trend", "Momentum", "Volatility", "Volume"])
    with tabs[0]:
        st.plotly_chart(
            charts.lines_figure(
                iw[["macd", "macd_signal"]], title="MACD (12, 26, 9)", hlines=(0.0,)
            ),
            width="stretch",
        )
        st.plotly_chart(
            charts.lines_figure(iw[["macd_hist"]], title="MACD histogram", hlines=(0.0,)),
            width="stretch",
        )
    with tabs[1]:
        st.plotly_chart(
            charts.lines_figure(
                iw[["rsi_14"]],
                title="RSI (14) — 30/50/70 are conventional reference levels, not signals",
                hlines=(30.0, 50.0, 70.0),
            ),
            width="stretch",
        )
        st.plotly_chart(
            charts.lines_figure(
                iw[["stoch_k", "stoch_d"]], title="Stochastic %K/%D", hlines=(20.0, 80.0)
            ),
            width="stretch",
        )
        st.plotly_chart(
            charts.lines_figure(iw[["roc_12"]], title="ROC (12), %", hlines=(0.0,)),
            width="stretch",
        )
    with tabs[2]:
        st.plotly_chart(charts.lines_figure(iw[["atr_14"]], title="ATR (14)"), width="stretch")
        st.plotly_chart(
            charts.lines_figure(
                iw[["realized_vol_20"]], title="Realized volatility (20, annualised)", yformat=".0%"
            ),
            width="stretch",
        )
        st.plotly_chart(
            charts.lines_figure(
                iw[["bb_width_pct"]],
                title="Bollinger width (upper - lower) / middle",
                yformat=".1%",
            ),
            width="stretch",
        )
    with tabs[3]:
        st.plotly_chart(
            charts.lines_figure(
                iw[["relative_volume_20"]],
                title="Relative volume (volume / mean of previous 20)",
                hlines=(1.0,),
            ),
            width="stretch",
        )
        st.plotly_chart(
            charts.lines_figure(iw[["obv"]], title="On-balance volume"), width="stretch"
        )
    st.caption(
        f"Indicator Engine {b.indicators.engine_version} · config "
        f"`{b.indicators.config_fingerprint}`"
    )


# ── 3. Market Structure ──


def market_structure(ctx: Context) -> None:
    st.title("Market Structure")
    note("price_action")
    b = ctx.engines()
    pv = analysis.price_action_view(b, ctx.ts)
    s = pv["state"]
    c = st.columns(5)
    c[0].metric("Structure", str(s["structure"]))
    c[1].metric("Trend (structure)", str(s["trend"]))
    c[2].metric("Trend quality", num(s["trend_quality"], "{:.2f}"))
    c[3].metric(
        "Last swing labels", f"{s['last_high_label'] or '—'} / {s['last_low_label'] or '—'}"
    )
    c[4].metric("Consolidation", "yes" if s["in_consolidation"] else "no")
    n = range_picker("pa_range", default="6 months")
    w = window(ctx.bars, ctx.ts, n)
    swings = analysis.swings_known_at(b, ctx.ts)
    swings = swings[swings["pivot_ts"] >= w.index[0]]
    events = analysis.events_known_at(b, ctx.ts)
    events = events[events["ts"] >= w.index[0]]
    zones: list[dict[str, float | str]] = []
    if s["support_low"] is not None:
        zones.append({"kind": "support", "low": s["support_low"], "high": s["support_high"]})
    if s["resistance_low"] is not None:
        zones.append(
            {"kind": "resistance", "low": s["resistance_low"], "high": s["resistance_high"]}
        )
    st.plotly_chart(
        charts.candlestick_figure(
            w,
            swings=swings,
            events=events,
            zones=zones,
            marker_ts=ctx.ts,
            title=f"Confirmed swings, events and nearest zones known at the close of {ctx.session}",
        ),
        width="stretch",
    )
    st.caption(
        "Confirmation latency is respected: a pivot appears only once its `confirmed_at` "
        "is on or before the selected session; events only from their `available_at`; "
        "zones are those of the selected session's state."
    )
    left, right = st.columns(2)
    with left:
        st.markdown("**Support / resistance / range**")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "item": "support zone",
                        "value": f"{num(s['support_low'])} - {num(s['support_high'])}",
                        "distance": pct(s["support_distance_pct"]),
                    },
                    {
                        "item": "resistance zone",
                        "value": f"{num(s['resistance_low'])} - {num(s['resistance_high'])}",
                        "distance": pct(s["resistance_distance_pct"]),
                    },
                    {
                        "item": "range",
                        "value": f"{num(s['range_low'])} - {num(s['range_high'])}",
                        "distance": f"{s['range_bar_count']} bars",
                    },
                    {
                        "item": "window width state",
                        "value": str(s["window_width_state"]),
                        "distance": "",
                    },
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        st.markdown(f"**Event counts, last {pv['recent_sessions']} sessions**")
        st.dataframe(pd.DataFrame([pv["event_counts"]]), hide_index=True)
    with right:
        st.markdown("**Most recent confirmed swings**")
        st.dataframe(pv["recent_swings"], hide_index=True, width="stretch")
    st.markdown("**Events known in the last sessions**")
    st.dataframe(pv["recent_events"], hide_index=True, width="stretch")
    st.caption(f"Price Action Engine {pv['engine_version']} · config `{pv['config_fingerprint']}`")


# ── 4. Market Regime ──


def market_regime(ctx: Context) -> None:
    st.title("Market Regime")
    note("regimes")
    b = ctx.engines()
    rv = analysis.regime_view(b, ctx.ts)
    cols = st.columns(5)
    for col, (name, d) in zip(cols, rv["dimensions"].items(), strict=True):
        col.metric(
            name.capitalize(),
            str(d["label"]),
            f"{d['age_sessions']} sessions"
            + (" · changed today" if d["changed_on_session"] else ""),
            delta_color="off",
        )
        col.caption(f"previous: {d['previous_label'] or '—'}")
    m = rv["measures"]
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "volatility (RV20)": m["volatility_measure"],
                    "volatility percentile": m["volatility_percentile"],
                    "reference sessions": m["volatility_reference_count"],
                    "ATR %": m["atr_pct"],
                    "RSI": m["momentum_rsi"],
                    "ROC": m["momentum_roc"],
                    "MACD": m["momentum_macd"],
                    "relative volume": m["relative_volume"],
                    "trend quality": m["trend_quality"],
                }
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    n = range_picker("rg_range")
    st_ = b.regimes.state.loc[: ctx.ts].iloc[-n:]
    st.plotly_chart(
        charts.lines_figure(
            st_[["volatility_percentile"]], title="Causal volatility percentile", yformat=".0%"
        ),
        width="stretch",
    )
    st.markdown("**Regime labels, last 30 sessions** (descriptive)")
    hist = st_.iloc[-30:][
        [
            "trend_regime",
            "volatility_regime",
            "momentum_regime",
            "participation_regime",
            "composite_regime",
        ]
    ].iloc[::-1]
    st.dataframe(hist, width="stretch")
    st.caption(f"Regime Engine {rv['engine_version']} · config `{rv['config_fingerprint']}`")


# ── 5. Strategy Research ──


def strategy_research(ctx: Context) -> None:
    st.title("Research Strategy States")
    note("strategies")
    b = ctx.engines()
    tbl = analysis.strategy_states(b, ctx.ts)
    st.dataframe(tbl, hide_index=True, width="stretch")
    st.caption(
        "`state` is decided at the close (`observed_at`) and becomes effective at the next "
        "session open (`effective_at`); `state_in_effect` is the state decided at the "
        "previous close. INSUFFICIENT_DATA never becomes LONG or FLAT."
    )
    sid = st.selectbox(
        "History of",
        analysis.STRATEGIES,
        index=analysis.STRATEGIES.index(ctx.strategy_id),
        key="sr_strategy",
    )
    n = range_picker("sr_range")
    states = analysis.strategy_input_series(b, str(sid)).loc[: ctx.ts].iloc[-n:]
    numeric = states.map({"LONG": 1.0, "FLAT": 0.0}).rename(
        "LONG=1 / FLAT=0 (gaps = insufficient data)"
    )
    st.plotly_chart(
        charts.lines_figure(numeric.to_frame(), title=f"{sid} research state"),
        width="stretch",
    )
    st.caption(
        f"Strategy Engine {b.strategies.engine_version} · config "
        f"`{b.strategies.config_fingerprint}`"
    )


# ── 6. Backtesting ──


def backtesting(ctx: Context) -> None:
    st.title("Backtesting (Phase 8)")
    st.warning(DISCLAIMERS["backtest"])
    st.caption(
        "Hypothetical: decision at the close, fill at the next session's raw open; costs "
        "per order: $1 commission + half of a 2 bps spread + 2 bps slippage (research "
        "assumptions). Full SPY history, in-sample, descriptive."
    )
    results = ctx.backtest()
    table = ctx.backtest_table()
    st.subheader("Comparison (unranked; click a column header to sort)")
    st.dataframe(
        table,
        width="stretch",
        column_config={
            c: st.column_config.NumberColumn(format="percent")
            for c in (
                "cumulative_return",
                "gross_cumulative_return",
                "cost_drag_cumulative",
                "cagr",
                "annualized_volatility",
                "max_drawdown",
                "win_rate",
                "avg_trade_net_return",
                "median_trade_net_return",
                "exposure",
            )
        },
    )
    for k, v in research.BENCHMARK_NOTES.items():
        st.caption(f"`{k}` — {v}")
    gross = st.toggle("Show gross (zero-cost) curves", value=False, key="bt_gross")
    curves = research.equity_curves(results, gross=gross)
    st.plotly_chart(
        charts.lines_figure(
            curves,
            title=("Gross" if gross else "Net") + " cumulative return",
            height=420,
            yformat=".0%",
        ),
        width="stretch",
    )
    st.plotly_chart(
        charts.lines_figure(
            research.drawdown_curves(results),
            title="Drawdown (equity / running peak - 1)",
            height=320,
            yformat=".0%",
        ),
        width="stretch",
    )
    sid = st.selectbox(
        "Trade-return distribution of", analysis.STRATEGIES[1:], key="bt_hist_strategy"
    )
    trades = results[str(sid)].trades
    if len(trades):
        st.plotly_chart(
            charts.histogram_figure(
                trades["net_return"],
                title=f"{sid}: completed-trade net returns (n={len(trades)})",
                xformat=".0%",
            ),
            width="stretch",
        )
    else:
        st.write("No completed trades.")
    st.caption(
        f"Benchmarks: `{BENCHMARK_PRICE}` (price return) vs `{BENCHMARK_TOTAL}` (total "
        "return). Compare timing strategies with the price-return benchmark."
    )


# ── 7. Statistical Validation ──


def statistical_validation(ctx: Context) -> None:
    st.title("Statistical Validation (Phase 9)")
    note("validation")
    try:
        frames, meta = research.load_validation(RESEARCH_ROOT, ctx.ident.key)
    except research.ResultsMissingError:
        st.warning(
            "No validation results stored for this dataset and configuration. The "
            "validation is never run automatically."
        )
        if st.button(
            "Run Phase 9 validation now (about 1-2 min; block bootstrap, no fitting)",
            key="run_validation",
        ):
            with st.spinner("Running the pre-declared validation…"):
                research.compute_validation(ctx.bars, RESEARCH_ROOT, ctx.ident.key)
            st.rerun()
        return
    comps = frames["comparisons"]
    st.subheader(f"Project conclusion: {research.validation_conclusion(comps)}")
    st.caption(
        f"Computed {meta['created_at']} · validation {meta['validation_version']} · "
        f"config `{meta['config_fingerprint']}` · method moving-block bootstrap, block "
        f"{meta['config']['block_length']}, {meta['config']['n_resamples']} resamples, "
        f"{meta['config']['confidence']:.0%} intervals, seed {meta['config']['seed']}."
    )
    formal = research.classify_formal(comps)
    st.markdown(
        f"**Pre-declared formal family** ({len(formal)} tests; slice "
        f"`{meta['config']['formal_slice']}`, cost scenario "
        f"`{meta['config']['default_scenario']}`). H0: paired difference = 0."
    )
    st.dataframe(
        formal[
            [
                "a",
                "b",
                "metric",
                "null_hypothesis",
                "estimate",
                "lower",
                "upper",
                "p_value",
                "p_holm",
                "p_bh",
                "status",
            ]
        ],
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "`status` uses the Holm-adjusted p-value (family-wise error control). "
        "Undefined tests count in the family size."
    )
    st.markdown("**Bootstrap intervals**")
    iv = frames["intervals"]
    f1, f2 = st.columns(2)
    sl = f1.selectbox("Slice", sorted(iv["prov_slice_name"].unique()), key="val_slice")
    metric = f2.selectbox("Metric", sorted(iv["metric"].unique()), key="val_metric")
    sel = iv[(iv["prov_slice_name"] == sl) & (iv["metric"] == metric)]
    st.dataframe(
        sel[
            [
                "subject",
                "metric",
                "point",
                "lower",
                "upper",
                "valid_resamples",
                "note",
                "prov_cost_scenario",
            ]
        ],
        hide_index=True,
        width="stretch",
    )
    with st.expander("Cost sensitivity (predefined scenarios)"):
        st.dataframe(frames["cost_sensitivity"], hide_index=True, width="stretch")
    with st.expander("Descriptive comparisons (no p-values)"):
        st.dataframe(comps[comps["family"] != "formal"], hide_index=True, width="stretch")


# ── 8. Machine Learning ──


def machine_learning(ctx: Context) -> None:
    st.title("Machine Learning Research (Phase 10)")
    note("ml")
    exp_id = research.ml_experiment_id(ctx.bars)
    try:
        frames, meta = research.load_ml(RESEARCH_ROOT, exp_id)
    except research.ResultsMissingError:
        st.warning(
            "No experiment stored for this dataset and configuration. Models are never "
            "trained automatically."
        )
        if st.button(
            "Run the pre-declared Phase 10 experiment now (trains logistic regression "
            "and random forest on TRAIN only; about 2-3 min)",
            key="run_ml",
        ):
            with st.spinner("Training on TRAIN, evaluating on VALIDATION/TEST…"):
                research.compute_ml(ctx.bars, RESEARCH_ROOT, ctx.ident.instrument_id)
            st.rerun()
        return
    s = research.ml_summary(frames, meta)
    st.subheader(f"Project conclusion: {s['overall_verdict']}")
    c = st.columns(4)
    c[0].metric("Models", ", ".join(s["models"]))
    c[1].metric("Features", num(s["feature_count"], "{:.0f}"))
    c[2].metric("Experiment", s["experiment_id"])
    c[3].metric("Git commit (when run)", str(s["git_commit"]))
    st.caption(
        f"Target: {s['target']}. TRAIN {s['train']} · VALIDATION {s['validation']} · "
        f"TEST {s['test']} (chronological, purged)."
    )
    st.markdown(
        "**Classification metrics** (TRAIN is in-sample; TEST is the primary out-of-sample period)"
    )
    st.dataframe(frames["classification"], hide_index=True, width="stretch")
    st.markdown("**ROC-AUC with block-bootstrap 95% intervals**")
    st.dataframe(frames["auc_intervals"], hide_index=True, width="stretch")
    st.markdown("**Pre-declared edge rule per model**")
    st.dataframe(frames["verdicts"], hide_index=True, width="stretch")
    preds = frames.get("predictions")
    if preds is not None:
        model = st.selectbox("Model", s["models"], key="ml_model")
        test = preds[preds["period"] == "test"]
        st.plotly_chart(
            charts.histogram_figure(
                test[f"p_{model}"].dropna(), title=f"{model}: predicted P(up) on TEST"
            ),
            width="stretch",
        )
        cal = research.calibration_table(preds, str(model))
        if len(cal):
            st.plotly_chart(
                charts.calibration_figure(
                    cal, title=f"{model}: calibration on TEST (probability deciles)"
                ),
                width="stretch",
            )
            st.dataframe(cal, hide_index=True)
    with st.expander("Financial evaluation (Phase 8 backtests of model states)"):
        st.dataframe(frames["financial"], hide_index=True, width="stretch")


# ── 9. Risk Management ──


def risk_management(ctx: Context) -> None:
    st.title("Risk Management (Phase 11)")
    note("risk")
    st.code(
        "Strategy request (LONG 1 / FLAT 0)\n      ↓\nRiskManager (sizing → limits → stop "
        "lockout → drawdown/session locks)\n      ↓\nApproved exposure (executed at the next "
        "session open)",
        language=None,
    )
    run = ctx.risk_run()
    d = research.risk_decision_at(run, ctx.ts)
    st.subheader(f"{ctx.strategy_id} · {ctx.scenario} · decision at the close of {ctx.session}")
    c = st.columns(5)
    c[0].metric("Strategy state", str(d["strategy_state"]))
    c[1].metric("Requested exposure", num(d["requested_exposure"], "{:.2f}"))
    c[2].metric("Sized exposure", num(d["sized_exposure"], "{:.2f}"))
    c[3].metric("Approved exposure", num(d["approved_exposure"], "{:.2f}"))
    c[4].metric("Risk state", str(d["risk_state"]))
    c = st.columns(5)
    c[0].metric("Sizing method", str(d["sizing_method"]))
    c[1].metric("Volatility target", pct(d["target_volatility"]))
    c[2].metric("ATR risk budget", pct(d["atr_risk_budget"]))
    c[3].metric("Drawdown (from HWM)", pct(d["drawdown"]))
    c[4].metric("Stop level", num(d["stop_level"]))
    st.write(
        f"**Reasons for reduction/block:** {d['reasons'] or 'none (approved = requested)'} · "
        f"stop triggered: {d['stop_triggered']} · intraday touch (diagnostic only): "
        f"{d['stop_intraday_touch']} · drawdown lock "
        f"{'enabled' if d['drawdown_lock_enabled'] else 'disabled'} in this scenario."
    )
    n = range_picker("rk_range")
    dec = run.decisions.set_index("bar_ts").loc[: ctx.ts].iloc[-n:]
    st.plotly_chart(
        charts.lines_figure(
            dec[["requested_exposure", "approved_exposure"]], title="Requested vs approved exposure"
        ),
        width="stretch",
    )
    st.markdown(
        f"**All pre-declared scenarios for {ctx.strategy_id}** (descriptive; no scenario "
        "is selected)"
    )
    st.dataframe(
        cached_risk_table(ctx.key, ctx.ident.symbol, ctx.strategy_id),
        hide_index=True,
        width="stretch",
    )
    with st.expander("Scenario specifications"):
        st.dataframe(research.scenario_specs(), hide_index=True, width="stretch")


# ── 10. Paper Trading ──


def paper_trading(ctx: Context) -> None:
    st.title("Paper Trading")
    st.warning(f"**{PAPER_LABEL}** — {DISCLAIMERS['paper']}")
    refs = paper.list_accounts(PAPER_ROOT)
    with st.expander(f"{PAPER_LABEL}: create a local paper account", expanded=not refs):
        inputs = cached_paper_inputs(ctx.key, ctx.ident.symbol)
        c = st.columns(3)
        sid = c[0].selectbox("Strategy", list(inputs), key="pp_new_strategy")
        scen = c[1].selectbox("Risk scenario", [s.name for s in SCENARIOS], key="pp_new_scenario")
        start = c[2].date_input(
            "First session",
            value=ctx.ident.last_ts.date() - timedelta(days=365),
            key="pp_new_start",
        )
        if st.button(
            f"{PAPER_LABEL}: create account and process available sessions", key="pp_create"
        ):
            version, bars_in = inputs[str(sid)]
            try:
                store = paper.paper_create_account(
                    PAPER_ROOT,
                    strategy_id=str(sid),
                    strategy_version=version,
                    scenario_name=str(scen),
                    instrument=ctx.ident.symbol,
                )
                counts = paper.paper_process_sessions(
                    store, bars_in, start if isinstance(start, date) else None
                )
                st.success(f"{PAPER_LABEL}: account {store.account_id} created; {counts}.")
            except paper.AccountError as exc:
                st.error(str(exc))
            st.rerun()
    if not refs:
        st.info("No local paper account yet.")
        return
    labels = {f"{r.strategy_id} · {r.scenario} · {r.account_id}": r for r in refs}
    ref = labels[str(st.selectbox("Paper account", list(labels), key="pp_account"))]
    try:
        store = paper.open_account(ref, PAPER_ROOT)  # re-read every rerun: never cached
        view = paper.account_view(store)
    except (paper.AccountError, Exception) as exc:
        st.error(f"Account cannot be opened safely: {exc}. Nothing was modified.")
        return
    b1, b2 = st.columns(2)
    if b1.button(f"{PAPER_LABEL}: process available completed sessions", key="pp_process"):
        inputs = cached_paper_inputs(ctx.key, ctx.ident.symbol)
        if ref.strategy_id not in inputs or inputs[ref.strategy_id][0] != ref.strategy_version:
            st.error("Strategy version in the data differs from the account's; not processed.")
        else:
            try:
                counts = paper.paper_process_sessions(store, inputs[ref.strategy_id][1])
                st.success(f"{PAPER_LABEL}: {counts}")
            except paper.AccountError as exc:
                st.error(str(exc))
        st.rerun()
    if b2.button("Refresh account", key="pp_refresh"):
        st.rerun()

    st.subheader("Account")
    c = st.columns(5)
    c[0].metric("Equity", num(view["equity"]))
    c[1].metric("Cash", num(view["cash"]))
    c[2].metric("Market value", num(view["market_value"]))
    c[3].metric("Exposure", pct(view["exposure"]))
    c[4].metric("Initial capital", num(view["initial_capital"]))
    c = st.columns(4)
    c[0].metric("Realized P&L", num(view["realized_pnl"]))
    c[1].metric("Unrealized P&L", num(view["unrealized_pnl"]))
    c[2].metric("Total P&L", num(view["total_pnl"]))
    c[3].metric("Cumulative costs", num(view["cumulative_costs"]))
    st.subheader("Position")
    c = st.columns(4)
    c[0].metric("Instrument", view["instrument"])
    c[1].metric("Quantity", num(view["position_quantity"], "{:,.4f}"))
    c[2].metric("Average cost (incl. costs)", num(view["average_cost"]))
    c[3].metric("Mark (raw close)", num(view["mark_price"]))
    st.subheader("Current state")
    c = st.columns(5)
    c[0].metric("Latest strategy state", str(view["latest_strategy_state"]))
    c[1].metric("Requested exposure", num(view["requested_exposure"], "{:.2f}"))
    c[2].metric("Approved exposure", num(view["approved_exposure"], "{:.2f}"))
    c[3].metric("Pending", str(view["pending_kind"] or "none"))
    c[4].metric(
        "Next effective session",
        "—"
        if view["next_effective_session"] is None
        else f"{view['next_effective_session']:%Y-%m-%d %H:%M} UTC",
    )
    st.caption(
        f"Account `{view['account_id']}` · last processed session {view['last_session']} "
        f"({view['sessions_processed']} sessions) · risk state {view['risk_state']} · "
        f"ledger: {view['ledger_events']} events, {view['ledger_status']}, last hash "
        f"`{view['ledger_last_hash'][:16]}`"
    )
    if view["uncommitted_ledger_events"]:
        st.warning(
            f"{view['uncommitted_ledger_events']} uncommitted ledger event(s) from an "
            "interrupted session; they are completed only if re-processing reproduces "
            "them exactly."
        )
    st.subheader("Ledger (most recent sessions, newest first)")
    tables = paper.ledger_tables(store)
    tabs = st.tabs(
        [
            "Decisions (risk approvals & instructions)",
            "Orders",
            "Fills",
            "Reconciliation",
            "Account snapshots",
        ]
    )
    for tab, key in zip(tabs, paper.LEDGER_TYPES, strict=True):
        with tab:
            st.dataframe(tables[key], hide_index=True, width="stretch")


# ── 11. System / Data Health ──


def system_health(ctx: Context) -> None:
    st.title("System / Data Health")
    db = health.database_status(get_engine())
    app = health.app_status()
    safety = health.safety_status(get_settings())
    with get_session_factory()() as s:
        schema = health.schema_status(s)
        q = health.quality_events(s)
    c = st.columns(4)
    c[0].metric("Database", db["status"])
    c[1].metric(
        "Schema revision",
        str(schema["current"]),
        "at head" if schema["at_head"] else f"head is {schema['head']}",
        delta_color="off",
    )
    c[2].metric("App version", app["app_version"])
    c[3].metric("Git commit", app["git_commit"])
    c = st.columns(4)
    c[0].metric("Dataset", f"{ctx.ident.first_ts:%Y-%m-%d} → {ctx.ident.last_ts:%Y-%m-%d}")
    c[1].metric("Bars", f"{ctx.ident.bars:,}")
    c[2].metric("Data-quality events", f"{sum(q.values())}")
    c[3].metric("Trading mode", safety["trading_mode"])
    st.caption(
        "Quality events by severity: " + (", ".join(f"{k} {v}" for k, v in q.items()) or "none")
    )
    st.subheader("Safety")
    st.json(safety)
    st.subheader("Paper accounts")
    rows = []
    for ref in paper.list_accounts(PAPER_ROOT):
        try:
            v = paper.account_view(paper.open_account(ref, PAPER_ROOT))
            rows.append(
                {
                    "account": ref.account_id,
                    "strategy": ref.strategy_id,
                    "scenario": ref.scenario,
                    "last session": v["last_session"],
                    "ledger events": v["ledger_events"],
                    "ledger": v["ledger_status"],
                    "last hash": v["ledger_last_hash"][:16],
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "account": ref.account_id,
                    "strategy": ref.strategy_id,
                    "scenario": ref.scenario,
                    "ledger": f"ERROR {type(exc).__name__}: {exc}",
                }
            )
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    else:
        st.write("No local paper accounts.")
    st.caption(
        f"Cache keys: dataset `{ctx.ident.key}`, configuration `{CONFIG_KEY}`. "
        "No secret or connection setting is displayed."
    )


# ── 12. Alerts & Monitoring ──


SEVERITY_ORDER = ("CRITICAL", "WARNING", "INFO")


def alerts_monitoring(ctx: Context) -> None:
    st.title("Alerts & Monitoring")
    st.caption(
        "Alerts report events the engines, the risk layer and the paper simulation already "
        "recorded. They are not recommendations and never trigger any trading action. "
        "Paper-related alerts are PAPER SIMULATION events."
    )
    with get_session_factory()() as s:
        ing = market.latest_ingestion(s, ctx.ident.instrument_id)
    fresh = market.freshness(ctx.ident.last_ts, datetime.now(UTC))
    mon = alerts_svc.monitoring_state(
        get_engine(), PAPER_ROOT, ident=ctx.ident, fresh=fresh, last_ingestion=ing
    )
    st.subheader("Monitoring")
    st.dataframe(
        pd.DataFrame([{"component": k, **v} for k, v in mon.items()]),
        hide_index=True,
        width="stretch",
    )
    alerts, metrics = alerts_svc.load_alerts()
    if st.button("Evaluate alert rules now (writes to the local alert store only)", key="al_run"):
        from app.alerts.engine import run_alerts
        from app.alerts.sources import dashboard_results_missing, load_market

        res = run_alerts(
            settings=get_settings(),
            load_market=load_market,
            paper_root=PAPER_ROOT,
            dashboard_results_missing=dashboard_results_missing,
        )
        st.success(
            f"{len(res.new_alerts)} new alert(s); {res.candidates - len(res.new_alerts)} "
            "already known (deduplicated)."
        )
        st.rerun()
    if metrics is None:
        st.info("No alert store yet. Run `python -m app.alerts.cli run` or the button above.")
        return
    c = st.columns(5)
    c[0].metric("Alerts stored", metrics["alerts_stored"])
    c[1].metric("Deduplicated", metrics["alerts_deduplicated"])
    c[2].metric("Deliveries succeeded", metrics["deliveries_succeeded"])
    c[3].metric("Delivery attempts failed", metrics["delivery_attempts_failed"])
    c[4].metric("Failed permanently", metrics["deliveries_failed_permanently"])
    st.caption(
        "Operational counters of the alert subsystem; not performance metrics. By "
        f"severity {metrics['by_severity']}; by source {metrics['by_source']}."
    )
    if alerts.empty:
        st.info("No alerts stored.")
        return
    f = st.columns(5)
    sev = f[0].multiselect(
        "Severity", SEVERITY_ORDER, default=["CRITICAL", "WARNING"], key="al_sev"
    )
    src = f[1].multiselect("Source", sorted(alerts["source"].unique()), key="al_src")
    strat = f[2].selectbox(
        "Strategy", ["(all)", *sorted(alerts["strategy_id"].dropna().unique())], key="al_strat"
    )
    acct = f[3].selectbox(
        "Account", ["(all)", *sorted(alerts["account_id"].dropna().unique())], key="al_acct"
    )
    days = pd.to_datetime(alerts["occurred_at"], utc=True)
    rng = f[4].date_input(
        "Date range", value=(days.min().date(), days.max().date()), key="al_range"
    )
    start, end = rng if isinstance(rng, tuple) and len(rng) == 2 else (None, None)
    view = alerts_svc.filter_alerts(
        alerts,
        severities=sev,
        sources=src,
        strategy=None if strat == "(all)" else str(strat),
        account=None if acct == "(all)" else str(acct),
        start=start,
        end=end,
    )
    st.caption(
        f"{len(view)} of {len(alerts)} alerts (newest first). Defaults show CRITICAL and "
        "WARNING only to keep the view quiet."
    )
    st.dataframe(
        view[[*alerts_svc.ALERT_COLUMNS[:8], "delivery"]], hide_index=True, width="stretch"
    )
    if len(view):
        chosen = str(
            st.selectbox(
                "Alert detail",
                view["alert_id"],
                format_func=lambda i: (
                    f"{i} · " + str(view.loc[view["alert_id"] == i, "title"].iloc[0])
                ),
                key="al_detail",
            )
        )
        d = alerts_svc.alert_detail(view, str(chosen))
        st.markdown(f"**{d['event_type']}** · {d['severity']} · source {d['source']}")
        st.write(d["message"])
        st.json(
            {
                k: d[k]
                for k in (
                    "occurred_at",
                    "session",
                    "instrument",
                    "strategy_id",
                    "account_id",
                    "dedup_key",
                    "alert_id",
                    "recorded_at",
                    "delivery",
                    "rule_version",
                    "app_version",
                )
            }
        )
        st.markdown("**Structured reason (recorded values)**")
        st.json(d["payload"])


# ── 13. Operations ──

OPS_LABEL = "PAPER SIMULATION / LOCAL OPERATIONS"


def operations(ctx: Context) -> None:

    from app.operations.config import OPS_ROOT

    st.title("Operations")
    st.caption(
        f"{OPS_LABEL}. The daily pipeline (precheck -> ingest -> validate -> paper -> alerts -> "
        "health) runs locally on completed XNYS sessions only. It never sends an order or "
        "moves money."
    )
    ov = ops_svc.overview(
        OPS_ROOT,
        PAPER_ROOT,
        datetime.now(UTC),
        ctx.ident.last_ts.tz_convert("America/New_York").date().isoformat(),
    )
    c = st.columns(6)
    c[0].metric("Latest completed session", str(ov["latest_completed_session"]))
    c[1].metric("Next session eligible", str(ov["next_session_eligible_at"])[:16] + " UTC")
    c[2].metric("Latest ingested", str(ov["latest_ingested_session"]))
    c[3].metric("Latest paper session", str(ov["latest_paper_session"]))
    c[4].metric("Pipeline checkpoint", str(ov["checkpoint"]))
    c[5].metric("Scheduler", str(ov["scheduler"]["status"]).split(" (")[0])
    st.caption(
        "Latest alert evaluation: "
        f"{ov['latest_alert_evaluation'] or 'none recorded by the pipeline'}"
        f" · grace after the official close: {ov['grace_minutes']} min · lock: "
        + (
            "free"
            if not ov["lock"]
            else f"held by {ov['lock'].get('host')}:{ov['lock'].get('pid')}"
            f" since {ov['lock'].get('acquired_at')}"
        )
    )
    b1, b2 = st.columns(2)
    if b1.button("Dry run (shows the plan; changes nothing)", key="ops_dry"):
        from app.operations.cli import build_pipeline
        from app.operations.config import OperationsConfig

        res = build_pipeline(OperationsConfig(), dry_run=True).run(mode="dry_run")
        st.json(
            {
                "targets": res.target_sessions,
                "precheck": res.precheck.status if res.precheck else None,
                "plan": res.plan,
            }
        )
    if b2.button(f"{OPS_LABEL}: run pending sessions now", key="ops_run"):
        from app.operations.cli import build_pipeline
        from app.operations.config import OperationsConfig

        with st.spinner("Running the local pipeline..."):
            res = build_pipeline(OperationsConfig()).run(mode="manual")
        st.success(f"{OPS_LABEL}: {res.status}; sessions {res.target_sessions or 'none'}")
        if res.errors:
            st.error("; ".join(res.errors))
    st.subheader("Last run")
    last = ops_svc.last_run_detail(OPS_ROOT)
    if last is None:
        st.info(
            "No pipeline run recorded yet. Use the buttons above or "
            "`python -m app.operations.cli run`."
        )
    else:
        st.write(
            f"**{last['status']}** · {last.get('mode')} · {last['run_id']} · sessions "
            f"{', '.join(last['sessions']) or 'none'}"
        )
        st.dataframe(ops_svc.stage_table(last), hide_index=True, width="stretch")
    st.subheader("Recent runs")
    st.dataframe(ops_svc.history_table(OPS_ROOT), hide_index=True, width="stretch")
    with st.expander("Scheduler and operational metrics"):
        st.json({"scheduler": ov["scheduler"], "metrics": ov["metrics"]})
    st.caption(
        "Operational metrics only (durations, counts, retries); not trading performance. "
        "Start the scheduler with `python -m app.operations.cli scheduler`."
    )


PAGES = (
    ("Market Overview", market_overview),
    ("Technical Analysis", technical_analysis),
    ("Market Structure", market_structure),
    ("Market Regime", market_regime),
    ("Strategy Research", strategy_research),
    ("Backtesting", backtesting),
    ("Statistical Validation", statistical_validation),
    ("Machine Learning", machine_learning),
    ("Risk Management", risk_management),
    ("Paper Trading", paper_trading),
    ("Alerts & Monitoring", alerts_monitoring),
    ("Operations", operations),
    ("System / Data Health", system_health),
)
