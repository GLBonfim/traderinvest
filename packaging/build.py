"""Builds the Windows desktop package: `uv run --group build python packaging/build.py`.

Steps: render the procedural icon (.ico, several sizes), write the Windows version resource,
run PyInstaller (one-folder build in dist/TraderInvestFX), smoke-test the engine mode of the
built executable, and zip a portable archive. No signing (see docs/commercial-readiness.md).
"""

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
        line = proc.stdout.readline().decode() if proc.stdout else ""
        print(f"engine smoke test: {line.strip()!r} after {time.monotonic() - started:.1f}s")
        try:
            proc.wait(timeout=60)  # orphan policy: exits by itself without a client
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            raise SystemExit("engine did not stop by itself (orphan policy)") from exc
        if not line.startswith("ENGINE_READY"):
            raise SystemExit("the frozen engine did not start")


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


def portable_zip() -> Path:
    from app.desktop import APP_VERSION

    out = ROOT / "dist" / f"TraderInvestFX-{APP_VERSION}-win64-portable.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in DIST.rglob("*"):
            z.write(p, Path("TraderInvestFX") / p.relative_to(DIST))
    return out


def main() -> None:
    print("icon:", render_icon())
    print("version:", version_resource())
    if DIST.exists():
        shutil.rmtree(DIST)
    pyinstaller()
    smoke_test_engine()
    smoke_test_ui()
    size = sum(p.stat().st_size for p in DIST.rglob("*") if p.is_file()) / 1e6
    print(f"dist: {DIST} ({size:.0f} MB)")
    print("zip:", portable_zip())


if __name__ == "__main__":
    main()
