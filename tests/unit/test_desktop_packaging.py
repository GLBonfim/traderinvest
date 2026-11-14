"""Distribution gates must reject a failed or stalled frozen engine."""

import importlib.util
import subprocess
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest


@pytest.fixture
def build_module() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "packaging" / "build.py"
    spec = importlib.util.spec_from_file_location("desktop_build", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("output", "code", "accepted"),
    [
        (b"ENGINE_READY port=1234\n", 0, True),
        (b"ENGINE_READY port=1234\n", 1, False),
        (b"", 0, False),
    ],
)
def test_engine_build_gate(
    build_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    output: bytes,
    code: int,
    accepted: bool,
) -> None:
    proc = Mock(returncode=code)
    proc.communicate.return_value = (output, b"engine diagnostic")
    monkeypatch.setattr(build_module.tempfile, "TemporaryDirectory", lambda: nullcontext(tmp_path))
    monkeypatch.setattr(build_module.subprocess, "Popen", Mock(return_value=proc))
    if accepted:
        build_module.smoke_test_engine()
    else:
        with pytest.raises(SystemExit, match="frozen engine failed"):
            build_module.smoke_test_engine()
    proc.communicate.assert_called_once_with(timeout=60)


def test_stalled_engine_is_killed_and_reaped(
    build_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    proc = Mock()
    proc.communicate.side_effect = [subprocess.TimeoutExpired("engine", 60), (b"", b"")]
    monkeypatch.setattr(build_module.tempfile, "TemporaryDirectory", lambda: nullcontext(tmp_path))
    monkeypatch.setattr(build_module.subprocess, "Popen", Mock(return_value=proc))
    with pytest.raises(SystemExit, match="orphan policy"):
        build_module.smoke_test_engine()
    proc.kill.assert_called_once_with()
    assert proc.communicate.call_count == 2


def test_installer_missing_is_optional(
    build_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(build_module.shutil, "which", lambda _: None)
    for key in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        monkeypatch.delenv(key, raising=False)
    assert build_module.compile_installer() is None
    assert "skipped" in capsys.readouterr().out


def test_installer_compiler_failure_is_not_ignored(
    build_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build_module.shutil, "which", lambda _: "ISCC.exe")
    run = Mock(side_effect=subprocess.CalledProcessError(2, "ISCC.exe"))
    monkeypatch.setattr(build_module.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        build_module.compile_installer()
    assert run.call_args.kwargs["check"] is True


@pytest.mark.parametrize(
    ("stdout", "code", "accepted"),
    [("auto-verificação OK", 0, True), ("", 0, False), ("auto-verificação OK", 1, False)],
)
def test_pilot_build_gate(
    build_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    code: int,
    accepted: bool,
) -> None:
    result = Mock(returncode=code, stdout=stdout.encode("utf-8"), stderr=b"")
    monkeypatch.setattr(build_module.subprocess, "run", Mock(return_value=result))
    if accepted:
        build_module.smoke_test_pilot()
    else:
        with pytest.raises(SystemExit, match="self-check"):
            build_module.smoke_test_pilot()


def test_clean_dist_only_inside_repository_dist(
    build_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(build_module, "ROOT", tmp_path)
    inside = tmp_path / "dist" / "TraderInvestFX"
    inside.mkdir(parents=True)
    (inside / "x.txt").write_text("x")
    build_module.clean_dist(inside)
    assert not inside.exists()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(SystemExit, match="refusing"):
        build_module.clean_dist(outside)
    with pytest.raises(SystemExit, match="refusing"):
        build_module.clean_dist(tmp_path / "dist")
    assert outside.exists()
