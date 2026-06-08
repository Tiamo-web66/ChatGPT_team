#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ChatGPT_team Web Server
=======================

FastAPI-based web interface for ChatGPT_team.py.

Start:
    python web_server.py
    # Open http://127.0.0.1:8000

Dependencies:
    pip install fastapi uvicorn
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ChatGPT_team as engine

try:
    from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse, JSONResponse
    import uvicorn
except ImportError:
    print("Missing dependencies. Run:")
    print("  pip install fastapi uvicorn")
    raise SystemExit(1)

# ── Paths ──────────────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
TEMPLATE_PATH = HERE / "templates" / "index.html"

# ── App ────────────────────────────────────────────────────────────
# ── In-memory task store ───────────────────────────────────────────
_active_tasks: dict[str, dict[str, Any]] = {}
_task_lock = threading.Lock()

# ── WebSocket broadcast queues ─────────────────────────────────────
_log_queues: list[asyncio.Queue] = []
_progress_queues: list[asyncio.Queue] = []


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════════
# Task Manager
# ═══════════════════════════════════════════════════════════════════

class TaskManager:
    """Runs ChatGPT_team functions in background threads, bridges logs/progress."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ── Log callback (called from any thread) ──────────────────────
    @staticmethod
    def _on_log(level: str, tag: str, message: str) -> None:
        payload = {
            "ts": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "tag": tag,
            "message": message,
        }
        # Push to all connected WebSocket queues
        for q in list(_log_queues):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    # ── Progress callback ──────────────────────────────────────────
    @staticmethod
    def _on_progress(success: int, fail: int, submitted: int, total: int) -> None:
        payload = {
            "success": success,
            "fail": fail,
            "submitted": submitted,
            "total": total,
        }
        for q in list(_progress_queues):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    # ── Submit a register task ─────────────────────────────────────
    def start_register(
        self,
        task_id: str,
        total: int,
        workers: int,
        proxy: str | None,
        output: str,
    ) -> None:
        def _run() -> None:
            try:
                engine.run_batch(total, workers, proxy, output)
            except SystemExit:
                pass
            except Exception as exc:
                engine._print_pipe("ERR", "Web", f"任务异常: {type(exc).__name__}: {exc}")
            finally:
                with _task_lock:
                    t = _active_tasks.get(task_id)
                    if t:
                        t["status"] = "done"
                        t["finished_at"] = _now_iso()

        with _task_lock:
            _active_tasks[task_id]["status"] = "running"
            _active_tasks[task_id]["started_at"] = _now_iso()

        t = threading.Thread(target=_run, name=f"web-task-{task_id[:8]}", daemon=True)
        _active_tasks[task_id]["_thread"] = t
        t.start()

    # ── Submit a check task ────────────────────────────────────────
    def start_check(self, task_id: str, workers: int, proxy: str | None) -> None:
        def _run() -> None:
            try:
                engine.run_token_check(proxy, workers=workers)
            except SystemExit:
                pass
            except Exception as exc:
                engine._print_pipe("ERR", "Web", f"检查异常: {type(exc).__name__}: {exc}")
            finally:
                with _task_lock:
                    t = _active_tasks.get(task_id)
                    if t:
                        t["status"] = "done"
                        t["finished_at"] = _now_iso()

        with _task_lock:
            _active_tasks[task_id]["status"] = "running"
            _active_tasks[task_id]["started_at"] = _now_iso()

        t = threading.Thread(target=_run, name=f"web-task-{task_id[:8]}", daemon=True)
        _active_tasks[task_id]["_thread"] = t
        t.start()


task_manager = TaskManager()


# ═══════════════════════════════════════════════════════════════════
# Lifespan
# ═══════════════════════════════════════════════════════════════════

@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    task_manager.set_loop(asyncio.get_running_loop())
    engine._LOG_CALLBACK = TaskManager._on_log
    engine._PROGRESS_CALLBACK = TaskManager._on_progress
    engine.QUIET_LOGS = False
    print(" ChatGPT_team Web 已启动 → http://127.0.0.1:8000")
    print(" 按 Ctrl+C 停止")
    yield
    # Shutdown
    engine._LOG_CALLBACK = None
    engine._PROGRESS_CALLBACK = None

app = FastAPI(title="ChatGPT_team Web", docs_url=None, redoc_url=None, lifespan=_lifespan)


# ═══════════════════════════════════════════════════════════════════
# Frontend
# ═══════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    if TEMPLATE_PATH.exists():
        html = TEMPLATE_PATH.read_text(encoding="utf-8")
    else:
        html = "<h2>templates/index.html 未找到，请先创建前端文件</h2>"
    return HTMLResponse(content=html)


# ═══════════════════════════════════════════════════════════════════
# Config API
# ═══════════════════════════════════════════════════════════════════

def _find_config_path() -> Path:
    """Find the local config file path (same logic as ChatGPT_team)."""
    candidates = ["ChatGPT_team.config.local.json", "ChatGPT_team.config.json"]
    for name in candidates:
        p = HERE / name
        if p.exists():
            return p
    # Return the local path even if it doesn't exist yet
    return HERE / candidates[0]


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    path = _find_config_path()
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
    return {
        "path": str(path),
        "config": raw if isinstance(raw, dict) else {},
    }


@app.put("/api/config")
async def update_config(body: dict[str, Any]) -> dict[str, Any]:
    path = _find_config_path()
    # Read existing
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    if not isinstance(existing, dict):
        existing = {}
    # Merge (shallow merge — user can send partial)
    existing.update(body or {})
    # Remove helper fields
    existing.pop("_说明", None)
    # Write
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    # Invalidate engine cache
    engine._CONFIG_CACHE = None
    return {"ok": True, "path": str(path)}


# ═══════════════════════════════════════════════════════════════════
# Token API
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/tokens")
async def list_tokens(search: str = "", limit: int = 200) -> dict[str, Any]:
    token_dir = engine._cwd_path(engine.CODEX_TOKEN_DIR)
    files = sorted(
        [p for p in token_dir.glob("*.json") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    items: list[dict[str, Any]] = []
    for fp in files:
        email = fp.stem
        if search and search.lower() not in email.lower():
            continue
        data = engine._load_json(fp)
        items.append(
            {
                "email": email,
                "type": data.get("type", ""),
                "token_source": data.get("token_source", ""),
                "saved_at": data.get("saved_at", ""),
                "has_rt": bool(data.get("refresh_token")),
                "has_at": bool(data.get("access_token")),
                "file": fp.name,
            }
        )
        if len(items) >= limit:
            break
    return {"total": len(items), "items": items}


@app.get("/api/tokens/{email}")
async def get_token(email: str) -> dict[str, Any]:
    fp = engine._codex_token_path(email)
    if not fp.exists():
        # Try search by filename
        token_dir = engine._cwd_path(engine.CODEX_TOKEN_DIR)
        matches = list(token_dir.glob(f"*{email}*.json"))
        if not matches:
            raise HTTPException(404, f"Token not found: {email}")
        fp = matches[0]
    data = engine._load_json(fp)
    if not data:
        raise HTTPException(404, f"Token empty: {email}")
    return {"email": email, "data": data, "file": fp.name}


@app.delete("/api/tokens/{email}")
async def delete_token(email: str) -> dict[str, Any]:
    fp = engine._codex_token_path(email)
    if not fp.exists():
        token_dir = engine._cwd_path(engine.CODEX_TOKEN_DIR)
        matches = list(token_dir.glob(f"*{email}*.json"))
        if not matches:
            raise HTTPException(404, f"Token not found: {email}")
        fp = matches[0]
    fp.unlink()
    return {"ok": True, "deleted": fp.name}


# ═══════════════════════════════════════════════════════════════════
# Task API
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/tasks")
async def list_tasks() -> dict[str, Any]:
    with _task_lock:
        result = []
        for tid, t in _active_tasks.items():
            result.append(
                {
                    "id": tid,
                    "type": t.get("type", ""),
                    "status": t.get("status", ""),
                    "total": t.get("total", 0),
                    "workers": t.get("workers", 1),
                    "created_at": t.get("created_at", ""),
                    "started_at": t.get("started_at", ""),
                    "finished_at": t.get("finished_at", ""),
                }
            )
        return {"tasks": result}


@app.post("/api/tasks/register")
async def create_register_task(body: dict[str, Any]) -> dict[str, Any]:
    total = max(1, int(body.get("total") or 1))
    workers = max(1, int(body.get("workers") or 1))
    proxy = str(body.get("proxy") or "").strip() or None
    output = str(body.get("output") or engine.REGISTERED_OUTPUT_FILE)

    task_id = f"reg_{uuid.uuid4().hex[:12]}"
    with _task_lock:
        _active_tasks[task_id] = {
            "id": task_id,
            "type": "register",
            "status": "pending",
            "total": total,
            "workers": workers,
            "proxy": proxy,
            "output": output,
            "created_at": _now_iso(),
            "started_at": "",
            "finished_at": "",
        }
    task_manager.start_register(task_id, total, workers, proxy, output)
    return {"ok": True, "task_id": task_id}


@app.post("/api/tasks/check")
async def create_check_task(body: dict[str, Any]) -> dict[str, Any]:
    workers = max(1, int(body.get("workers") or 100))
    proxy = str(body.get("proxy") or "").strip() or None

    task_id = f"chk_{uuid.uuid4().hex[:12]}"
    with _task_lock:
        _active_tasks[task_id] = {
            "id": task_id,
            "type": "check",
            "status": "pending",
            "total": 0,
            "workers": workers,
            "proxy": proxy,
            "output": "",
            "created_at": _now_iso(),
            "started_at": "",
            "finished_at": "",
        }
    task_manager.start_check(task_id, workers, proxy)
    return {"ok": True, "task_id": task_id}


@app.post("/api/tasks/{task_id}/stop")
async def stop_task(task_id: str) -> dict[str, Any]:
    with _task_lock:
        t = _active_tasks.get(task_id)
        if not t:
            raise HTTPException(404, f"Task not found: {task_id}")
        if t["status"] not in ("running", "pending"):
            return {"ok": False, "reason": f"Task already {t['status']}"}
        t["status"] = "stopping"
    engine._print_pipe("WARN", "Web", f"用户请求停止任务 {task_id}")
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════
# WebSocket — Log Stream
# ═══════════════════════════════════════════════════════════════════

@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket) -> None:
    await ws.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    _log_queues.append(queue)
    try:
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=30)
                await ws.send_json(payload)
            except asyncio.TimeoutError:
                # Send heartbeat
                try:
                    await ws.send_json({"ts": "", "level": "PING", "tag": "", "message": ""})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if queue in _log_queues:
            _log_queues.remove(queue)


# ═══════════════════════════════════════════════════════════════════
# WebSocket — Progress Stream
# ═══════════════════════════════════════════════════════════════════

@app.websocket("/ws/progress")
async def ws_progress(ws: WebSocket) -> None:
    await ws.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _progress_queues.append(queue)
    try:
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=30)
                await ws.send_json(payload)
            except asyncio.TimeoutError:
                try:
                    await ws.send_json({"type": "ping"})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if queue in _progress_queues:
            _progress_queues.remove(queue)


# ═══════════════════════════════════════════════════════════════════
# Stats API (quick summary for dashboard)
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/stats")
async def get_stats() -> dict[str, Any]:
    token_dir = engine._cwd_path(engine.CODEX_TOKEN_DIR)
    token_count = len([p for p in token_dir.glob("*.json") if p.is_file()])
    session_dir = engine._cwd_path(engine.CHATGPT_SESSION_DIR)
    session_count = len([p for p in session_dir.glob("*.json") if p.is_file()])
    running_tasks = sum(1 for t in _active_tasks.values() if t.get("status") == "running")
    return {
        "token_count": token_count,
        "session_count": session_count,
        "running_tasks": running_tasks,
    }


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

def main() -> None:
    uvicorn.run(
        "web_server:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
