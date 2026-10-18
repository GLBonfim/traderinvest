import hashlib
import json
import zipfile
from pathlib import Path

from app.fx.research.m5_job import export


def test_partial_job_never_exports_models_as_finished(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    export(
        tmp_path,
        outputs,
        {
            "phase": "downloading",
            "observed_at": "2026-10-18T17:00:00Z",
            "downloaded_chunks": 8,
            "expected_chunks": 1190,
        },
    )
    assert not (outputs / "modelos-e-validacao-m5.zip").exists()
    assert "Nenhum resultado financeiro" in (outputs / "relatorio-treinamento-m5.md").read_text(
        encoding="utf-8"
    )


def test_completed_bundle_preserves_rejection_and_checksum(tmp_path: Path) -> None:
    out, outputs = tmp_path / "run", tmp_path / "outputs"
    out.mkdir()
    (out / "report.md").write_text("NO TRADE", encoding="utf-8")
    (out / "selected.json").write_text(
        json.dumps(
            {
                "statistically_approved": False,
                "active_model_ids": [],
            }
        ),
        encoding="utf-8",
    )
    export(out, outputs, {"phase": "completed", "active_model_ids": []})
    archive = outputs / "modelos-e-validacao-m5.zip"
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        assert json.loads(z.read("selected.json"))["active_model_ids"] == []
    assert (
        hashlib.sha256(archive.read_bytes()).hexdigest() in (outputs / "SHA256-m5.txt").read_text()
    )
