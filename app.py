import asyncio
import json
import logging
import os
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from queue import Queue, Empty


import main as rednote_main
from checkpoint_manager import CheckpointManager
from model_health import ModelHealthChecker

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("rednote.monitor")

PUBLISH_STEPS = ["fetch_papers", "openai_analyze", "render_images", "build_payload", "publish_mcp"]

AUTO_RETRY_ENABLED = os.getenv("AUTO_RETRY_ENABLED", "true").lower() in ("1", "true", "yes")
AUTO_RETRY_MAX_ATTEMPTS = int(os.getenv("AUTO_RETRY_MAX_ATTEMPTS", "3"))
AUTO_RETRY_DELAY_SECONDS = int(os.getenv("AUTO_RETRY_DELAY_SECONDS", "30"))
CHECKPOINT_RETENTION_HOURS = int(os.getenv("CHECKPOINT_RETENTION_HOURS", "24"))
MODEL_HEALTH_CHECK_INTERVAL = int(os.getenv("MODEL_HEALTH_CHECK_INTERVAL", "60"))
MODEL_HEALTH_CHECK_TIMEOUT = int(os.getenv("MODEL_HEALTH_CHECK_TIMEOUT", "5"))


def _init_steps(names: List[str]) -> List[Dict[str, Any]]:
    return [{"name": n, "status": "pending", "start_time": None, "end_time": None, "meta": {}} for n in names]


def _mutate_task(task_id: str, mutator) -> None:
    with TASKS_LOCK:
        tasks = _load_tasks()
        for t in tasks:
            if t.get("task_id") == task_id:
                mutator(t)
                break
        _save_tasks(tasks)


def _start_step(task_id: str, step_name: str, *, meta: Optional[Dict[str, Any]] = None) -> None:
    def mut(t: Dict[str, Any]) -> None:
        steps = t.get("steps")
        if not isinstance(steps, list):
            steps = _init_steps(PUBLISH_STEPS)
            t["steps"] = steps
        for s in steps:
            if s.get("name") == step_name:
                if s.get("status") in ("success", "failed"):
                    return
                s["status"] = "running"
                s["start_time"] = s.get("start_time") or _now_iso()
                if meta:
                    m = s.get("meta") if isinstance(s.get("meta"), dict) else {}
                    m.update(meta)
                    s["meta"] = m
                t["current_step"] = step_name
                break

    _mutate_task(task_id, mut)


def _finish_step(
    task_id: str,
    step_name: str,
    *,
    status: str,
    meta: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None:
    def mut(t: Dict[str, Any]) -> None:
        steps = t.get("steps")
        if not isinstance(steps, list):
            return
        for s in steps:
            if s.get("name") == step_name:
                s["status"] = status
                s["end_time"] = _now_iso()
                if meta:
                    m = s.get("meta") if isinstance(s.get("meta"), dict) else {}
                    m.update(meta)
                    s["meta"] = m
                if error:
                    s["error"] = error
                break
        if t.get("current_step") == step_name:
            t["current_step"] = None
        if status == "failed":
            t["failed_step"] = step_name
            if error and not t.get("error_message"):
                t["error_message"] = error
            if t.get("status") == "running":
                t["status"] = "failed"
                t["end_time"] = t.get("end_time") or _now_iso()

    _mutate_task(task_id, mut)


def _set_progress(task_id: str, updates: Dict[str, Any]) -> None:
    def mut(t: Dict[str, Any]) -> None:
        prog = t.get("progress") if isinstance(t.get("progress"), dict) else {}
        prog.update(updates)
        t["progress"] = prog

    _mutate_task(task_id, mut)


def _append_task_warning(task_id: str, warning: Dict[str, Any], *, max_items: int = 30) -> None:
    def mut(t: Dict[str, Any]) -> None:
        items = t.get("warnings") if isinstance(t.get("warnings"), list) else []
        items.append(warning)
        t["warnings"] = items[-max_items:]

    _mutate_task(task_id, mut)

def _fail_running_steps(task_id: str, error: str) -> None:
    def mut(t: Dict[str, Any]) -> None:
        steps = t.get("steps")
        if not isinstance(steps, list):
            return
        for s in steps:
            if s.get("status") == "running":
                s["status"] = "failed"
                s["end_time"] = _now_iso()
                s["error"] = error
        t["current_step"] = None

    _mutate_task(task_id, mut)


def _cancel_running_steps(task_id: str, reason: str) -> None:
    def mut(t: Dict[str, Any]) -> None:
        steps = t.get("steps")
        if not isinstance(steps, list):
            return
        for s in steps:
            if s.get("status") == "running":
                s["status"] = "canceled"
                s["end_time"] = _now_iso()
                s["error"] = reason
        t["current_step"] = None

    _mutate_task(task_id, mut)


def _format_exception_group(exc: BaseException) -> str:
    if not isinstance(exc, BaseExceptionGroup):
        return f"{type(exc).__name__}: {exc}"

    lines: List[str] = []

    def walk(e: BaseException, indent: int = 0) -> None:
        pad = "  " * indent
        if isinstance(e, BaseExceptionGroup):
            lines.append(f"{pad}{type(e).__name__}: {e}")
            for sub in e.exceptions:
                walk(sub, indent + 1)
        else:
            lines.append(f"{pad}{type(e).__name__}: {e}")

    walk(exc)
    return "\n".join(lines)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
SCHEDULER_CONFIG_PATH = DATA_DIR / "scheduler_config.json"
TASKS_PATH = DATA_DIR / "tasks.json"
CHECKPOINT_DIR = DATA_DIR / "checkpoints"

checkpoint_mgr = CheckpointManager(DATA_DIR, retention_hours=CHECKPOINT_RETENTION_HOURS)
health_checker = ModelHealthChecker(timeout_s=MODEL_HEALTH_CHECK_TIMEOUT)

TASKS_LOCK = threading.RLock()

CANCEL_EVENTS: Dict[str, threading.Event] = {}
CANCEL_EVENTS_LOCK = threading.Lock()

# Log streaming support
log_queues: List[Queue] = []
log_queues_lock = threading.Lock()

class LogQueueHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            with log_queues_lock:
                for q in log_queues:
                    q.put_nowait(msg)
        except Exception:
            self.handleError(record)



def _get_cancel_event(task_id: str) -> threading.Event:
    with CANCEL_EVENTS_LOCK:
        ev = CANCEL_EVENTS.get(task_id)
        if ev is None:
            ev = threading.Event()
            CANCEL_EVENTS[task_id] = ev
        return ev


def _pop_cancel_event(task_id: str) -> None:
    with CANCEL_EVENTS_LOCK:
        CANCEL_EVENTS.pop(task_id, None)


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


class ScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    publish_enabled: bool = True
    publish_hour: int = Field(9, ge=0, le=23)
    publish_minute: int = Field(30, ge=0, le=59)


class TriggerRequest(BaseModel):
    task_type: str = Field(..., pattern=r"^(publish)$")
    max_papers: Optional[int] = Field(default=None, ge=1, le=20)


def _ensure_data_files() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

    if not SCHEDULER_CONFIG_PATH.exists():
        _atomic_write_json(
            SCHEDULER_CONFIG_PATH,
            ScheduleConfig().model_dump(),
        )
    if not TASKS_PATH.exists():
        _atomic_write_json(TASKS_PATH, {"tasks": []})


def _load_schedule_config() -> ScheduleConfig:
    data = _read_json(SCHEDULER_CONFIG_PATH, ScheduleConfig().model_dump())
    return ScheduleConfig(**data)


def _save_schedule_config(cfg: ScheduleConfig) -> None:
    _atomic_write_json(SCHEDULER_CONFIG_PATH, cfg.model_dump())


def _load_tasks() -> List[Dict[str, Any]]:
    with TASKS_LOCK:
        payload = _read_json(TASKS_PATH, {"tasks": []})
        tasks = payload.get("tasks") if isinstance(payload, dict) else []
        items = [t for t in tasks if isinstance(t, dict)]

        # Reconcile impossible states:
        # - a task cannot be "running" if any step is "failed" / "canceled"
        changed = False
        for t in items:
            if t.get("status") != "running":
                continue
            steps = t.get("steps")
            if not isinstance(steps, list):
                continue
            failed = next((s for s in steps if isinstance(s, dict) and s.get("status") == "failed"), None)
            canceled = next((s for s in steps if isinstance(s, dict) and s.get("status") == "canceled"), None)
            if failed:
                t["status"] = "failed"
                t["failed_step"] = failed.get("name") or t.get("failed_step")
                if not t.get("error_message") and failed.get("error"):
                    t["error_message"] = failed.get("error")
                t["end_time"] = t.get("end_time") or _now_iso()
                changed = True
                continue
            if canceled:
                t["status"] = "canceled"
                t["end_time"] = t.get("end_time") or _now_iso()
                if not t.get("error_message") and canceled.get("error"):
                    t["error_message"] = canceled.get("error")
                changed = True

        if changed:
            _save_tasks(items)
        return items


def _save_tasks(tasks: List[Dict[str, Any]]) -> None:
    with TASKS_LOCK:
        _atomic_write_json(TASKS_PATH, {"tasks": tasks[:200]})


def _append_task(task: Dict[str, Any]) -> None:
    with TASKS_LOCK:
        tasks = _load_tasks()
        tasks.insert(0, task)
        _save_tasks(tasks)


def _update_task(task_id: str, updates: Dict[str, Any]) -> None:
    with TASKS_LOCK:
        tasks = _load_tasks()
        for t in tasks:
            if t.get("task_id") == task_id:
                t.update(updates)
                break
        _save_tasks(tasks)


def _new_task_id(task_type: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"task_{task_type}_{ts}"

def _checkpoint_path(task_id: str) -> Path:
    return CHECKPOINT_DIR / f"{task_id}.json"


def _step_index(step_name: str) -> int:
    try:
        return PUBLISH_STEPS.index(step_name)
    except ValueError:
        return 0


def _infer_failed_step(task: Dict[str, Any]) -> str:
    failed_step = (task.get("failed_step") or "").strip()
    if failed_step in PUBLISH_STEPS:
        return failed_step
    steps = task.get("steps") if isinstance(task.get("steps"), list) else []
    for s in steps:
        if isinstance(s, dict) and s.get("status") == "failed" and s.get("name") in PUBLISH_STEPS:
            return s["name"]
    for s in steps:
        if isinstance(s, dict) and s.get("status") == "running" and s.get("name") in PUBLISH_STEPS:
            return s["name"]
    last_ok = (task.get("last_successful_step") or "").strip()
    if last_ok in PUBLISH_STEPS:
        idx = _step_index(last_ok) + 1
        if idx < len(PUBLISH_STEPS):
            return PUBLISH_STEPS[idx]
    return "fetch_papers"


def _mark_task_resumable(task_id: str, *, last_successful_step: Optional[str] = None) -> None:
    updates: Dict[str, Any] = {
        "resumable": True,
        "checkpoint_path": str(_checkpoint_path(task_id)),
        "checkpoint_meta": {
            "path": str(_checkpoint_path(task_id)),
            "last_saved_step": last_successful_step,
            "updated_at": _now_iso(),
        },
    }
    if last_successful_step:
        updates["last_successful_step"] = last_successful_step
    _update_task(task_id, updates)


def _run_publish_sync(
    task_id: str,
    max_papers: int,
    *,
    resume_from_task_id: Optional[str] = None,
    resume_from_step: Optional[str] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """
    Run main.py pipeline (hybrid) and publish via MCP, while recording status for monitoring.

    NOTE: This function is intentionally synchronous so it can run in a worker thread without
    a running event loop. Some legacy helpers (e.g. crawler.run_crawler_tool) use asyncio.run(),
    which would fail if we wrapped the whole workflow in an event loop.
    """
    old = rednote_main.ENABLE_MCP_PUBLISH
    rednote_main.ENABLE_MCP_PUBLISH = False
    try:
        def cancel_cb() -> bool:
            return bool(cancel_event and cancel_event.is_set())

        def check_cancel(where: str) -> None:
            if cancel_cb():
                raise rednote_main.WorkflowCancelled(f"canceled: {where}")

        check_cancel("start")
        base_task_id = resume_from_task_id or task_id
        base_checkpoint = checkpoint_mgr.load_checkpoint(base_task_id)
        start_step = (resume_from_step or "fetch_papers").strip() or "fetch_papers"
        if start_step not in PUBLISH_STEPS:
            start_step = "fetch_papers"
        start_idx = _step_index(start_step)

        def resume_step(step_name: str, data_key: str) -> Any:
            data = CheckpointManager.get_step_data(base_checkpoint, step_name)
            if not isinstance(data, dict) or data_key not in data:
                raise RuntimeError(f"Checkpoint missing {step_name}.{data_key} (source={base_task_id})")
            checkpoint_mgr.save_checkpoint(task_id, step_name, data)
            _start_step(task_id, step_name, meta={"resumed": True, "source_task_id": base_task_id})
            _finish_step(task_id, step_name, status="success", meta={"resumed": True, "source_task_id": base_task_id})
            _mark_task_resumable(task_id, last_successful_step=step_name)
            return data[data_key]

        # Step 1: fetch_papers
        check_cancel("before fetch_papers")
        if start_idx > 0 and base_task_id and base_task_id != task_id:
            papers = resume_step("fetch_papers", "papers")
        elif start_idx > 0 and base_checkpoint:
            papers = resume_step("fetch_papers", "papers")
        else:
            _start_step(task_id, "fetch_papers", meta={"max_papers": max_papers})
            papers = rednote_main.fetch_papers_step({"max_papers": max_papers})
            if not papers:
                _finish_step(task_id, "fetch_papers", status="failed", meta={"count": 0}, error="no papers fetched")
                raise RuntimeError("No papers fetched; aborting publish.")
            _finish_step(task_id, "fetch_papers", status="success", meta={"count": len(papers)})
            checkpoint_mgr.save_checkpoint(task_id, "fetch_papers", {"papers": papers, "max_papers": max_papers})
            _mark_task_resumable(task_id, last_successful_step="fetch_papers")

        _set_progress(task_id, {"papers_total": len(papers), "openai_done": 0, "images_done": 0, "payload_done": 0})
        check_cancel("after fetch_papers")

        def progress_cb(evt: Dict[str, Any]) -> None:
            phase = (evt.get("phase") or "").strip()
            event = (evt.get("event") or "").strip()
            if phase not in ("openai_analyze", "render_images", "build_payload"):
                return

            if event == "phase_progress":
                done = int(evt.get("done") or 0)
                if phase == "openai_analyze":
                    _set_progress(task_id, {"openai_done": done})
                elif phase == "render_images":
                    _set_progress(task_id, {"images_done": done})
                elif phase == "build_payload":
                    _set_progress(task_id, {"payload_done": done})
                ok = evt.get("ok")
                if ok is False:
                    _append_task_warning(
                        task_id,
                        {
                            "ts": _now_iso(),
                            "phase": phase,
                            "title": evt.get("title"),
                            "error": evt.get("error"),
                        },
                    )
                return
            return

        # Step 2: openai_analyze
        check_cancel("before openai_analyze")
        if start_idx > _step_index("openai_analyze") and base_checkpoint:
            analysis_results = resume_step("openai_analyze", "analysis_results")
            _set_progress(task_id, {"openai_done": len(papers)})
        else:
            _start_step(task_id, "openai_analyze", meta={"total": len(papers)})
            analysis_results = rednote_main.openai_analyze_step(papers, progress_cb=progress_cb, cancel_cb=cancel_cb)
            _finish_step(task_id, "openai_analyze", status="success", meta={"total": len(papers), "done": len(analysis_results)})
            checkpoint_mgr.save_checkpoint(task_id, "openai_analyze", {"analysis_results": analysis_results})
            _mark_task_resumable(task_id, last_successful_step="openai_analyze")
            _set_progress(task_id, {"openai_done": len(analysis_results)})
        check_cancel("after openai_analyze")

        # Step 3: render_images
        output_base = str(BASE_DIR / "output_hybrid")
        check_cancel("before render_images")
        if start_idx > _step_index("render_images") and base_checkpoint:
            combined_results = resume_step("render_images", "combined_results")
            _set_progress(task_id, {"images_done": len(papers)})
        else:
            _start_step(task_id, "render_images", meta={"total": len(analysis_results)})
            combined_results = rednote_main.render_images_step(
                analysis_results,
                output_base=output_base,
                progress_cb=progress_cb,
                cancel_cb=cancel_cb,
            )
            _finish_step(task_id, "render_images", status="success", meta={"total": len(analysis_results), "done": len(combined_results)})
            checkpoint_mgr.save_checkpoint(task_id, "render_images", {"combined_results": combined_results})
            _mark_task_resumable(task_id, last_successful_step="render_images")
            _set_progress(task_id, {"images_done": len(combined_results)})
        check_cancel("after render_images")

        # Step 4: build_payload
        payload_path = Path(output_base) / "post_payload.json"
        try:
            payload_path.unlink()
        except FileNotFoundError:
            pass

        check_cancel("before build_payload")
        if start_idx > _step_index("build_payload") and base_checkpoint:
            payload = resume_step("build_payload", "payload")
            _set_progress(task_id, {"payload_done": len(papers)})
        else:
            _start_step(task_id, "build_payload", meta={"total": len(combined_results)})
            payload = rednote_main.build_payload_step(
                combined_results,
                output_base=output_base,
                progress_cb=progress_cb,
                cancel_cb=cancel_cb,
            )
            _finish_step(
                task_id,
                "build_payload",
                status="success",
                meta={
                    "items": len(payload.get("items") or []),
                    "images": len(payload.get("images") or []),
                    "tags": len(payload.get("tags") or []),
                },
            )
            checkpoint_mgr.save_checkpoint(task_id, "build_payload", {"payload": payload})
            _mark_task_resumable(task_id, last_successful_step="build_payload")
            _set_progress(task_id, {"payload_done": len(papers)})

        if not isinstance(payload, dict) or not payload.get("title"):
            raise RuntimeError(f"post_payload.json missing/invalid at {payload_path}")
        check_cancel("before publish_mcp")

        _start_step(task_id, "publish_mcp")
        mcp_url = os.getenv("XHS_MCP_URL", "http://127.0.0.1:18060/mcp")
        mcp_result = asyncio.run(rednote_main.publish_post_via_mcp(payload, mcp_url))
        if mcp_result.get("isError"):
            _finish_step(task_id, "publish_mcp", status="failed", error=str(mcp_result))
            raise RuntimeError(f"MCP publish returned isError=true: {mcp_result}")
        _finish_step(task_id, "publish_mcp", status="success")
        checkpoint_mgr.save_checkpoint(task_id, "publish_mcp", {"mcp_result": mcp_result})
        _mark_task_resumable(task_id, last_successful_step="publish_mcp")
        return {
            "papers_count": len(payload.get("items") or []),
            "published_count": 0 if mcp_result.get("isError") else 1,
            "mcp_result": mcp_result,
        }
    finally:
        rednote_main.ENABLE_MCP_PUBLISH = old


publish_lock = asyncio.Lock()


async def run_task(task_type: str, *, source: str, max_papers: Optional[int] = None) -> str:
    task_id = _new_task_id(task_type)
    return await run_task_with_id(task_id, task_type, source=source, max_papers=max_papers)


async def run_task_with_id(
    task_id: str,
    task_type: str,
    *,
    source: str,
    max_papers: Optional[int] = None,
    resume_from_task_id: Optional[str] = None,
    resume_from_step: Optional[str] = None,
    retry_of: Optional[str] = None,
    retry_count: int = 0,
) -> str:
    start = _now_iso()
    _append_task(
        {
            "task_id": task_id,
            "task_type": task_type,
            "status": "running",
            "source": source,
            "retry_of": retry_of,
            "retry_count": int(retry_count),
            "max_retries": int(AUTO_RETRY_MAX_ATTEMPTS),
            "auto_retry_enabled": bool(AUTO_RETRY_ENABLED),
            "resume_from_task_id": resume_from_task_id,
            "resume_from_step": resume_from_step,
            "resumable": False,
            "last_successful_step": None,
            "failed_step": None,
            "cancel_requested": False,
            "cancel_requested_at": None,
            "checkpoint_path": None,
            "checkpoint_meta": None,
            "start_time": start,
            "end_time": None,
            "papers_count": 0,
            "published_count": 0,
            "error_message": None,
            "steps": _init_steps(PUBLISH_STEPS) if task_type == "publish" else None,
            "progress": {"papers_total": 0, "openai_done": 0, "images_done": 0, "payload_done": 0} if task_type == "publish" else None,
            "current_step": None,
        }
    )

    lock = publish_lock

    if lock.locked():
        _update_task(task_id, {"status": "skipped", "end_time": _now_iso(), "error_message": "task already running"})
        return task_id

    cancel_event = _get_cancel_event(task_id) if task_type == "publish" else None

    async with lock:
        try:
            mp = int(max_papers or int(os.getenv("PUBLISH_MAX_PAPERS", "5")))
            mp = max(1, min(mp, 20))
            # run pipeline in a worker thread to avoid blocking the event loop
            result = await asyncio.to_thread(
                _run_publish_sync,
                task_id,
                mp,
                resume_from_task_id=resume_from_task_id,
                resume_from_step=resume_from_step,
                cancel_event=cancel_event,
            )

            _update_task(
                task_id,
                {
                    "status": "success",
                    "end_time": _now_iso(),
                    "papers_count": int(result.get("papers_count") or 0),
                    "published_count": int(result.get("published_count") or 0),
                    "failed_step": None,
                },
            )
        except rednote_main.WorkflowCancelled as e:
            reason = str(e) or "canceled"
            _cancel_running_steps(task_id, reason)
            _update_task(
                task_id,
                {
                    "status": "canceled",
                    "end_time": _now_iso(),
                    "error_message": reason,
                    "failed_step": None,
                    "current_step": None,
                },
            )
        except Exception as e:
            # capture the step that was running when the exception happened
            current_task = next((t for t in _load_tasks() if t.get("task_id") == task_id), {})
            failed_step = (current_task.get("current_step") or _infer_failed_step(current_task) or "fetch_papers").strip()
            if failed_step not in PUBLISH_STEPS:
                failed_step = "fetch_papers"

            err_text = _format_exception_group(e) if isinstance(e, BaseExceptionGroup) else f"{type(e).__name__}: {e}"
            _fail_running_steps(task_id, err_text)
            _update_task(
                task_id,
                {
                    "status": "failed",
                    "end_time": _now_iso(),
                    "error_message": err_text,
                    "failed_step": failed_step,
                    "traceback": traceback.format_exc(limit=50),
                },
            )

            # auto retry (new task id) if enabled and we have at least fetch checkpoint
            if task_type == "publish" and AUTO_RETRY_ENABLED and int(retry_count) < int(AUTO_RETRY_MAX_ATTEMPTS):
                delay = max(1, int(AUTO_RETRY_DELAY_SECONDS)) * (2 ** max(0, int(retry_count)))
                delay = min(delay, 600)
                origin = retry_of or task_id
                next_retry_count = int(retry_count) + 1

                async def _auto_retry_later() -> None:
                    await asyncio.sleep(delay)
                    new_id = _new_task_id("publish")
                    asyncio.create_task(
                        run_task_with_id(
                            new_id,
                            "publish",
                            source="auto_retry",
                            max_papers=mp,
                            resume_from_task_id=task_id,
                            resume_from_step=failed_step,
                            retry_of=origin,
                            retry_count=next_retry_count,
                        )
                    )

                asyncio.create_task(_auto_retry_later())
        finally:
            if cancel_event is not None:
                _pop_cancel_event(task_id)
        return task_id


def _safe_resolve_under(base: Path, rel_path: str) -> Path:
    rel_path = (rel_path or "").lstrip("/").strip()
    target = (base / rel_path).resolve()
    base_resolved = base.resolve()
    try:
        # Python 3.9+ doesn't have is_relative_to
        if os.path.commonpath([str(base_resolved), str(target)]) != str(base_resolved):
            raise ValueError("path traversal")
    except Exception:
        if str(base_resolved) not in str(target):
            raise ValueError("path traversal")
    return target


scheduler = AsyncIOScheduler(
    timezone=ZoneInfo(os.getenv("MONITOR_TZ", "Asia/Shanghai")),
    executors={"default": AsyncIOExecutor()},
)


def _reschedule_jobs(cfg: ScheduleConfig) -> None:
    # Only manage the publish job here; other background jobs (health/cleanup) live independently.
    try:
        scheduler.remove_job("publish")
    except Exception:
        pass

    if cfg.publish_enabled:
        scheduler.add_job(
            run_task,
            CronTrigger(hour=cfg.publish_hour, minute=cfg.publish_minute),
            args=["publish"],
            kwargs={"source": "scheduler"},
            id="publish",
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=300,
        )


app = FastAPI(title="RedNote Auto Monitor", version="1.0")


@app.on_event("startup")
async def _on_startup() -> None:
    # Setup log streaming
    root_log = logging.getLogger()
    handler = LogQueueHandler()
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    root_log.addHandler(handler)

    _ensure_data_files()

    # best-effort: clean old checkpoints
    try:
        deleted = checkpoint_mgr.cleanup()
        if deleted:
            logger.info("Checkpoint cleanup: deleted=%d", deleted)
    except Exception:
        pass

    # If the service restarted, any previously "running" tasks are no longer executing.
    # Mark them as failed to avoid the UI showing stuck tasks forever.
    try:
        now = _now_iso()
        tasks = _load_tasks()
        changed = False
        for t in tasks:
            if t.get("status") == "running":
                t["status"] = "failed"
                t["end_time"] = t.get("end_time") or now
                t["error_message"] = t.get("error_message") or "monitor service restarted; task was interrupted"
                t["failed_step"] = t.get("failed_step") or (t.get("current_step") or None)
                t["current_step"] = None
                changed = True
        if changed:
            _save_tasks(tasks)
    except Exception:
        pass

    cfg = _load_schedule_config()
    _reschedule_jobs(cfg)

    # periodic model health checks
    try:
        scheduler.add_job(
            health_checker.check_all,
            "interval",
            seconds=max(10, MODEL_HEALTH_CHECK_INTERVAL),
            id="models_health",
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=30,
        )
        # checkpoint retention cleanup
        scheduler.add_job(
            lambda: checkpoint_mgr.cleanup(),
            "interval",
            hours=max(1, CHECKPOINT_RETENTION_HOURS // 2),
            id="checkpoint_cleanup",
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=300,
        )
    except Exception:
        pass

    scheduler.start()
    logger.info("Scheduler started with config: %s", cfg.model_dump())
    # initial model health check (async)
    asyncio.create_task(health_checker.check_all())


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    scheduler.shutdown(wait=False)


@app.get("/")
async def index():
    html = STATIC_DIR / "monitor.html"
    if not html.exists():
        raise HTTPException(status_code=404, detail="monitor.html not found")
    return FileResponse(
        html,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.get("/api/schedule")
async def get_schedule():
    return _load_schedule_config().model_dump()


@app.post("/api/schedule")
async def update_schedule(cfg: ScheduleConfig):
    _save_schedule_config(cfg)
    _reschedule_jobs(cfg)
    return {"ok": True, "schedule": cfg.model_dump()}


@app.post("/api/tasks/trigger")
async def trigger_task(req: TriggerRequest):
    cfg = _load_schedule_config()
    if req.task_type == "publish" and not cfg.publish_enabled:
        raise HTTPException(status_code=400, detail="publish task disabled by schedule config")

    task_id = _new_task_id(req.task_type)
    asyncio.create_task(run_task_with_id(task_id, req.task_type, source="manual", max_papers=req.max_papers))
    return {"ok": True, "task_id": task_id}


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: str):
    tasks = _load_tasks()
    t = next((x for x in tasks if x.get("task_id") == task_id), None)
    if not t:
        raise HTTPException(status_code=404, detail="task not found")
    if t.get("task_type") != "publish":
        raise HTTPException(status_code=400, detail="only publish task can be retried")
    if t.get("status") == "running":
        raise HTTPException(status_code=400, detail="task is running")

    failed_step = _infer_failed_step(t)
    new_id = _new_task_id("publish")
    origin = t.get("retry_of") or task_id
    next_retry_count = int(t.get("retry_count") or 0) + 1
    asyncio.create_task(
        run_task_with_id(
            new_id,
            "publish",
            source="manual_retry",
            max_papers=int(os.getenv("PUBLISH_MAX_PAPERS", "5")),
            resume_from_task_id=task_id,
            resume_from_step=failed_step,
            retry_of=origin,
            retry_count=next_retry_count,
        )
    )
    return {"ok": True, "task_id": new_id, "resume_from_task_id": task_id, "resume_from_step": failed_step}


@app.post("/api/tasks/{task_id}/retry/{step_name}")
async def retry_task_from_step(task_id: str, step_name: str):
    step_name = (step_name or "").strip()
    if step_name not in PUBLISH_STEPS:
        raise HTTPException(status_code=400, detail="invalid step name")
    tasks = _load_tasks()
    t = next((x for x in tasks if x.get("task_id") == task_id), None)
    if not t:
        raise HTTPException(status_code=404, detail="task not found")
    if t.get("task_type") != "publish":
        raise HTTPException(status_code=400, detail="only publish task can be retried")
    if t.get("status") == "running":
        raise HTTPException(status_code=400, detail="task is running")

    new_id = _new_task_id("publish")
    origin = t.get("retry_of") or task_id
    next_retry_count = int(t.get("retry_count") or 0) + 1
    asyncio.create_task(
        run_task_with_id(
            new_id,
            "publish",
            source="manual_retry",
            max_papers=int(os.getenv("PUBLISH_MAX_PAPERS", "5")),
            resume_from_task_id=task_id,
            resume_from_step=step_name,
            retry_of=origin,
            retry_count=next_retry_count,
        )
    )
    return {"ok": True, "task_id": new_id, "resume_from_task_id": task_id, "resume_from_step": step_name}


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    tasks = _load_tasks()
    t = next((x for x in tasks if x.get("task_id") == task_id), None)
    if not t:
        raise HTTPException(status_code=404, detail="task not found")
    if t.get("task_type") != "publish":
        raise HTTPException(status_code=400, detail="only publish task can be canceled")
    if t.get("status") != "running":
        raise HTTPException(status_code=400, detail="task is not running")

    _update_task(task_id, {"cancel_requested": True, "cancel_requested_at": _now_iso()})
    with CANCEL_EVENTS_LOCK:
        ev = CANCEL_EVENTS.get(task_id)
    if ev:
        ev.set()
        return {"ok": True}

    # If the worker handle isn't present (e.g. service restarted), mark canceled best-effort.
    reason = "canceled by user"
    _cancel_running_steps(task_id, reason)
    _update_task(task_id, {"status": "canceled", "end_time": _now_iso(), "error_message": reason, "current_step": None})
    return {"ok": True, "note": "no worker handle; marked canceled"}


@app.get("/api/tasks/{task_id}/checkpoint")
async def get_task_checkpoint(task_id: str):
    cp = checkpoint_mgr.load_checkpoint(task_id)
    if not cp:
        raise HTTPException(status_code=404, detail="checkpoint not found")
    return cp


@app.get("/api/models/config")
async def get_models_config():
    return {"models": health_checker.get_config()}


@app.get("/api/models/health")
async def get_models_health():
    return {"models": health_checker.get_cached(), "interval_s": MODEL_HEALTH_CHECK_INTERVAL}


@app.get("/api/models/health/{model_name}")
async def get_model_health(model_name: str):
    cached = health_checker.get_cached()
    if model_name not in cached:
        raise HTTPException(status_code=404, detail="model not found")
    return cached[model_name]


@app.post("/api/models/health/check")
async def check_models_now():
    results = await health_checker.check_all()
    return {"models": {k: v.to_dict() for k, v in results.items()}}


@app.get("/api/jobs")
async def get_jobs():
    jobs = []
    for job in scheduler.get_jobs():
        next_run = job.next_run_time.isoformat() if job.next_run_time else None
        jobs.append({"id": job.id, "next_run_time": next_run, "trigger": str(job.trigger)})
    return {"jobs": jobs}


@app.get("/api/data/{file_path:path}")
async def get_data(file_path: str):
    if not file_path or not file_path.endswith(".json"):
        raise HTTPException(status_code=400, detail="only .json files are allowed")
    if file_path not in ("tasks.json", "scheduler_config.json"):
        raise HTTPException(status_code=404, detail="file not found")
    try:
        target = _safe_resolve_under(DATA_DIR, file_path)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid file path")
    if not target.exists():
        raise HTTPException(status_code=404, detail="file not found")
    try:
        with open(target, "r", encoding="utf-8") as f:
            return JSONResponse(content=json.load(f))
    except Exception:
        raise HTTPException(status_code=500, detail="failed to read json")


@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await websocket.accept()
    q = Queue()
    with log_queues_lock:
        log_queues.append(q)
    try:
        while True:
            try:
                # Non-blocking check to allow handling disconnects
                # In a real async loop we might use asyncio.Queue, but logging is thread-based.
                # efficiently bridging:
                while True:
                    try:
                        line = q.get_nowait()
                        await websocket.send_text(line)
                    except Empty:
                        break
                await asyncio.sleep(0.1)
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    finally:
        with log_queues_lock:
            if q in log_queues:
                log_queues.remove(q)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("MONITOR_PORT", "9999")))
