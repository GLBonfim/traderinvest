"""Single offline M5 research queue. Never arms a broker or promotes a candidate."""

import argparse
import json
import msvcrt
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ALLOWED = {
    "app.fx.research.m5_gap_training",
    "app.fx.research.m5_balanced_training",
    "app.fx.research.m5_magnitude_training",
    "app.fx.research.m5_recency_training",
    "app.fx.research.m5_unique_training",
    "app.fx.research.m5_economic_training",
    "app.fx.research.m5_crosspair_training",
    "app.fx.research.m5_session_training",
    "app.fx.research.m5_boosted_training",
    "app.fx.research.m5_lesson_training",
}


def atomic(path: Path, value: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def validate_job(job: dict[str, Any], training_root: Path) -> Path:
    if not re.fullmatch(r"[a-z0-9-]{1,80}", str(job.get("id", ""))):
        raise ValueError("invalid job identity")
    if job.get("module") not in ALLOWED:
        raise ValueError("only offline research modules allowed")
    target = Path(job["out"]).resolve()
    if not target.is_relative_to(training_root.resolve()) or target == training_root.resolve():
        raise ValueError("run output must be a dedicated training subdirectory")
    return target


def run(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    lock = (root / "manager.lock").open("a+b")
    lock.seek(0)
    if not lock.read(1):
        lock.write(b"0")
        lock.flush()
    lock.seek(0)
    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    repo = Path(__file__).resolve().parents[3]
    training = repo / "data/fx/training"
    control_path = root / "control.json"
    if not control_path.exists():
        atomic(
            control_path,
            {
                "schema": "m5-offline-training-manager/1",
                "enabled": True,
                "desired_positive_specialists": 3,
                "execution_enabled": False,
                "acceptance": (
                    "Existing development gate, future holdout and joint protected pipeline; "
                    "positive mean alone is not approval."
                ),
                "policy": (
                    "Keep research enabled. Each frozen experiment runs once; no refit loop "
                    "on identical data or threshold relaxation. Await new evidence or a new "
                    "predeclared hypothesis when the queue is empty."
                ),
                "jobs": [
                    {
                        "id": "aula-session-v3-20261024",
                        "module": "app.fx.research.m5_session_training",
                        "out": str(training / "run-m5-aula-session-v3-20261024"),
                        "state": "pending",
                    }
                ],
            },
        )
    child = None
    stream = None
    current = None
    try:
        while True:
            control = json.loads(control_path.read_text(encoding="utf-8"))
            if control.get("execution_enabled") is not False:
                raise ValueError("manager is research-only: execution flag must remain false")
            if child is not None and child.poll() is not None:
                for job in control["jobs"]:
                    if job["id"] == current:
                        target = validate_job(job, training)
                        summary = target / "summary.json"
                        job.update(
                            state="completed"
                            if child.returncode == 0 and summary.exists()
                            else "failed_preserved",
                            exit_code=child.returncode,
                            finished_at=datetime.now(UTC).isoformat(),
                        )
                        if summary.exists():
                            job["summary"] = json.loads(summary.read_text(encoding="utf-8"))
                atomic(control_path, control)
                if stream is not None:
                    stream.close()
                child = stream = current = None
            if child is None and control.get("enabled"):
                for job in control["jobs"]:
                    if job["state"] == "running":
                        # A previous child may still exist; never duplicate it.
                        job["state"] = "requires_review_previous_worker"
                        atomic(control_path, control)
                    if job["state"] != "pending":
                        continue
                    target = validate_job(job, training)
                    if target.exists():
                        job.update(state="requires_review_existing_run")
                        atomic(control_path, control)
                        continue
                    stream = (root / (job["id"] + ".log")).open("a", encoding="utf-8")
                    child = subprocess.Popen(  # noqa: S603
                        [sys.executable, "-B", "-u", "-m", job["module"], "--out", str(target)],
                        cwd=repo,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                    )
                    current = job["id"]
                    job.update(
                        state="running", pid=child.pid, started_at=datetime.now(UTC).isoformat()
                    )
                    atomic(control_path, control)
                    break
            state = {
                "observed_at": datetime.now(UTC).isoformat(),
                "pid": os.getpid(),
                "m5_research_enabled": bool(control.get("enabled")),
                "training_running": child is not None,
                "worker_pid": child.pid if child else None,
                "active_job": current,
                "state": "training"
                if child
                else (
                    "awaiting_next_frozen_experiment_or_new_data"
                    if control.get("enabled")
                    else "paused_by_control"
                ),
                "desired_positive_specialists": control["desired_positive_specialists"],
                "execution_enabled": False,
                "orders_sent": 0,
                "jobs": [{k: v for k, v in j.items() if k != "summary"} for j in control["jobs"]],
            }
            atomic(root / "status.json", state)
            print(json.dumps(state), flush=True)
            time.sleep(30)
    finally:
        if stream is not None:
            stream.close()
        lock.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dir", type=Path, default=Path("data/fx/training/m5-research-manager"))
    a = p.parse_args()
    run(a.dir.resolve())
