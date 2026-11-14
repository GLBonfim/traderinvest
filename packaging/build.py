"""Builds the Windows desktop package: `uv run --group build python packaging/build.py`.

Steps: render the procedural icon (.ico, several sizes), write the Windows version resource,
run PyInstaller (one-folder build in dist/TraderInvestFX), smoke-test the engine mode of the
built executable, and zip a portable archive. No signing (see docs/commercial-readiness.md).
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "packaging"
DIST = ROOT / "dist" / "TraderInvestFX"

sys.path.insert(0, str(ROOT))


def render_icon() -> Path:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PIL import Image
    from PySide6.QtGui import QGuiApplication

    from app.desktop.icon import icon_image

    _app = QGuiApplication.instance() or QGuiApplication([])
    BUILD.mkdir(parents=True, exist_ok=True)
    png = BUILD / "icon_256.png"
    icon_image(256).save(str(png))
    ico = BUILD / "icon.ico"
    Image.open(png).save(
        ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    )
    return ico


def version_resource() -> Path:
    from app.desktop import APP_NAME, APP_VERSION, ORG_NAME

    nums = [int(x) for x in APP_VERSION.split(".")] + [0]
    t = tuple(nums[:4])
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('080904B0', [
      StringStruct('CompanyName', '{ORG_NAME}'),
      StringStruct('FileDescription', '{APP_NAME} - simulação Forex (sem dinheiro real)'),
      StringStruct('FileVersion', '{APP_VERSION}'),
      StringStruct('InternalName', 'TraderInvestFX'),
      StringStruct('OriginalFilename', 'TraderInvestFX.exe'),
      StringStruct('ProductName', '{APP_NAME}'),
      StringStruct('ProductVersion', '{APP_VERSION}')])]),
        VarFileInfo([VarStruct('Translation', [2057, 1200])])]
)
"""
    path = BUILD / "version_info.txt"
    path.write_text(text, encoding="utf-8")
    return path


def pyinstaller() -> None:
    subprocess.run(  # noqa: S603 - fixed argv built from repository paths
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(ROOT / "dist"),
            "--workpath",
            str(BUILD / "work"),
            str(ROOT / "packaging" / "traderinvest_fx.spec"),
        ],
        check=True,
        cwd=ROOT,
    )


def smoke_test_engine() -> None:
    """The frozen exe must run the engine (`--engine`) and print ENGINE_READY."""
    exe = DIST / "TraderInvestFX.exe"
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "TRADERINVEST_ENGINE_TOKEN": "s" * 40}
        cfg = Path(tmp) / "config.json"
        cfg.write_text('{"simulation": {"warm_up_minutes": 1200}}', encoding="utf-8")
        argv = [str(exe), "--engine", "--data-dir", tmp, "--orphan-timeout", "3"]
        proc = subprocess.Popen(  # noqa: S603 - the executable this script just built
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
        )
        started = time.monotonic()
        try:
            stdout, stderr = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.communicate()  # reap the child before cleaning up its data directory
            raise SystemExit("engine did not stop by itself (orphan policy)") from exc
        output = stdout.decode("utf-8", errors="replace")
        ready = any(line.startswith("ENGINE_READY ") for line in output.splitlines())
        print(
            f"engine smoke test: ready={ready}, exit={proc.returncode} "
            f"after {time.monotonic() - started:.1f}s"
        )
        if not ready or proc.returncode != 0:
            diagnostic = stderr.decode("utf-8", errors="replace")[-4000:]
            raise SystemExit(f"the frozen engine failed (exit {proc.returncode}): {diagnostic}")


def smoke_test_ui() -> None:
    """The frozen UI must start its engine, receive snapshots and stop it (offscreen)."""
    exe = DIST / "TraderInvestFX.exe"
    shot = BUILD / "smoke_ui.png"
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "config.json").write_text(
            '{"simulation": {"warm_up_minutes": 1200}}', encoding="utf-8"
        )
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "TRADERINVEST_FX_HOME": tmp}
        env.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
        rc = subprocess.run(  # noqa: S603 - the executable this script just built
            [str(exe), "--smoke-test", str(shot)], env=env, timeout=180, check=False
        ).returncode
    print(f"UI smoke test: exit {rc}, screenshot {shot}")
    if rc != 0:
        raise SystemExit("the frozen UI did not receive engine data")


def clean_dist(target: Path | None = None) -> None:
    """Deletes the previous build only when it is a real folder strictly inside dist/ of this
    repository (never a link/junction or anything that resolves elsewhere)."""
    target = DIST if target is None else target
    if not target.exists() and not target.is_symlink():
        return
    resolved, root = target.resolve(), (ROOT / "dist").resolve()
    if target.is_symlink() or not resolved.is_relative_to(root) or resolved == root:
        raise SystemExit(f"refusing to delete {target} (resolves to {resolved})")
    print(f"clean: {resolved}")
    shutil.rmtree(resolved)


def check_pilot_run(run: Path) -> None:
    """The shipped selection must be a valid experimental registry (never approval)."""
    from app.fx.agents.trained import load_models

    active = load_models(run / "selected.json")
    names = ", ".join(m.model_id for m in active) or "NENHUM elegível (NO TRADE)"
    print(f"pilot selection: {run} -> {names}")


def copy_pilot_launchers() -> None:
    """Double-click launchers (verify / dry-run) and the guide, next to the executable."""
    for p in (ROOT / "packaging" / "pilot").iterdir():
        shutil.copy2(p, DIST / p.name)


def smoke_test_pilot() -> None:
    """The frozen exe must run the paper pilot's offline self-check (no IBKR connection)."""
    exe = DIST / "TraderInvestFX.exe"
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(  # noqa: S603 - the executable this script just built
            [str(exe), "--paper-pilot", "--self-check", "--dir", tmp],
            capture_output=True,
            timeout=120,
            check=False,
        )
    out = proc.stdout.decode("utf-8", errors="replace")
    print(f"pilot smoke test: exit {proc.returncode}")
    if proc.returncode != 0 or "auto-verificação OK" not in out:
        tail = (out + proc.stderr.decode("utf-8", errors="replace"))[-4000:]
        raise SystemExit(f"the frozen paper pilot failed its self-check: {tail}")


def portable_zip() -> Path:
    from app.desktop import APP_VERSION

    out = ROOT / "dist" / f"TraderInvestFX-{APP_VERSION}-win64-portable.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in DIST.rglob("*"):
            z.write(p, Path("TraderInvestFX") / p.relative_to(DIST))
    return out


def compile_installer() -> Path | None:
    """Compile with an existing Inno Setup 6 installation, when available."""
    compiler = shutil.which("ISCC.exe") or shutil.which("ISCC")
    if compiler is None:
        for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            base = os.environ.get(variable)
            if base:
                relative = (
                    Path("Programs") / "Inno Setup 6" / "ISCC.exe"
                    if variable == "LOCALAPPDATA"
                    else Path("Inno Setup 6") / "ISCC.exe"
                )
                candidate = Path(base) / relative
                if candidate.is_file():
                    compiler = str(candidate)
                    break
    if compiler is None:
        print("installer: skipped (Inno Setup 6 / ISCC.exe not found; portable ZIP is ready)")
        return None
    from app.desktop import APP_VERSION

    subprocess.run(  # noqa: S603 - installed compiler, fixed repository script
        [compiler, str(ROOT / "packaging" / "installer.iss")], check=True, cwd=ROOT
    )
    output = ROOT / "dist" / f"TraderInvestFX-{APP_VERSION}-setup.exe"
    if not output.is_file():
        raise SystemExit(f"installer compiler did not produce {output}")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", action="store_true", help="also compile with Inno Setup 6")
    parser.add_argument(
        "--pilot-run",
        type=Path,
        default=ROOT / "data" / "fx" / "training" / "run-20261018",
        help="training run whose selected.json the paper pilot ships",
    )
    args = parser.parse_args()
    check_pilot_run(args.pilot_run)
    os.environ["TRADERINVEST_PILOT_RUN"] = str(args.pilot_run.resolve())
    print("icon:", render_icon())
    print("version:", version_resource())
    clean_dist()
    pyinstaller()
    copy_pilot_launchers()
    smoke_test_engine()
    smoke_test_ui()
    smoke_test_pilot()
    size = sum(p.stat().st_size for p in DIST.rglob("*") if p.is_file()) / 1e6
    print(f"dist: {DIST} ({size:.0f} MB)")
    print("zip:", portable_zip())
    if args.installer:
        output = compile_installer()
        if output is not None:
            print("installer:", output)


if __name__ == "__main__":
    main()
