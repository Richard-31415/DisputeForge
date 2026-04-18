"""Checkpoint + rollback. Scratchpad for per-run state."""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import time


class Scratchpad(dict):
    """Per-run kv store. Do not store secrets or user PII."""

    def record(self, key: str, value: object) -> object:
        self[key] = value
        return value


class Snapshot:
    def __init__(self, root: str = "./.agent_snaps"):
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def take(self, state: dict, files: list[pathlib.Path]) -> str:
        sid = hashlib.sha1(f"{int(time.time() * 1000)}".encode()).hexdigest()[:12]
        d = self.root / sid
        d.mkdir()
        (d / "state.json").write_text(json.dumps(state, default=str))
        for f in files:
            if pathlib.Path(f).exists():
                shutil.copy2(f, d / pathlib.Path(f).name)
        return sid

    def restore(self, sid: str, target: pathlib.Path) -> dict:
        d = self.root / sid
        state = json.loads((d / "state.json").read_text())
        for f in d.iterdir():
            if f.name != "state.json":
                shutil.copy2(f, target / f.name)
        return state

    def list_snapshots(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())
