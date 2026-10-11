"""Phase 5B.1 regression: stored `occurred_at` values mix ISO-8601 variants (with and without
fractional seconds); both the alert filter and the Alerts page must parse every value."""

import ast
import inspect
import textwrap
from datetime import date

import pandas as pd
import pytest

from app.dashboard.config import REPO_ROOT
from app.dashboard.services import alerts as alerts_svc

MIXED = [
    "2026-10-09T13:49:37+00:00",  # no fraction first: inferring from it used to fail
    "2026-10-09T13:49:37.271934+00:00",
    "2026-10-10T02:15:00.5+00:00",
    "2026-10-10T21:00:00Z",
]


def test_alert_times_parses_mixed_iso8601_exactly() -> None:
    got = alerts_svc.alert_times(pd.Series(MIXED))
    assert list(got) == [pd.Timestamp(v).tz_convert("UTC") for v in MIXED]
    assert str(got.dt.tz) == "UTC"
    reversed_order = alerts_svc.alert_times(pd.Series(MIXED[::-1]))  # fraction first
    assert list(reversed_order) == list(got)[::-1]


def test_filter_alerts_by_date_with_mixed_timestamps() -> None:
    alerts = pd.DataFrame({"alert_id": list("abcd"), "occurred_at": MIXED})
    # New York dates: a, b -> 2026-10-09; c (02:15 UTC) -> 2026-10-09; d -> 2026-10-10
    out = alerts_svc.filter_alerts(alerts, start=date(2026, 10, 10), end=date(2026, 10, 10))
    assert list(out["alert_id"]) == ["d"]
    out = alerts_svc.filter_alerts(alerts, end=date(2026, 10, 9))
    assert list(out["alert_id"]) == ["a", "b", "c"]


def test_alerts_page_uses_the_shared_parser() -> None:
    from app.dashboard.ui import pages

    tree = ast.parse(textwrap.dedent(inspect.getsource(pages.alerts_monitoring)))
    calls = [ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert "alerts_svc.alert_times" in calls


def test_no_format_inferring_parse_of_occurred_at_in_the_dashboard() -> None:
    offenders = []
    for path in (REPO_ROOT / "app" / "dashboard").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and ast.unparse(node.func).endswith("to_datetime")
                and "occurred_at" in ast.unparse(node)
                and not any(k.arg == "format" for k in node.keywords)
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


# ── M2: start and end together are evaluated on one index (no reindexing warning) ──

# NY dates: 2026-10-08, 2026-10-09 (x2), 2026-10-10, 2026-10-11
BOUNDS = pd.DataFrame(
    {
        "alert_id": list("abcdef"),
        "severity": ["WARNING", "INFO", "WARNING", "CRITICAL", "WARNING", "INFO"],
        "occurred_at": [
            "2026-10-08T15:00:00+00:00",
            "2026-10-09T14:00:00.250+00:00",
            "2026-10-09T23:30:00Z",  # 19:30 NY, still 10-09
            "2026-10-10T03:59:59.999999+00:00",  # 23:59:59 NY on 10-09
            "2026-10-10T04:00:00+00:00",  # 00:00 NY on 10-10
            "2026-10-11T12:00:00+00:00",
        ],
    }
)


@pytest.mark.filterwarnings("error::UserWarning")
def test_start_and_end_both_exclude_rows_bounds_inclusive_without_warning() -> None:
    out = alerts_svc.filter_alerts(BOUNDS, start=date(2026, 10, 9), end=date(2026, 10, 10))
    assert list(out["alert_id"]) == ["b", "c", "d", "e"]  # a (before) and f (after) excluded
    assert list(out.index) == [0, 1, 2, 3]


@pytest.mark.filterwarnings("error::UserWarning")
def test_date_filter_after_severity_filter_non_contiguous_index() -> None:
    out = alerts_svc.filter_alerts(
        BOUNDS, severities=["WARNING", "CRITICAL"], start=date(2026, 10, 9), end=date(2026, 10, 9)
    )
    assert list(out["alert_id"]) == ["c", "d"]  # rows 2 and 3 of the severity-filtered view


@pytest.mark.filterwarnings("error::UserWarning")
def test_date_filters_with_empty_results() -> None:
    assert alerts_svc.filter_alerts(BOUNDS, start=date(2026, 10, 12)).empty
    assert alerts_svc.filter_alerts(BOUNDS, end=date(2026, 10, 7)).empty
    assert alerts_svc.filter_alerts(BOUNDS, start=date(2026, 10, 11), end=date(2026, 10, 9)).empty
    empty = alerts_svc.filter_alerts(
        BOUNDS, severities=["DEBUG"], start=date(2026, 10, 9), end=date(2026, 10, 10)
    )
    assert empty.empty and list(empty.columns) == list(BOUNDS.columns)


@pytest.mark.filterwarnings("error::UserWarning")
def test_single_bound_results_unchanged() -> None:
    assert list(alerts_svc.filter_alerts(BOUNDS, start=date(2026, 10, 10))["alert_id"]) == [
        "e",
        "f",
    ]
    assert list(alerts_svc.filter_alerts(BOUNDS, end=date(2026, 10, 9))["alert_id"]) == list("abcd")
    assert list(alerts_svc.filter_alerts(BOUNDS)["alert_id"]) == list("abcdef")
