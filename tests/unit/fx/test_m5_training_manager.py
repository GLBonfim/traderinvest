import pytest

from app.fx.research.m5_training_manager import validate_job


def test_manager_accepts_only_dedicated_offline_research_paths(tmp_path):
    job = {
        "id": "session-v3",
        "module": "app.fx.research.m5_session_training",
        "out": str(tmp_path / "run"),
    }
    assert validate_job(job, tmp_path) == tmp_path / "run"
    for mutation in (
        {"module": "app.fx.ibkr.candle_lab"},
        {"out": str(tmp_path.parent / "outside")},
        {"out": str(tmp_path)},
        {"id": "../../escape"},
    ):
        with pytest.raises(ValueError):
            validate_job({**job, **mutation}, tmp_path)
