"""Event-sourced tracing: every run is a replayable log, not a printout.

One append-only JSONL file per run holds every observation, proposal, validator
verdict and state transition, plus a manifest recording code version, config
hash, seeds and model identity. Three things this buys us:

  replay      - reconstruct any run without re-calling a model
  attribution - when a community collapses on turn 40, find the turn it started
  cost        - tokens and latency per agent per turn, in the same place

Design note: nothing here imports the simulator, so traces can be read by
analysis code that never loads COBRA or an LLM client.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterator


def _default(o: Any) -> Any:
    if is_dataclass(o) and not isinstance(o, type):
        return asdict(o)
    if isinstance(o, set):
        return sorted(o)
    if hasattr(o, "tolist"):
        return o.tolist()
    return str(o)


def stable_hash(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, default=_default).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


class Tracer:
    """Append-only run log. One instance per simulation run."""

    def __init__(self, run_dir: str | Path, run_id: str | None = None,
                 config: dict | None = None, echo: bool = False):
        self.run_id = run_id or f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.dir = Path(run_dir) / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "events.jsonl"
        self._fh = open(self.path, "a", buffering=1)
        self.echo = echo
        self.t0 = time.time()
        self.n_events = 0
        self.config = config or {}
        self.counters: dict[str, float] = {}
        self._write_manifest()

    # ------------------------------------------------------------------ manifest

    def _write_manifest(self) -> None:
        manifest = {
            "run_id": self.run_id,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "config": self.config,
            "config_hash": stable_hash(self.config),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "cwd": os.getcwd(),
            "git": _git_describe(),
        }
        (self.dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=_default))
        self.manifest = manifest

    # -------------------------------------------------------------------- write

    def emit(self, event: str, **fields: Any) -> None:
        rec = {
            "i": self.n_events,
            "t": round(time.time() - self.t0, 6),
            "event": event,
            **fields,
        }
        self._fh.write(json.dumps(rec, default=_default) + "\n")
        self.n_events += 1
        if self.echo:
            print(f"[{rec['t']:8.3f}] {event} " +
                  " ".join(f"{k}={v}" for k, v in fields.items() if k in
                           ("species", "turn", "growth", "ok")))

    def count(self, key: str, value: float = 1.0) -> None:
        self.counters[key] = self.counters.get(key, 0.0) + value

    def close(self, result: dict | None = None) -> None:
        self.emit("run_end", counters=self.counters, wall_s=round(time.time() - self.t0, 3))
        if result is not None:
            (self.dir / "result.json").write_text(json.dumps(result, indent=2, default=_default))
        (self.dir / "counters.json").write_text(json.dumps(self.counters, indent=2))
        self._fh.close()

    def __enter__(self) -> "Tracer":
        return self

    def __exit__(self, *exc: Any) -> None:
        if not self._fh.closed:
            self.close()

    # --------------------------------------------------------------- timing aid

    def timed(self, event: str, **fields: Any) -> "_Timer":
        return _Timer(self, event, fields)


class _Timer:
    def __init__(self, tracer: Tracer, event: str, fields: dict):
        self.tracer, self.event, self.fields = tracer, event, fields

    def __enter__(self) -> "_Timer":
        self.start = time.time()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        ms = round((time.time() - self.start) * 1000, 2)
        self.tracer.emit(self.event, latency_ms=ms,
                         error=str(exc) if exc else None, **self.fields)
        self.tracer.count(f"latency_ms.{self.event}", ms)


def read_events(run_dir: str | Path) -> Iterator[dict]:
    p = Path(run_dir)
    path = p / "events.jsonl" if p.is_dir() else p
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def load_run(run_dir: str | Path) -> dict:
    """Read a whole run into memory: manifest, events, and derived tables."""
    p = Path(run_dir)
    events = list(read_events(p))
    manifest_path = p / "manifest.json"
    return {
        "manifest": json.loads(manifest_path.read_text()) if manifest_path.exists() else {},
        "events": events,
        "snapshots": [e for e in events if e["event"] == "state"],
        "proposals": [e for e in events if e["event"] == "proposal"],
        "validations": [e for e in events if e["event"] == "validation"],
        "llm_calls": [e for e in events if e["event"] == "llm_call"],
    }


def latest_run(runs_dir: str | Path) -> Path | None:
    dirs = [d for d in Path(runs_dir).iterdir() if d.is_dir() and (d / "events.jsonl").exists()]
    return max(dirs, key=lambda d: d.stat().st_mtime) if dirs else None


def _git_describe() -> dict:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=3)
        if out.returncode == 0:
            return {"commit": out.stdout.strip()}
    except Exception:
        pass
    return {"commit": None}
