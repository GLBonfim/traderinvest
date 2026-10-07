import json
import logging
from datetime import datetime

import pytest

from app.core.logging import REDACTED, configure_logging, get_logger


def _last_json_line(out: str) -> dict[str, object]:
    line = [ln for ln in out.strip().splitlines() if ln.startswith("{")][-1]
    parsed: dict[str, object] = json.loads(line)
    return parsed


def test_json_logs_are_structured_with_utc_timestamp(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    get_logger("test").info("something.happened", asset="SPY")
    record = _last_json_line(capsys.readouterr().out)
    assert record["event"] == "something.happened"
    assert record["level"] == "info"
    assert record["asset"] == "SPY"
    ts = datetime.fromisoformat(str(record["timestamp"]))
    assert ts.utcoffset() is not None and ts.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_sensitive_fields_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    get_logger("test").info(
        "auth", postgres_password="p", api_key="k", access_token="t", client_secret="s"
    )
    out = capsys.readouterr().out
    record = _last_json_line(out)
    for key in ("postgres_password", "api_key", "access_token", "client_secret"):
        assert record[key] == REDACTED
    assert '"p"' not in out


def test_stdlib_loggers_share_the_json_pipeline(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    logging.getLogger("uvicorn.error").warning("from stdlib")
    record = _last_json_line(capsys.readouterr().out)
    assert record["event"] == "from stdlib"
    assert record["level"] == "warning"
