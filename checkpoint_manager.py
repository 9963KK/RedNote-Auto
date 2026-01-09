import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class CheckpointMeta:
    task_id: str
    updated_at: str
    created_at: str


class CheckpointManager:
    """
    Task checkpoint manager.

    Stores per-task checkpoints as JSON files under `data/checkpoints/{task_id}.json`.
    """

    def __init__(self, data_dir: Path, *, retention_hours: int = 24):
        self.data_dir = Path(data_dir)
        self.checkpoints_dir = self.data_dir / "checkpoints"
        self.retention_hours = max(1, int(retention_hours))

    def _path_for(self, task_id: str) -> Path:
        safe = (task_id or "").strip()
        if not safe:
            raise ValueError("task_id is required")
        return (self.checkpoints_dir / f"{safe}.json").resolve()

    def save_checkpoint(self, task_id: str, step_name: str, data: Dict[str, Any]) -> Path:
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        path = self._path_for(task_id)
        payload = self.load_checkpoint(task_id) or {}
        created_at = payload.get("created_at") or _now_iso()
        payload.update(
            {
                "task_id": task_id,
                "created_at": created_at,
                "updated_at": _now_iso(),
            }
        )
        step_outputs = payload.get("step_outputs") if isinstance(payload.get("step_outputs"), dict) else {}
        step_outputs[step_name] = {"timestamp": _now_iso(), "data": data}
        payload["step_outputs"] = step_outputs

        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return path

    def load_checkpoint(self, task_id: str) -> Optional[Dict[str, Any]]:
        path = self._path_for(task_id)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def clear_checkpoint(self, task_id: str) -> None:
        path = self._path_for(task_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return

    def cleanup(self) -> int:
        """
        Remove checkpoint files older than retention_hours.
        Returns the number of deleted files.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.retention_hours)
        deleted = 0
        if not self.checkpoints_dir.exists():
            return 0
        for p in self.checkpoints_dir.glob("*.json"):
            try:
                ts = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
                if ts < cutoff:
                    p.unlink()
                    deleted += 1
            except Exception:
                continue
        return deleted

    @staticmethod
    def get_step_data(checkpoint: Optional[Dict[str, Any]], step_name: str) -> Optional[Dict[str, Any]]:
        if not checkpoint:
            return None
        step_outputs = checkpoint.get("step_outputs")
        if not isinstance(step_outputs, dict):
            return None
        entry = step_outputs.get(step_name)
        if not isinstance(entry, dict):
            return None
        data = entry.get("data")
        return data if isinstance(data, dict) else None

