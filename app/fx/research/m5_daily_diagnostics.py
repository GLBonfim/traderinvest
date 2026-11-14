"""Daily realized simulation diagnostics. Not daily mark-to-market equity or approval."""

from typing import Any

import numpy as np
import pandas as pd

from app.fx.models import pair
from app.fx.research.backtest import EUR_TO


def daily_realized(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {
            "days_with_closes": 0,
            "positive_days": 0,
            "negative_days": 0,
            "total_net_eur_estimated": 0.0,
            "daily": [],
            "includes_unrealized": False,
            "approved": False,
        }
    values = trades.r_net * trades.units * trades.stop_dist
    conversion = np.array([EUR_TO[pair(s).quote] for s in trades["pair"]])
    cash = pd.Series(
        values.to_numpy(float) / conversion,
        index=pd.DatetimeIndex(pd.to_datetime(trades.exit_ts, utc=True)),
    )
    daily = cash.groupby(pd.DatetimeIndex(cash.index).floor("D")).sum().sort_index()
    return {
        "days_with_closes": len(daily),
        "positive_days": int((daily > 0).sum()),
        "negative_days": int((daily < 0).sum()),
        "flat_days_with_closes": int((daily == 0).sum()),
        "total_net_eur_estimated": float(daily.sum()),
        "worst_day_eur_estimated": float(daily.min()),
        "best_day_eur_estimated": float(daily.max()),
        "daily": [
            {"date_utc": str(pd.Timestamp(str(day)).date()), "net_eur_estimated": float(value)}
            for day, value in daily.items()
        ],
        "conversion": "Frozen research EUR_TO rates; not confirmed live account cash.",
        "includes_unrealized": False,
        "approved": False,
        "limitation": "Realized closes only; no-closing days omitted; open MTM required.",
    }
