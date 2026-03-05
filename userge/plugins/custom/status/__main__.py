""" merged status dashboard for all active tasks """

import asyncio
import time
from typing import Dict, Optional, Any

import psutil

from userge import userge, Message, config
from userge.utils import humanbytes

_TASKS: Dict[str, Dict[str, Any]] = {}
_STATUS_MSG: Optional[Message] = None
_UPDATE_LOCK = asyncio.Lock()
_UPDATER_TASK: Optional[asyncio.Task] = None

# Minimum seconds between message edits to avoid Telegram flood limits
_MIN_EDIT_INTERVAL = 5

# Prime the CPU measurement so subsequent calls return accurate values
psutil.cpu_percent(interval=None)


def register_task(task_id: str, name: str, kind: str = "download") -> None:
    """Register a new task in the status dashboard."""
    _TASKS[task_id] = {
        "name": name,
        "kind": kind,       # "download" | "upload"
        "speed": 0,
        "done": 0,
        "total": 0,
        "eta": "",
        "status": "active",
        "started": time.time(),
    }
    _ensure_updater()


def update_task(task_id: str, **kwargs) -> None:
    """Update progress fields for an existing task."""
    if task_id in _TASKS:
        _TASKS[task_id].update(kwargs)


def complete_task(task_id: str) -> None:
    """Mark a task as completed."""
    if task_id in _TASKS:
        _TASKS[task_id]["status"] = "done"


def remove_task(task_id: str) -> None:
    """Remove a task from the dashboard."""
    _TASKS.pop(task_id, None)


def _ensure_updater() -> None:
    global _UPDATER_TASK  # pylint: disable=global-statement
    if _UPDATER_TASK is None or _UPDATER_TASK.done():
        try:
            loop = asyncio.get_running_loop()
            _UPDATER_TASK = loop.create_task(_status_updater())
        except RuntimeError:
            pass


async def _status_updater() -> None:
    global _STATUS_MSG  # pylint: disable=global-statement
    last_edit = 0.0
    while True:
        if not _TASKS:
            # No active tasks — stop the updater
            break
        now = time.time()
        if now - last_edit >= _MIN_EDIT_INTERVAL:
            text = _build_status_text()
            if _STATUS_MSG is not None:
                try:
                    await _STATUS_MSG.edit(text, parse_mode="html")
                    last_edit = now
                except Exception:  # pylint: disable=broad-except
                    pass
        # Remove completed tasks after showing once
        for tid in list(_TASKS):
            if _TASKS[tid]["status"] == "done":
                remove_task(tid)
        await asyncio.sleep(2)


def _build_status_text() -> str:
    if not _TASKS:
        return "No active tasks."

    dl_speed = sum(t["speed"] for t in _TASKS.values() if t["kind"] == "download")
    ul_speed = sum(t["speed"] for t in _TASKS.values() if t["kind"] == "upload")

    cpu = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory()
    ram_used = humanbytes(ram.used)
    ram_total = humanbytes(ram.total)

    lines = [
        "<b>📊 Status Dashboard</b>",
        f"⬇ Total DL: <code>{humanbytes(dl_speed)}/s</code>  "
        f"⬆ Total UL: <code>{humanbytes(ul_speed)}/s</code>",
        f"🖥 CPU: <code>{cpu:.1f}%</code>  "
        f"🧠 RAM: <code>{ram_used}/{ram_total}</code>",
        "",
    ]

    for tid, task in list(_TASKS.items()):
        icon = "⬇" if task["kind"] == "download" else "⬆"
        name = task["name"][:40] + ("…" if len(task["name"]) > 40 else "")
        speed_str = humanbytes(task["speed"]) + "/s" if task["speed"] else "–"
        done_str = humanbytes(task["done"])
        total_str = humanbytes(task["total"]) if task["total"] else "?"
        eta_str = task.get("eta", "")
        pct = (task["done"] / task["total"] * 100) if task["total"] else 0
        bar = _make_bar(pct)
        status = task.get("status", "active")
        status_icon = "✅" if status == "done" else icon
        lines.append(
            f"{status_icon} <code>{name}</code>\n"
            f"  {bar} {pct:.1f}%\n"
            f"  {done_str}/{total_str} @ {speed_str}"
            + (f"  ETA: {eta_str}" if eta_str else "")
        )

    return "\n".join(lines)


def _make_bar(pct: float, width: int = 10) -> str:
    filled = int(pct / 100 * width)
    bar = config.FINISHED_PROGRESS_STR * filled + config.UNFINISHED_PROGRESS_STR * (width - filled)
    return f"[{bar}]"


@userge.on_cmd("status", about={
    'header': "Show merged download/upload status dashboard",
    'usage': "{tr}status"})
async def status_cmd(message: Message):
    """ show or attach merged status dashboard """
    global _STATUS_MSG  # pylint: disable=global-statement
    text = _build_status_text()
    await message.edit(text, parse_mode="html")
    _STATUS_MSG = message
    _ensure_updater()
