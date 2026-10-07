"""Target definition (fixed before any result was seen).

For the feature row observed at the close of session T:
    target(T) = 1 if raw close(T+1) > raw close(T) else 0      (an unchanged close counts as 0)
    target_timestamp(T) = session close of T+1 (when the label becomes known)
    prediction timestamp = close of T (with the features of T)
    execution (Phase 7/8 convention) = open of T+1
The target is the ONLY column that uses T+1 information; it never enters the feature vector.
The last row has no T+1 in the data: target = NaN (excluded from fitting and scoring, but a
prediction is still made, as it would be live).
Raw close is used (no adjusted close). Note: the close-to-close target includes the overnight
gap close(T)->open(T+1), which the next-open execution convention cannot capture.
"""

import pandas as pd

TARGET_NAME = "next_session_close_to_close_up"
HORIZON = 1


def make_target(close: pd.Series, observed_at: pd.Series) -> pd.DataFrame:
    nxt = close.shift(-HORIZON)
    target = (nxt > close).astype("float64").where(nxt.notna())
    return pd.DataFrame(
        {"target": target, "target_timestamp": observed_at.shift(-HORIZON)}, index=close.index
    )
