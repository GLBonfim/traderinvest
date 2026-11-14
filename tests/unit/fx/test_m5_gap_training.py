from pathlib import Path

from app.fx.research import m5_gap_training as mod


def test_gapfix_protocol_preserves_criteria_and_freezes_corrected_sources(monkeypatch):
    base = {
        "variants": {r: [3.0, 5.0] for r in ("reversao", "continuacao", "impulso")},
        "base_protocol": {"min_closed": 60},
        "thresholds": {"same": True},
    }
    monkeypatch.setattr(mod.balanced, "protocol", lambda _: base.copy())
    value = mod.protocol(Path("synthetic.pdf"))
    for key in ("variants", "base_protocol", "thresholds"):
        assert value[key] == base[key]
    assert sum(map(len, value["variants"].values())) == 6
    assert len(value["frozen_source_sha256"]) == 10
    assert "gapfix_wrapper.py" in value["frozen_source_sha256"]
    assert "OPEN" in value["correction"]


def test_manager_can_queue_only_the_explicit_offline_gapfix_module(tmp_path):
    from app.fx.research.m5_training_manager import validate_job

    job = {
        "id": "balanced-gapfix-v1",
        "module": "app.fx.research.m5_gap_training",
        "out": str(tmp_path / "one-run"),
    }
    assert validate_job(job, tmp_path) == (tmp_path / "one-run").resolve()
