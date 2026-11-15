"""Market data for a causal replay: EUR total-return unit prices, cash rate and calendars.

Everything is built from the frozen raw files listed in the manifest. A replay may only load
rows up to `through`; rows on or after the sealed boundary need a valid sealed claim
(`app.trend.guard`). Per asset, the loaded history starts 201 own sessions before the
evaluable start (the SMA and volatility warm-up) and ends at `through`.

Unit prices (design v2 section 6.4): ``price_s(t) = unit_scale * L_s(t)``, where ``L_s`` is the
asset's EUR total-return level in cost scenario ``s`` (TER adjustment of section 8.2),
normalised to 1 at its last own session on or before the evaluable start, and `unit_scale` is
a manifest constant (`unit_anchors`) that makes the realistic level equal the executable
product's EUR close on a fixed anchor date inside the development partition.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.trend.audit import MAX_FX_STALENESS, fx_staleness
from app.trend.costs import SCENARIOS, ter_adjustment_pct
from app.trend.loaders import read_ecb_csv, read_price_csv, total_return_factors
from app.trend.universe import AssetSpec, Universe

WARMUP_ROWS = 201  # 200 closes for the SMA plus one for the first return
SPLICE = pd.Timestamp("2019-10-01")


class DataError(ValueError):
    """A replay input fails closed (stale FX, missing file, sealed rows without a claim)."""


@dataclass(frozen=True)
class AssetSeries:
    id: str
    prices: dict[str, pd.Series]  # scenario -> EUR unit price on the asset's own sessions

    @property
    def sessions(self) -> pd.DatetimeIndex:
        return pd.DatetimeIndex(self.prices["realistic"].index)


@dataclass(frozen=True)
class Market:
    master: pd.DatetimeIndex  # union of the universe assets' own sessions (loaded range)
    assets: dict[str, AssetSeries]  # universe assets and benchmark-only legs
    universe_ids: tuple[str, ...]
    cash_rate: pd.Series  # annual decimal rate (haircut included), indexed by rate date
    scheduled: pd.DatetimeIndex  # exchange-scheduled sessions (XNYS u XETR), known in advance


def eur_level(level: pd.Series, fx_usd_per_eur: pd.Series) -> pd.Series:
    """USD level -> EUR level with the ECB rate of the same date or the latest earlier one;
    more than MAX_FX_STALENESS weekdays of staleness fails closed."""
    dates = pd.DatetimeIndex(level.index)
    stale = fx_staleness(fx_usd_per_eur, dates)
    if (stale < 0).any() or (stale > MAX_FX_STALENESS).any():
        bad = dates[(stale.to_numpy() < 0) | (stale.to_numpy() > MAX_FX_STALENESS)]
        raise DataError(f"stale or missing ECB rate on {bad[0].date().isoformat()}")
    rate = fx_usd_per_eur.dropna().reindex(dates, method="ffill")
    return pd.Series(level.to_numpy() / rate.to_numpy(), index=level.index, name=level.name)


def ter_factors(index: pd.DatetimeIndex, annual_pct: float) -> np.ndarray:
    """Per-session multiplicative TER adjustment ``1 + a * days / 365`` (first row = 1)."""
    days = np.zeros(len(index))
    if len(index) > 1:
        days[1:] = np.diff(index.values).astype("timedelta64[D]").astype("float64")
    return 1.0 + (annual_pct / 100.0) * days / 365.0


def scenario_levels(
    frame: pd.DataFrame,
    fx: pd.Series | None,
    kind: str,
    ter_eu: float,
    ter_proxy: float | None,
    ref: pd.Timestamp,
) -> dict[str, pd.Series]:
    """EUR total-return level per scenario, normalised to 1 at session `ref`."""
    factors = total_return_factors(frame["close"], frame["dividends"]).fillna(1.0)
    base = factors.cumprod()
    if fx is not None:
        base = eur_level(base, fx)
    index = pd.DatetimeIndex(base.index)
    out: dict[str, pd.Series] = {}
    for scenario in SCENARIOS:
        adj = np.cumprod(ter_factors(index, ter_adjustment_pct(kind, ter_eu, ter_proxy, scenario)))
        level = base * adj
        out[scenario] = level / float(level.loc[ref])
    return out


def reference_session(sessions: pd.DatetimeIndex, evaluable_start: pd.Timestamp) -> pd.Timestamp:
    """The asset's last own session on or before the evaluable start."""
    before = sessions[sessions <= evaluable_start]
    if before.empty:
        raise DataError("asset has no session before the evaluable start")
    return pd.Timestamp(before[-1])


def warmup_slice(frame: pd.DataFrame, ref: pd.Timestamp) -> pd.DataFrame:
    pos = int(pd.DatetimeIndex(frame.index).searchsorted(ref))
    return frame.iloc[max(0, pos - WARMUP_ROWS + 1) :]


def cash_rate_series(eonia: pd.Series, estr: pd.Series, raw_cash: dict[str, Any]) -> pd.Series:
    """EONIA + adjustment until the splice, EUR STR from it; minus the haircut; decimal."""
    old = eonia.loc[eonia.index < SPLICE] + float(raw_cash["eonia"]["adjustment_pp"])
    new = estr.loc[estr.index >= SPLICE]
    chained = pd.concat([old, new]) + float(raw_cash["haircut_pp"])
    return pd.Series(chained.to_numpy() / 100.0, index=chained.index, name="cash_rate")


def interest(cash: float, rate: pd.Series, prev_day: pd.Timestamp, day: pd.Timestamp) -> float:
    """Interest on `cash` from prev_day to day, ACT/360, at the rate dated on or before
    prev_day (published on the morning of `day`, so known at D(day))."""
    known = rate.loc[rate.index <= prev_day]
    if known.empty:
        raise DataError(f"no cash rate on or before {prev_day.date().isoformat()}")
    return cash * float(known.iloc[-1]) * (day - prev_day).days / 360.0


def scheduled_sessions(start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    """Union of XNYS and XETR scheduled sessions (published calendars, known in advance)."""
    from app.trend.audit import calendar_sessions

    return calendar_sessions("XNYS", start, end).union(calendar_sessions("XETR", start, end))


def is_period_end(
    day: pd.Timestamp, scheduled: pd.DatetimeIndex, freq: str, master_prev: pd.Timestamp | None
) -> bool:
    """D(day) is the period's rebalance decision if no scheduled session follows `day` in the
    same month (freq 'M') or year (freq 'Y'). Fallback, from the past only: if the previous
    master day was in an earlier period that had no rebalance flag, `day` takes it."""
    period = day.to_period(freq)
    later = scheduled[(scheduled > day) & (scheduled.to_period(freq) == period)]
    if later.empty:
        return True
    if master_prev is None or master_prev.to_period(freq) == period:
        return False
    prev_period = master_prev.to_period(freq)
    after_prev = scheduled[(scheduled > master_prev) & (scheduled.to_period(freq) == prev_period)]
    return not after_prev.empty  # the previous day was not flagged, so its period was missed


def _series_inputs(
    asset: AssetSpec, manifest: dict[str, Any], data_root: Path
) -> tuple[Path, str | None]:
    roles = {f["role"]: data_root / f["path"] for f in manifest["files"]}
    path = roles.get(f"price:{asset.research.symbol}")
    if path is None:
        raise DataError(f"manifest lacks the price file of {asset.research.symbol}")
    return path, ("fx:USD" if asset.research.currency == "USD" else None)


def sealed_start(manifest: dict[str, Any]) -> pd.Timestamp:
    return pd.Timestamp(manifest["partitions"]["sealed"]["start"])


def build_market(
    manifest: dict[str, Any],
    universe: Universe,
    data_root: Path,
    *,
    through: str,
    sealed_claim: dict[str, Any] | None = None,
    unit_scales: dict[str, float] | None = None,
) -> Market:
    """Load every series up to `through` (inclusive). Raises DataError on any failure."""
    end = pd.Timestamp(through)
    if end >= sealed_start(manifest):
        from app.trend.guard import require_published_claim

        require_published_claim(sealed_claim, manifest)
    if end > pd.Timestamp(manifest["data_end"]):
        raise DataError("`through` is after the manifest's data end")
    roles = {f["role"]: data_root / f["path"] for f in manifest["files"]}
    fx = read_ecb_csv(roles["fx:USD"], manifest["data_end"])
    fx = fx.loc[fx.index <= end]
    evaluable = pd.Timestamp(manifest["window"]["evaluable_start"])
    scales = unit_scales
    if scales is None:
        scales = {k: float(v["unit_scale"]) for k, v in manifest["unit_anchors"].items()}
    assets: dict[str, AssetSeries] = {}
    for asset in universe.all_series():
        path, fx_role = _series_inputs(asset, manifest, data_root)
        frame = read_price_csv(path, manifest["data_end"])
        frame = frame.loc[frame.index <= end]
        ref = reference_session(pd.DatetimeIndex(frame.index), evaluable)
        frame = warmup_slice(frame, ref)
        levels = scenario_levels(
            frame,
            fx if fx_role else None,
            asset.research.kind,
            asset.product.ter_pct,
            asset.research.ter_pct,
            ref,
        )
        scale = scales[asset.id]
        assets[asset.id] = AssetSeries(asset.id, {s: lv * scale for s, lv in levels.items()})
    ids = tuple(a.id for a in universe.assets)
    master = pd.DatetimeIndex(sorted(set().union(*(set(assets[i].sessions) for i in ids))))
    eonia = read_ecb_csv(roles["rate:eonia"], manifest["data_end"])
    estr = read_ecb_csv(roles["rate:estr"], manifest["data_end"])
    rate = cash_rate_series(eonia, estr, universe.raw["cash_rate"])
    rate = rate.loc[rate.index <= end]
    scheduled = scheduled_sessions(master[0], end + pd.Timedelta(days=40))
    return Market(master, assets, ids, rate, scheduled)


def unit_anchors(
    manifest: dict[str, Any], universe: Universe, data_root: Path
) -> dict[str, dict[str, Any]]:
    """Anchor constants for unit prices, computed with development data only.

    Own series: the product's own close on its reference session (scale = that close).
    Proxies: the product's first Xetra close on or after the evaluable start, divided by the
    proxy's realistic EUR level on that date (latest proxy session on or before it).
    """
    dev_end = manifest["partitions"]["development"]["end"]
    market = build_market(
        manifest,
        universe,
        data_root,
        through=dev_end,
        unit_scales=dict.fromkeys((a.id for a in universe.all_series()), 1.0),
    )
    roles = {f["role"]: data_root / f["path"] for f in manifest["files"]}
    evaluable = pd.Timestamp(manifest["window"]["evaluable_start"])
    out: dict[str, dict[str, Any]] = {}
    for asset in universe.all_series():
        level = market.assets[asset.id].prices["realistic"]
        if asset.research.kind == "product_own":
            frame = read_price_csv(roles[f"price:{asset.research.symbol}"], dev_end)
            ref = reference_session(pd.DatetimeIndex(frame.index), evaluable)
            pos = int(pd.DatetimeIndex(frame.index).searchsorted(ref))
            close = float(frame["close"].to_numpy()[pos])
            symbol, date = asset.research.symbol, ref
        else:
            symbol = f"{asset.product.ticker}.DE"
            frame = read_price_csv(roles[f"anchor:{symbol}"], dev_end)
            after = frame.loc[frame.index >= evaluable]
            if after.empty:
                raise DataError(f"{symbol}: no product close inside the development partition")
            date, close = pd.Timestamp(after.index[0]), float(after["close"].iloc[0])
        at = float(level.loc[level.index <= date].iloc[-1])
        out[asset.id] = {
            "symbol": symbol,
            "date": date.date().isoformat(),
            "close_eur": close,
            "unit_scale": close / at,
        }
    return out
