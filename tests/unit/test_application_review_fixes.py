"""Offline application review regressions."""
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from PySide6.QtCore import QByteArray
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.data.calendar import TradingCalendar
from app.data.normalization import normalize_daily
from app.data.validation import validate_daily
from app.database.models import Instrument, PriceBar
from app.desktop.engine_client import EngineClient, EngineSupervisor
from app.engine.instance import EngineAlreadyRunningError
from app.engine.service import EngineService
from app.operations.config import OperationsConfig
from app.operations.services import SystemServices
from tests.factories import AFTER_JULY_2024, provider_bars, regular_rows


@pytest.mark.parametrize("split", range(1, len(b"ENGINE_READY port=12345\n")))
def test_supervisor_waits_for_complete_readiness_line(split: int) -> None:
    client = EngineClient()
    client.connect_to = Mock()
    supervisor = EngineSupervisor(client)
    line = b"ENGINE_READY port=12345\n"
    supervisor.proc = Mock()
    supervisor.proc.readAllStandardOutput.side_effect = [
        QByteArray(line[:split]), QByteArray(line[split:])
    ]
    supervisor._on_stdout()
    assert supervisor.port is None
    supervisor._on_stdout()
    assert supervisor.port == 12345
    client.connect_to.assert_called_once_with(12345, supervisor.token)


@pytest.mark.parametrize("column", ["open", "high", "low", "close", "volume", "adj_close"])
@pytest.mark.parametrize("value", [np.inf, -np.inf])
def test_daily_validation_excludes_infinity(column: str, value: float) -> None:
    cal = TradingCalendar("XNYS")
    frame = normalize_daily(provider_bars(regular_rows()), cal).frame
    bad_ts = frame.index[0]
    frame.loc[bad_ts, column] = value
    result = validate_daily(
        frame, cal.sessions(date(2024, 7, 1), date(2024, 7, 9)),
        as_of=AFTER_JULY_2024, requested_end=date(2024, 7, 9),
    )
    assert bad_ts not in result.valid.index
    assert len(result.valid) == 5
    assert any(i.check_name == "non_finite_value" and i.bar_ts == bad_ts for i in result.issues)


@pytest.mark.parametrize("target_has_earlier", [False, True])
def test_previous_session_requirement_ignores_other_instruments(target_has_earlier: bool) -> None:
    engine = create_engine("sqlite://")
    Instrument.__table__.create(engine)
    PriceBar.__table__.create(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    cal = TradingCalendar("XNYS")
    with sf() as session:
        for iid, symbol in [(1, "SPY"), (2, "OTHER")]:
            session.add(Instrument(id=iid, symbol=symbol, exchange="ARCX", underlying=symbol,
                                   asset_class="etf", currency="USD", timezone="America/New_York"))
        dates = [(1, "2024-07-03"), (2, "2024-07-01")]
        if target_has_earlier:
            dates.append((1, "2024-07-01"))
        for bid, (iid, day) in enumerate(dates, 1):
            d = date.fromisoformat(day)
            ts = cal.sessions(d, d)["open_utc"].iloc[0]
            session.add(PriceBar(id=bid, instrument_id=iid, provider="yfinance", timeframe="1d",
                                 ts=ts.to_pydatetime(), open=100, high=101, low=99, close=100,
                                 volume=1000, is_closed=True))
        session.commit()
    svc = SystemServices.__new__(SystemServices)
    svc.sf, svc.cal, svc.cfg = sf, cal, OperationsConfig()
    svc.provider = SimpleNamespace(name="yfinance")
    try:
        result = svc.validate(date(2024, 7, 3))
        assert result["ok"] is (not target_has_earlier)
        if target_has_earlier:
            assert "previous session 2024-07-02 not stored" in result["detail"]
    finally:
        engine.dispose()


def test_refused_second_engine_preserves_active_metadata(tmp_path: Path) -> None:
    metadata = tmp_path / "engine.json"
    metadata.write_text('{"pid":123,"port":456}', encoding="utf-8")
    service = EngineService(tmp_path, "test-session-token")
    service.lock = Mock()
    service.lock.acquire.side_effect = EngineAlreadyRunningError("busy")
    coroutine = service.run()
    try:
        with pytest.raises(EngineAlreadyRunningError):
            coroutine.send(None)
    finally:
        coroutine.close()
    assert metadata.read_text(encoding="utf-8") == '{"pid":123,"port":456}'
