"""Deterministic session summary assembled ONLY from structured engine outputs.

No language model is involved and no text is invented: every line is a fixed template filled
with fields of the views produced by the engines, and every line carries the source fields it
was built from (`sources`), so each statement is traceable. Undefined values are shown as
"undefined", never replaced.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Line:
    section: str
    text: str
    sources: tuple[str, ...]


def _f(v: Any, fmt: str = "{:.2f}") -> str:
    return "undefined" if v is None else fmt.format(v)


def _pct(v: Any) -> str:
    return "undefined" if v is None else f"{v * 100:+.2f}%"


def explain_session(
    *,
    session: Any,
    price: dict[str, Any],
    candle: dict[str, Any],
    price_action: dict[str, Any],
    regime: dict[str, Any],
    strategy: dict[str, Any] | None,
    risk: dict[str, Any] | None,
    paper: dict[str, Any] | None,
) -> list[Line]:
    out: list[Line] = []
    out.append(
        Line(
            "Price",
            f"Session {session}: open {_f(price['open'])}, high {_f(price['high'])}, low "
            f"{_f(price['low'])}, close {_f(price['close'])}; change vs previous close "
            f"{_pct(price.get('change_pct'))}.",
            ("bars.open", "bars.high", "bars.low", "bars.close", "bars.close[t-1]"),
        )
    )

    pats = candle["patterns"]
    g = candle["geometry"]
    names = (
        ", ".join(
            f"{p['pattern']} ({p['family']}, {p['orientation']}, strength {_f(p['strength'])})"
            for p in pats
        )
        or "no pattern detected"
    )
    out.append(
        Line(
            "Candlestick",
            f"{names}. Body ratio {_f(g['body_ratio'])}, upper wick {_f(g['upper_wick_ratio'])}, "
            f"lower wick {_f(g['lower_wick_ratio'])}. Descriptive observation, not a signal.",
            ("candles.observations", "candles.geometry"),
        )
    )

    s = price_action["state"]
    counts = price_action["event_counts"]
    ev = ", ".join(f"{k} {v}" for k, v in counts.items() if v) or "none"
    out.append(
        Line(
            "Market structure",
            f"Structure {s['structure']} (trend {s['trend']}, quality {_f(s['trend_quality'])}); "
            f"last swing labels {s['last_high_label'] or 'n/a'}/{s['last_low_label'] or 'n/a'}; "
            f"consolidation {'yes' if s['in_consolidation'] else 'no'}. Events known in the last "
            f"{price_action['recent_sessions']} sessions: {ev}.",
            ("price_action.state", "price_action.events[available_at<=T]"),
        )
    )

    d = regime["dimensions"]
    m = regime["measures"]
    for name, label in (
        ("trend", "Trend"),
        ("volatility", "Volatility regime"),
        ("momentum", "Momentum"),
        ("participation", "Participation"),
    ):
        x = d[name]
        extra = ""
        if name == "volatility":
            extra = f"; percentile {_f(m['volatility_percentile'])}"
        if name == "momentum":
            extra = f"; RSI {_f(m['momentum_rsi'])}, ROC {_f(m['momentum_roc'])}"
        out.append(
            Line(
                label,
                f"{x['label']} for {x['age_sessions']} sessions"
                f"{' (changed this session)' if x['changed_on_session'] else ''}; previous "
                f"{x['previous_label'] or 'n/a'}{extra}. Describes observed conditions only.",
                (f"regimes.{name}_regime", f"regimes.{name}_age"),
            )
        )

    if strategy is not None:
        out.append(
            Line(
                "Strategy state",
                f"{strategy['strategy_id']}: {strategy['state']} (in effect: "
                f"{strategy['state_in_effect']}); reason: {strategy['reason']}. Research benchmark "
                "state, not a recommendation.",
                ("strategies.signals",),
            )
        )
    if risk is not None:
        out.append(
            Line(
                "Risk-approved exposure",
                f"Scenario {risk['scenario']}: requested {_f(risk['requested_exposure'])} -> "
                f"approved {_f(risk['approved_exposure'])}"
                f"{' (' + risk['reasons'] + ')' if risk.get('reasons') else ''}; risk state "
                f"{risk['risk_state']}.",
                ("risk.decisions",),
            )
        )
    if paper is not None:
        out.append(
            Line(
                "Paper account",
                f"PAPER SIMULATION {paper['account_id']}: equity {_f(paper['equity'])}, exposure "
                f"{_f(paper['exposure'])}, total P&L {_f(paper['total_pnl'])}; last processed "
                f"session {paper['last_session']}; pending {paper['pending_kind'] or 'none'}.",
                ("paper.state", "paper.ledger"),
            )
        )
    return out
