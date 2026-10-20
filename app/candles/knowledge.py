"""Cited candle vocabulary and research recognition. No order interfaces.

Numeric thresholds are explicit local conventions, not TA-Lib parity or accuracy.
Existing execution retains its original twenty detectors; extras are research only.
"""

from datetime import datetime
from typing import Any, cast

import pandas as pd

from app.candles.config import CandleConfig
from app.candles.engine import CandlestickEngine
from app.candles.patterns import REGISTRY, evening_star, morning_star

VERSION = "candle-knowledge/1.0.0"
TIMEFRAMES = {"M5": 5, "H1": 60, "H4": 240}
TA = "https://ta-lib.org/functions/"
IG = "https://www.ig.com/uk/ig-academy/the-basics-of-technical-analysis/candlestick-patterns"
FX = "https://www.forex.com/en-us/trading-academy/courses/technical-analysis/key-candlestick-patterns/"
BP = "https://www.babypips.com/learn/forex/triple-candlestick-patterns"

# Source descriptions are paraphrased; exact numeric definitions belong to local code.
BASE = {
    "hammer": ("Martelo", "Pavio inferior longo, corpo no topo; queda prévia.", IG),
    "hanging_man": ("Enforcado", "Forma do martelo após alta; contexto muda a identidade.", IG),
    "inverted_hammer": ("Martelo invertido", "Pavio superior longo após queda.", IG),
    "shooting_star": ("Estrela cadente", "Pavio superior longo após alta.", IG),
    "bullish_engulfing": (
        "Engolfo de alta",
        "Corpo comprador maior envolve corpo vendedor, não necessariamente pavios.",
        FX,
    ),
    "bearish_engulfing": (
        "Engolfo de baixa",
        "Corpo vendedor maior envolve corpo comprador; contexto de alta.",
        FX,
    ),
    "morning_star": (
        "Estrela da manhã",
        "Três velas: queda, corpo pequeno, recuperação de metade do primeiro corpo.",
        BP,
    ),
    "evening_star": (
        "Estrela da tarde",
        "Três velas: alta, corpo pequeno, recuo de metade do primeiro corpo.",
        BP,
    ),
    "tweezer_bottom": (
        "Pinça de fundo",
        "Mínimas próximas com cores opostas após queda; tolerância local.",
        FX,
    ),
    "tweezer_top": (
        "Pinça de topo",
        "Máximas próximas com cores opostas após alta; tolerância local.",
        FX,
    ),
    "three_white_soldiers": (
        "Três soldados brancos",
        "Três corpos compradores progressivos, abrindo dentro do anterior.",
        TA + "cdl3whitesoldiers",
    ),
    "three_black_crows": (
        "Três corvos negros",
        "Três corpos vendedores progressivos, abrindo dentro do anterior.",
        TA + "cdl3blackcrows",
    ),
    "rising_three_methods": (
        "Três métodos de alta",
        "Impulso, três velas menores contidas, retomada compradora.",
        TA,
    ),
    "falling_three_methods": (
        "Três métodos de baixa",
        "Impulso, três velas menores contidas, retomada vendedora.",
        TA,
    ),
    "inside_bar": (
        "Barra interna",
        "Amplitude totalmente contida na anterior; sem direção automática.",
        FX,
    ),
    "outside_bar": (
        "Barra externa",
        "Amplitude envolve a anterior; não equivale a engolfo de corpos.",
        FX,
    ),
    "doji": ("Doji", "Corpo muito pequeno; indecisão, sem direção automática.", IG),
    "spinning_top": ("Pião", "Corpo pequeno e dois pavios relevantes; indecisão.", IG),
    "marubozu": (
        "Marubozu",
        "Corpo ocupa quase toda a amplitude; impulso, não lucro garantido.",
        TA,
    ),
    "long_body": (
        "Corpo longo",
        "Corpo grande na própria amplitude e ante os corpos anteriores.",
        TA,
    ),
}


def catalog() -> dict[str, dict[str, Any]]:
    result = {
        s.name: {
            "name": s.name,
            "label_pt": BASE[s.name][0],
            "rule_pt": BASE[s.name][1],
            "source": BASE[s.name][2],
            "family": s.family,
            "bars": s.bars,
            "orientation": s.orientation,
            "prior_trend": s.context_trend,
            "live_detector": True,
        }
        for s, _ in REGISTRY
    }
    for bull in (True, False):
        prefix, orientation, trend, label = (
            ("bullish", "bullish", "down", "alta")
            if bull
            else ("bearish", "bearish", "up", "baixa")
        )
        for cross in (False, True):
            suffix = "harami_cross" if cross else "harami"
            name = f"{prefix}_{suffix}"
            result[name] = {
                "name": name,
                "label_pt": f"Harami {'cruz ' if cross else ''}de {label}",
                "rule_pt": "Corpo longo seguido de corpo estritamente contido: "
                + ("doji." if cross else "pequeno, de cor oposta."),
                "source": TA + ("cdlharamicross" if cross else "cdlharami"),
                "family": "reversal",
                "bars": 2,
                "orientation": orientation,
                "prior_trend": trend,
                "live_detector": False,
            }
    extras = (
        (
            "piercing_line",
            "Linha penetrante",
            2,
            "bullish",
            "down",
            "Gap abaixo da mínima anterior e recuperação além do meio do corpo, sem engolfo.",
            "cdlpiercing",
        ),
        (
            "dark_cloud_cover",
            "Nuvem negra",
            2,
            "bearish",
            "up",
            "Gap acima da máxima anterior e recuo além do meio do corpo, sem engolfo.",
            "cdldarkcloudcover",
        ),
        (
            "dragonfly_doji",
            "Doji libélula",
            1,
            "neutral",
            None,
            "Doji com pavio inferior longo e superior curto; não autoriza direção.",
            "cdldragonflydoji",
        ),
        (
            "gravestone_doji",
            "Doji lápide",
            1,
            "neutral",
            None,
            "Doji com pavio superior longo e inferior curto; não autoriza direção.",
            "cdlgravestonedoji",
        ),
        (
            "morning_doji_star",
            "Estrela da manhã doji",
            3,
            "bullish",
            "down",
            "Variante local da estrela da manhã com doji central; admite contato de corpos.",
            "cdlmorningdojistar",
        ),
        (
            "evening_doji_star",
            "Estrela da tarde doji",
            3,
            "bearish",
            "up",
            "Variante local da estrela da tarde com doji central; admite contato de corpos.",
            "cdleveningdojistar",
        ),
    )
    for name, label, n, orient, expected_trend, rule, page in extras:
        result[name] = {
            "name": name,
            "label_pt": label,
            "rule_pt": rule,
            "source": TA + page,
            "family": "indecision" if orient == "neutral" else "reversal",
            "bars": n,
            "orientation": orient,
            "prior_trend": expected_trend,
            "live_detector": False,
        }
    for name, item in result.items():
        item.update(
            knowledge_version=VERSION,
            statistically_approved=False,
            strength_is_probability=False,
            thresholds="explicit local conventions",
        )
        item["qualification"] = (
            "Soldados/corvos também são descritos como reversão ou impulso; "
            "família de execução congelada."
            if name in ("three_white_soldiers", "three_black_crows")
            else "Descrição de forma e contexto; não é previsão nem autorização de ordem."
        )
    return result


CATALOG = catalog()


def explain_pattern(name: str) -> dict[str, Any]:
    item = CATALOG[name]
    return {
        key: item[key]
        for key in (
            "label_pt",
            "rule_pt",
            "source",
            "prior_trend",
            "qualification",
            "knowledge_version",
        )
    }


def additional_masks(g: pd.DataFrame) -> dict[str, pd.Series]:
    cfg = CandleConfig()
    usable = g["usable"] & g["usable"].shift(1, fill_value=False)
    large = g["body_ratio"].shift(1) >= cfg.large_body_min_ratio
    small = g["body_ratio"] <= cfg.small_body_max_ratio
    doji = g["body_ratio"] <= cfg.doji_max_body_ratio
    top, bottom = g[["open", "close"]].max(axis=1), g[["open", "close"]].min(axis=1)
    inside = (top < top.shift(1)) & (bottom > bottom.shift(1))
    masks = {}
    for bull in (True, False):
        prefix, prev, cur = (
            ("bullish", "bearish", "bullish") if bull else ("bearish", "bullish", "bearish")
        )
        common = usable & large & inside & (g["direction"].shift(1) == prev)
        masks[f"{prefix}_harami"] = common & small & ~doji & (g["direction"] == cur)
        masks[f"{prefix}_harami_cross"] = common & doji
    middle = (g["open"].shift(1) + g["close"].shift(1)) / 2
    big = g["body_ratio"] >= cfg.large_body_min_ratio
    masks["piercing_line"] = (
        usable
        & large
        & big
        & (g["direction"].shift(1) == "bearish")
        & (g["direction"] == "bullish")
        & (g["open"] < g["low"].shift(1))
        & (g["close"] > middle)
        & (g["close"] < g["open"].shift(1))
    )
    masks["dark_cloud_cover"] = (
        usable
        & large
        & big
        & (g["direction"].shift(1) == "bullish")
        & (g["direction"] == "bearish")
        & (g["open"] > g["high"].shift(1))
        & (g["close"] < middle)
        & (g["close"] > g["open"].shift(1))
    )
    masks["dragonfly_doji"] = (
        g["usable"] & doji & (g["lower_wick_ratio"] >= 0.6) & (g["upper_wick_ratio"] <= 0.1)
    )
    masks["gravestone_doji"] = (
        g["usable"] & doji & (g["upper_wick_ratio"] >= 0.6) & (g["lower_wick_ratio"] <= 0.1)
    )
    masks["morning_doji_star"] = morning_star(g, cfg)[0] & doji.shift(1, fill_value=False)
    masks["evening_doji_star"] = evening_star(g, cfg)[0] & doji.shift(1, fill_value=False)
    return {name: mask.fillna(False) for name, mask in masks.items()}


def recognize(bars: pd.DataFrame, *, timeframe: str, as_of: datetime) -> pd.DataFrame:
    """Research observations; confirmation is known only after the next CLOSED candle.

    Support/resistance use twenty bars BEFORE the pattern, never future pivots. There
    is no parameter search, future-return target, model update, or order authorization.
    """
    stamp = pd.Timestamp(as_of)
    if stamp.tzinfo is None:
        raise ValueError("as_of must be timezone aware")
    duration = pd.Timedelta(minutes=TIMEFRAMES[timeframe])
    closed = bars.loc[bars.index + duration <= stamp].copy()
    if "is_closed" in closed:
        closed = closed.loc[closed["is_closed"].astype(bool)]
    analysis = CandlestickEngine().analyze(closed, timeframe=timeframe)
    g = analysis.geometry
    if g.empty:
        return pd.DataFrame()
    candidates: list[tuple[str, pd.Timestamp, float | None, str]] = [
        (str(r["pattern"]), pd.Timestamp(r["ts"]), float(r["strength"]), str(r["orientation"]))
        for r in analysis.observations.to_dict("records")
    ]
    for name, mask in additional_masks(g).items():
        candidates.extend((name, ts, None, CATALOG[name]["orientation"]) for ts in g.index[mask])
    records = []
    for name, ts, strength, orientation in candidates:
        lesson = CATALOG[name]
        i = cast(int, g.index.get_loc(ts))
        first = i - lesson["bars"] + 1
        if first < 0 or any(
            g.index[j] - g.index[j - 1] != duration for j in range(first + 1, i + 1)
        ):
            continue
        trend = str(g.iloc[first]["prior_trend"])
        section, prior = g.iloc[first : i + 1], g.iloc[max(0, first - 20) : first]
        low, high = float(section["low"].min()), float(section["high"].max())
        confirmation = "not_directional" if orientation == "neutral" else "pending"
        known_at = ts + duration
        if orientation != "neutral" and i + 1 < len(g) and g.index[i + 1] - ts == duration:
            nxt = g.iloc[i + 1]
            if bool(nxt["usable"]):
                up = orientation == "bullish"
                confirms = nxt["close"] > high if up else nxt["close"] < low
                invalidates = nxt["low"] < low if up else nxt["high"] > high
                confirmation = (
                    "ambiguous"
                    if confirms and invalidates
                    else "invalidated"
                    if invalidates
                    else "confirmed"
                    if confirms
                    else "not_confirmed"
                )
                known_at = g.index[i + 1] + duration
        records.append(
            {
                "ts": ts,
                "pattern": name,
                "orientation": orientation,
                "family": lesson["family"],
                "prior_trend": trend,
                "context_requirements_met": lesson["prior_trend"] is None
                or trend == lesson["prior_trend"],
                "pattern_low": low,
                "pattern_high": high,
                "prior_support_20": float(prior["low"].min()) if len(prior) == 20 else None,
                "prior_resistance_20": float(prior["high"].max()) if len(prior) == 20 else None,
                "confirmation": confirmation,
                "confirmation_known_at": known_at,
                "strength": strength,
                "strength_is_probability": False,
                "live_detector": lesson["live_detector"],
                "order_authorized": False,
                "explanation": explain_pattern(name),
            }
        )
    return (
        pd.DataFrame(records).sort_values(["ts", "pattern"]).reset_index(drop=True)
        if records
        else pd.DataFrame()
    )
