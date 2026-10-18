"""Single-instance, resumable M5 collection -> training -> report job. No order API."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def write_state(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def export(out: Path, outputs: Path, state: dict[str, Any]) -> None:
    outputs.mkdir(parents=True, exist_ok=True)
    write_state(outputs / "estado-treinamento-m5.json", state)
    guide = Path("docs/m5-training.md")
    if guide.exists():
        shutil.copyfile(guide, outputs / "guia-treinamento-m5.md")
    if state["phase"] != "completed":
        title = "falha; requer revisão" if state["phase"] == "failed" else "em andamento"
        (outputs / "relatorio-treinamento-m5.md").write_text(
            f"# Treinamento M5 {title}\n\n"
            f"Estado: {state['phase']}. Atualização UTC: {state['observed_at']}.\n\n"
            f"Arquivos M5 obtidos: {state['downloaded_chunks']}/{state['expected_chunks']}.\n\n"
            "Coleta BID/ASK real da IBKR, treinamento de seis candidatos (três especialidades), "
            "validação walk-forward e teste temporal 2026. Nenhum resultado financeiro "
            "nem aprovação foi produzido enquanto estas etapas não terminarem. "
            "O laboratório H1 permanece separado e nenhuma ordem M5 é autorizada pelo job.\n",
            encoding="utf-8",
        )
        return
    shutil.copyfile(out / "report.md", outputs / "relatorio-treinamento-m5.md")
    archive = outputs / "modelos-e-validacao-m5.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.rglob("*")):
            if f.is_file() and f.suffix in (".json", ".md", ".csv"):
                z.write(f, f.relative_to(out))
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    (outputs / "SHA256-m5.txt").write_text(f"{sha}  {archive.name}\n", encoding="utf-8")


def run(data: Path, out: Path, outputs: Path) -> int:
    import msvcrt

    out.mkdir(parents=True, exist_ok=True)
    lock = (out / "job.lock").open("a+b")
    lock.seek(0)
    if not lock.read(1):
        lock.write(b"0")
        lock.flush()
    lock.seek(0)
    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    state: dict[str, Any] = {"pid": os.getpid(), "orders_allowed": False}

    def status(phase: str, **extra: Any) -> None:
        from app.fx.research.m5_history import SYMBOLS, ends

        expected = {
            data / sym / f"{side}_M5_{e:%Y%m%d}.parquet"
            for e in ends()
            for sym in SYMBOLS
            for side in ("BID", "ASK")
        }
        obtained = set(data.glob("*/*_M5_*.parquet"))
        state.update(
            phase=phase,
            observed_at=datetime.now(UTC).isoformat(),
            downloaded_chunks=len(obtained),
            expected_chunks=len(expected | obtained),
            **extra,
        )
        write_state(out / "job.json", state)
        export(out, outputs, state)

    def execute(phase: str, args: list[str]) -> None:
        if (out / "STOP").exists():
            raise RuntimeError("STOP requested for research job")
        with (out / f"{phase}.log").open("a", encoding="utf-8") as log:
            child = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)  # noqa: S603
            try:
                while child.poll() is None:
                    if (out / "STOP").exists():
                        raise RuntimeError("STOP requested for research job")
                    status(phase, child_pid=child.pid)
                    time.sleep(15)
                if child.returncode:
                    raise RuntimeError(f"{phase} exited {child.returncode}; inspect {phase}.log")
            finally:
                if child.poll() is None:
                    # This is only our read-only research worker, never the paper lab.
                    killer = Path(os.environ["SYSTEMROOT"]) / "System32" / "taskkill.exe"
                    subprocess.run(  # noqa: S603
                        [str(killer), "/PID", str(child.pid), "/T", "/F"],
                        capture_output=True,
                        check=False,
                    )
                    child.wait(timeout=15)

    try:
        if (out / "holdout.json").exists():
            raise RuntimeError("Existing holdout: review results instead of rerunning this job")
        manifest = data / "manifest.json"
        if not manifest.exists() or not json.loads(manifest.read_text(encoding="utf-8")).get(
            "complete"
        ):
            execute(
                "downloading",
                [
                    sys.executable,
                    "-u",
                    "-m",
                    "app.fx.research.m5_history",
                    "--data",
                    str(data),
                    "--parallel",
                    "6",
                ],
            )
        execute(
            "training",
            [
                sys.executable,
                "-u",
                "-m",
                "app.fx.research.m5_validation",
                "--data",
                str(data),
                "--out",
                str(out),
            ],
        )
        selected = json.loads((out / "selected.json").read_text(encoding="utf-8"))
        status(
            "completed",
            active_model_ids=selected["active_model_ids"],
            statistically_approved=False,
            no_trade_roles=selected["no_trade_roles"],
        )
        return 0
    except Exception as error:
        status("failed", error=str(error))
        raise
    finally:
        lock.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data/fx/ibkr-m5"))
    parser.add_argument("--out", type=Path, default=Path("data/fx/training/run-m5-20261018"))
    parser.add_argument("--outputs", type=Path, required=True)
    args = parser.parse_args()
    return run(args.data.resolve(), args.out.resolve(), args.outputs.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
