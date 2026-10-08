"""Plotly figure builders (pure functions; no Streamlit, no calculations of their own).

Restrained palette: muted tones for price direction; neutral greys/blues for overlays. No colour
is used to suggest "buy"/"sell" or certainty.
"""

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

UP, DOWN = "#4f7f72", "#9a5a55"  # muted: direction of the candle only
NEUTRAL = "#6b7280"
OVERLAY_COLORS = {
    "sma_20": "#2f5d8a",
    "sma_50": "#7a6aa8",
    "sma_200": "#a07c3c",
    "ema_20": "#3f88a6",
    "ema_50": "#8b7bb8",
    "ema_200": "#b08d57",
}
SERIES_COLORS = [
    "#2f5d8a",
    "#a07c3c",
    "#4f7f72",
    "#7a6aa8",
    "#9a5a55",
    "#3f88a6",
    "#6b7280",
    "#b08d57",
]
LAYOUT = {
    "template": "plotly_white",
    "margin": {"l": 50, "r": 20, "t": 40, "b": 30},
    "font": {"family": "Inter, Segoe UI, sans-serif", "size": 12, "color": "#1f2937"},
    "legend": {"orientation": "h", "y": 1.02, "x": 0, "yanchor": "bottom"},
    "hovermode": "x unified",
}


def candlestick_figure(
    bars: pd.DataFrame,
    *,
    overlays: dict[str, pd.Series] | None = None,
    bollinger: pd.DataFrame | None = None,
    swings: pd.DataFrame | None = None,
    events: pd.DataFrame | None = None,
    zones: list[dict[str, float | str]] | None = None,
    marker_ts: pd.Timestamp | None = None,
    title: str = "",
) -> go.Figure:
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, row_heights=[0.78, 0.22], vertical_spacing=0.03
    )
    fig.add_trace(
        go.Candlestick(
            x=bars.index,
            open=bars["open"],
            high=bars["high"],
            low=bars["low"],
            close=bars["close"],
            increasing={"line": {"color": UP}, "fillcolor": UP},
            decreasing={"line": {"color": DOWN}, "fillcolor": DOWN},
            name="OHLC",
            showlegend=False,
        ),
        row=1,
        col=1,
    )
    for name, s in (overlays or {}).items():
        fig.add_trace(
            go.Scatter(
                x=s.index,
                y=s,
                mode="lines",
                name=name,
                line={"width": 1.3, "color": OVERLAY_COLORS.get(name, NEUTRAL)},
            ),
            row=1,
            col=1,
        )
    if bollinger is not None:
        for col, dash in (("bb_upper", "dot"), ("bb_middle", "dash"), ("bb_lower", "dot")):
            fig.add_trace(
                go.Scatter(
                    x=bollinger.index,
                    y=bollinger[col],
                    mode="lines",
                    name=col,
                    line={"width": 1, "color": NEUTRAL, "dash": dash},
                ),
                row=1,
                col=1,
            )
    for z in zones or []:
        fig.add_hrect(
            y0=z["low"],
            y1=z["high"],
            line_width=0,
            opacity=0.12,
            fillcolor="#2f5d8a" if z["kind"] == "support" else "#a07c3c",
            annotation_text=str(z["kind"]),
            annotation_position="top left",
            row=1,
            col=1,
        )
    if swings is not None and len(swings):
        for kind, symbol in (("high", "triangle-down"), ("low", "triangle-up")):
            sw = swings[swings["kind"] == kind]
            fig.add_trace(
                go.Scatter(
                    x=sw["pivot_ts"],
                    y=sw["price"],
                    mode="markers+text",
                    text=sw["label"],
                    textposition="top center" if kind == "high" else "bottom center",
                    marker={"symbol": symbol, "size": 9, "color": "#374151"},
                    name=f"confirmed swing {kind}",
                    customdata=sw["confirmed_at"],
                    hovertemplate="%{text} %{y:.2f}<br>confirmed %{customdata}<extra></extra>",
                ),
                row=1,
                col=1,
            )
    if events is not None and len(events):
        fig.add_trace(
            go.Scatter(
                x=events["ts"],
                y=events["close"],
                mode="markers",
                name="price-action event",
                marker={"symbol": "diamond-open", "size": 9, "color": "#7a6aa8"},
                text=events["event_type"] + " " + events["direction"].astype(str),
                hovertemplate="%{text}<extra></extra>",
            ),
            row=1,
            col=1,
        )
    if marker_ts is not None:
        fig.add_vline(x=marker_ts, line={"color": NEUTRAL, "width": 1, "dash": "dot"})
    colors = [UP if c >= o else DOWN for o, c in zip(bars["open"], bars["close"], strict=True)]
    fig.add_trace(
        go.Bar(
            x=bars.index,
            y=bars["volume"],
            marker_color=colors,
            opacity=0.55,
            name="volume",
            showlegend=False,
        ),
        row=2,
        col=1,
    )
    fig.update_layout(**LAYOUT, title=title, height=620, xaxis_rangeslider_visible=False)
    fig.update_xaxes(rangebreaks=[{"bounds": ["sat", "mon"]}])
    fig.update_yaxes(title_text="price", row=1, col=1)
    fig.update_yaxes(title_text="volume", row=2, col=1)
    return fig


def lines_figure(
    frame: pd.DataFrame,
    *,
    title: str = "",
    hlines: tuple[float, ...] = (),
    height: int = 260,
    yformat: str | None = None,
) -> go.Figure:
    fig = go.Figure()
    for i, col in enumerate(frame.columns):
        fig.add_trace(
            go.Scatter(
                x=frame.index,
                y=frame[col],
                mode="lines",
                name=str(col),
                line={"width": 1.4, "color": SERIES_COLORS[i % len(SERIES_COLORS)]},
            )
        )
    for h in hlines:
        fig.add_hline(y=h, line={"color": "#9ca3af", "width": 1, "dash": "dot"})
    fig.update_layout(**LAYOUT, title=title, height=height)
    if yformat:
        fig.update_yaxes(tickformat=yformat)
    return fig


def histogram_figure(
    values: pd.Series, *, title: str = "", nbins: int = 40, xformat: str | None = None
) -> go.Figure:
    fig = go.Figure(go.Histogram(x=values, nbinsx=nbins, marker_color="#2f5d8a", opacity=0.8))
    fig.update_layout(**LAYOUT, title=title, height=280, bargap=0.05)
    if xformat:
        fig.update_xaxes(tickformat=xformat)
    return fig


def calibration_figure(table: pd.DataFrame, *, title: str = "") -> go.Figure:
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=[0, 1],
            y=[0, 1],
            mode="lines",
            name="perfect calibration",
            line={"color": "#9ca3af", "dash": "dot"},
        )
    )
    fig.add_trace(
        go.Scatter(
            x=table["mean_predicted"],
            y=table["observed_rate"],
            mode="markers+lines",
            name="observed",
            text=table["n"],
            marker={"color": "#2f5d8a"},
            hovertemplate="pred %{x:.3f} obs %{y:.3f} n=%{text}<extra></extra>",
        )
    )
    lo = min(table["mean_predicted"].min(), table["observed_rate"].min()) - 0.02
    hi = max(table["mean_predicted"].max(), table["observed_rate"].max()) + 0.02
    fig.update_layout(**LAYOUT, title=title, height=320)
    fig.update_xaxes(title_text="mean predicted P(up)", range=[lo, hi])
    fig.update_yaxes(title_text="observed up-rate", range=[lo, hi])
    return fig
