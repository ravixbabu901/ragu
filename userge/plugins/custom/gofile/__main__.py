""" GoFile download and upload plugin """

import asyncio
import hashlib
import math
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List

import aiohttp
from aiohttp import MultipartWriter

from userge import userge, Message, config
from userge.utils import humanbytes, time_formatter

try:
    from userge.plugins.custom.status import (
        register_task, update_task, complete_task, remove_task)
    _STATUS_AVAILABLE = True
except Exception:  # pylint: disable=broad-except
    _STATUS_AVAILABLE = False

LOGS = userge.getLogger(__name__)

GOFILE_TOKEN = os.environ.get("GOFILE_TOKEN", "")
_GOFILE_BASE = "https://api.gofile.io"

# Lazy aria2p client — connected only when .godl is first called.
_aria2 = None  # type: ignore

def _get_aria2():
    global _aria2  # pylint: disable=global-statement
    if _aria2 is None:
        try:
            import aria2p  # pylint: disable=import-outside-toplevel
            _aria2 = aria2p.API(
                aria2p.Client(host="http://localhost", port=6800, secret="")
            )
        except Exception as e:  # pylint: disable=broad-except
            raise RuntimeError(
                f"aria2p client not available. Is the aria plugin loaded? ({e})"
            ) from e
    return _aria2

# ------------------------------------------------------------------
# GoFile 2026 auth helpers
# ------------------------------------------------------------------

_GOFILE_UA = "Mozilla/5.0"
_GOFILE_LANG = "en-US"
_GOFILE_STATIC_SECRET = "gf2026x"

# UUID pattern — GoFile internal folder/file IDs look like this
_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE
)

def _generate_x_website_token(bearer_token: str) -> str:
    time_bucket = str(math.floor(int(time.time()) / 14400))
    raw = (
        f"{_GOFILE_UA}::{_GOFILE_LANG}::{bearer_token}"
        f"::{time_bucket}::{_GOFILE_STATIC_SECRET}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

async def _gofile_get_download_info(content_id: str) -> tuple:
    """Returns (account_token, files_dict) using the 2026 auth flow."""
    async with aiohttp.ClientSession() as session:
        headers = {"User-Agent": _GOFILE_UA, "Origin": "https://gofile.io"}

        async with session.post(
            f"{_GOFILE_BASE}/accounts", headers=headers, json={}
        ) as resp:
            acc_data = await resp.json()

        if acc_data.get("status") != "ok":
            raise RuntimeError(f"GoFile account creation failed: {acc_data}")

        token = acc_data["data"]["token"]
        headers["Authorization"] = f"Bearer {token}"
        headers["Cookie"] = f"accountToken={token}"
        headers["X-Website-Token"] = _generate_x_website_token(token)
        headers["X-bl"] = _GOFILE_LANG

        async with session.get(
            f"{_GOFILE_BASE}/contents/{content_id}", headers=headers
        ) as resp:
            data = await resp.json()

    if data.get("status") != "ok":
        raise RuntimeError(f"GoFile content fetch failed: {data}")

    content = data["data"]
    if content.get("type") == "file":
        files = {content["name"]: content}
    else:
        children = content.get("children", {})
        files = {k: v for k, v in children.items() if v.get("type") == "file"}

    return token, files

async def _resolve_folder_id(session: aiohttp.ClientSession, folder_code_or_id: str) -> str:
    """
    Given either:
      - A GoFile internal folder UUID  (e.g. f2a7a119-d9e7-...)  → return as-is
      - A GoFile folder share code     (e.g. MhvUoL, DSUnj9)     → look up UUID via API

    Returns the internal folder UUID string.
    """
    if _UUID_RE.match(folder_code_or_id):
        return folder_code_or_id

    headers = {"User-Agent": _GOFILE_UA, "Origin": "https://gofile.io"}
    async with session.post(
        f"{_GOFILE_BASE}/accounts", headers=headers, json={}
    ) as resp:
        acc_data = await resp.json()

    if acc_data.get("status") != "ok":
        raise RuntimeError(f"GoFile account creation failed (folder resolve): {acc_data}")

    token = acc_data["data"]["token"]
    headers["Authorization"] = f"Bearer {token}"
    headers["Cookie"] = f"accountToken={token}"
    headers["X-Website-Token"] = _generate_x_website_token(token)
    headers["X-bl"] = _GOFILE_LANG

    async with session.get(
        f"{_GOFILE_BASE}/contents/{folder_code_or_id}", headers=headers
    ) as resp:
        data = await resp.json()

    if data.get("status") != "ok":
        raise RuntimeError(
            f"Could not resolve GoFile folder '{folder_code_or_id}': {data}"
        )

    folder_id = data["data"].get("id")
    if not folder_id:
        raise RuntimeError(
            f"GoFile folder '{folder_code_or_id}' has no 'id' field in response."
        )
    return folder_id

# ------------------------------------------------------------------
# Download command  (.godl)
# ------------------------------------------------------------------
@userge.on_cmd("godl", about={
    'header': "Download files from GoFile via aria2",
    'usage': "{tr}godl <gofile_url_or_content_id>",
    'examples': ["{tr}godl https://gofile.io/d/AbCdEf",
                 "{tr}godl AbCdEf"]},
    check_downpath=True)
async def godl_(message: Message):
    """ download from GoFile """
    inp = message.input_str.strip() if message.input_str else ""
    if not inp:
        await message.err("Provide a GoFile URL or content ID.")
        return

    if "gofile.io/d/" in inp:
        content_id = inp.rstrip("/").split("/d/")[-1]
    else:
        content_id = inp

    await message.edit(f"`Fetching GoFile content: {content_id} …`")

    try:
        token, files = await _gofile_get_download_info(content_id)
    except Exception as e:  # pylint: disable=broad-except
        await message.err(str(e))
        return

    if not files:
        await message.err("No downloadable files found.")
        return

    try:
        aria2 = _get_aria2()
    except RuntimeError as e:
        await message.err(str(e))
        return

    dl_dir = os.path.join("/app", config.Dynamic.DOWN_PATH)
    os.makedirs(dl_dir, exist_ok=True)

    queued = []
    for fname, fdata in files.items():
        link = fdata.get("link") or fdata.get("directLink")
        if not link:
            LOGS.warning("No link for file %s, skipping.", fname)
            continue

        size = fdata.get("size", 0)
        options = {
            "dir": dl_dir,
            "allow-overwrite": "true",
            "max-connection-per-server": "14",
            "split": "14",
            "header": [f"Cookie: accountToken={token}"],
        }

        try:
            download = aria2.add_uris([link], options=options)
            queued.append((download.gid, fname, size))
            LOGS.info("Queued GoFile download: %s  gid=%s", fname, download.gid)
        except Exception as e:  # pylint: disable=broad-except
            await message.err(f"Failed to queue `{fname}`: {e}")
            return

    if not queued:
        await message.err("No files could be queued.")
        return

    total_files = len(queued)
    completed_files = []
    start_t = datetime.now()

    for idx, (gid, api_fname, size) in enumerate(queued, start=1):
        await message.edit(
            f"`[{idx}/{total_files}] Starting: {api_fname} ({humanbytes(size)}) …`"
        )
        result = await _godl_progress(aria2, gid, message, api_fname, idx, total_files)
        if result:
            completed_files.append(result)

    m_s = (datetime.now() - start_t).seconds

    if completed_files:
        lines = [
            f"✅ **Successfully Downloaded {len(completed_files)}/{total_files} file(s) in {m_s} seconds**\n"
        ]
        for i, (fname, size, dest) in enumerate(completed_files, start=1):
            lines.append(
                f"**{i}. Name :** `{fname}`\n"
                f"   **Size :** `{humanbytes(size)}`\n"
                f"   **Path :** `{dest}`"
            )
        await message.edit("\n\n".join(lines))

async def _godl_progress(
    aria2,
    gid: str,
    message: Message,
    api_name: str,
    file_index: int = 1,
    total_files: int = 1,
):
    if _STATUS_AVAILABLE:
        register_task(gid, api_name, kind="download")

    previous = ""

    while True:
        try:
            t_file = aria2.get_download(gid)
        except Exception:  # pylint: disable=broad-except
            if _STATUS_AVAILABLE:
                remove_task(gid)
            await message.edit("Download cancelled by user ...")
            return None

        if t_file.error_message:
            if _STATUS_AVAILABLE:
                remove_task(gid)
            await message.err(str(t_file.error_message))
            return None

        resolved_name = t_file.name if t_file.name else api_name

        if t_file.is_complete:
            if _STATUS_AVAILABLE:
                complete_task(gid)
            dest = os.path.join(t_file.dir, t_file.name or api_name)
            final_size = t_file.total_length or 0
            return (resolved_name, final_size, dest)

        percentage = int(t_file.progress)
        downloaded = (
            percentage * int(t_file.total_length) / 100
            if t_file.total_length else 0
        )

        if _STATUS_AVAILABLE:
            update_task(
                gid,
                name=resolved_name,
                speed=t_file.download_speed,
                done=int(downloaded),
                total=int(t_file.total_length),
                eta=t_file.eta_string(),
            )

        prog_str = "Downloading ....\n[{0}{1}] {2}".format(
            "".join(
                config.FINISHED_PROGRESS_STR
                for _ in range(math.floor(percentage / 10))
            ),
            "".join(
                config.UNFINISHED_PROGRESS_STR
                for _ in range(10 - math.floor(percentage / 10))
            ),
            t_file.progress_string(),
        )

        info_msg = f"**Connections**: `{t_file.connections}`\n"
        file_counter = f"[{file_index}/{total_files}] " if total_files > 1 else ""

        msg = (
            f"`{file_counter}{prog_str}`\n"
            f"**Name**: `{resolved_name}`\n"
            f"**Completed**: {humanbytes(downloaded)}\n"
            f"**Total**: {t_file.total_length_string()}\n"
            f"**Speed**: {t_file.download_speed_string()} 🔻\n"
            f"{info_msg}"
            f"**ETA**: {t_file.eta_string()}\n"
            f"**GID** : `{gid}`"
        )

        if msg != previous:
            await message.edit(msg)
            previous = msg

        await asyncio.sleep(config.Dynamic.EDIT_SLEEP_TIMEOUT)

# ------------------------------------------------------------------
# Upload helpers
# ------------------------------------------------------------------
async def _get_best_server(session: aiohttp.ClientSession) -> str:
    async with session.get(f"{_GOFILE_BASE}/servers") as resp:
        data = await resp.json()
    return data["data"]["servers"][0]["name"]

async def _rename_gofile_content(
    session: aiohttp.ClientSession,
    content_id: str,
    new_name: str,
) -> None:
    headers = {
        "Authorization": f"Bearer {GOFILE_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"attribute": "name", "attributeValue": new_name}
    async with session.put(
        f"{_GOFILE_BASE}/contents/{content_id}/update",
        headers=headers,
        json=payload,
    ) as resp:
        data = await resp.json()
    if data.get("status") != "ok":
        LOGS.warning("GoFile rename failed for %s → %s: %s", content_id, new_name, data)

async def _create_gofile_folder(
    session: aiohttp.ClientSession,
    parent_folder_id: str,
    folder_name: str,
) -> str:
    headers = {
        "Authorization": f"Bearer {GOFILE_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"parentFolderId": parent_folder_id, "folderName": folder_name}
    async with session.post(
        f"{_GOFILE_BASE}/contents/createFolder",
        headers=headers,
        json=payload,
    ) as resp:
        data = await resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"GoFile create folder failed: {data}")
    return data["data"]["folderId"]

def _make_progress_str(
    file_name: str,
    file_size: int,
    uploaded: int,
    speed: float,
    eta: int,
    completed: int = 0,
    total_count: int = 1,
) -> str:
    percentage = (uploaded / file_size * 100) if file_size else 0
    filled = math.floor(percentage / 5)
    bar = (
        "".join(config.FINISHED_PROGRESS_STR for _ in range(filled))
        + "".join(config.UNFINISHED_PROGRESS_STR for _ in range(20 - filled))
    )
    return (
        "__Uploading to GoFile...__\n"
        f"```
[{bar}]({round(percentage, 2)}%)```
"
        f"**File Name** : `{file_name}`\n"
        f"**File Size** : `{humanbytes(file_size)}`\n"
        f"**Uploaded** : `{humanbytes(uploaded)}`\n"
        f"**Completed** : `{completed}/{total_count}`\n"
        f"**Speed** : `{humanbytes(int(speed))}/s`\n"
        f"**ETA** : `{time_formatter(eta)}`"
    )

async def _upload_single_file(
    session: aiohttp.ClientSession,
    server: str,
    src: Path,
    task_id: str,
    total_size: int,
    progress_queue: asyncio.Queue,
    folder_id: Optional[str] = None,
    completed_count: int = 0,
    total_count: int = 1,
) -> dict:
    """
    Upload one file to GoFile using MultipartWriter with a raw Content-Disposition
    header so filenames with spaces, brackets, etc. are preserved exactly.
    Returns result["data"] dict.
    """
    upload_url = f"https://{server}.gofile.io/contents/uploadfile"
    auth_headers = {"Authorization": f"Bearer {GOFILE_TOKEN}"}

    filename = src.name
    safe_name = filename.replace('"', '\\"")
    sent = 0
    start = time.time()
    _last_progress_time = [0.0]

    def on_bytes(n: int):
        nonlocal sent
        sent += n
        now = time.time()
        elapsed = now - start or 0.001
        speed = sent / elapsed
        eta = int((total_size - sent) / speed) if speed and total_size > sent else 0

        if now - _last_progress_time[0] >= config.Dynamic.EDIT_SLEEP_TIMEOUT:
            _last_progress_time[0] = now
            progress_str = _make_progress_str(
                file_name=filename,
                file_size=total_size,
                uploaded=sent,
                speed=speed,
                eta=eta,
                completed=completed_count,
                total_count=total_count,
            )
            try:
                progress_queue.put_nowait(("progress", progress_str))
            except asyncio.QueueFull:
                pass

        if _STATUS_AVAILABLE:
            update_task(task_id, speed=int(speed), done=sent, total=total_size)

    # Read file into buffer while reporting progress
    buf = bytearray()
    with open(src, "rb") as f:
        while True:
            chunk = f.read(256 * 1024)
            if not chunk:
                break
            buf.extend(chunk)
            on_bytes(len(chunk))

    # Build multipart body with raw Content-Disposition to avoid percent-encoding.
    mp = MultipartWriter("form-data")

    # folderId field must come before the file field
    if folder_id:
        folder_part = mp.append(folder_id)
        folder_part.set_content_disposition("form-data", name="folderId")

    file_payload = mp.append(bytes(buf), {"content-type": "application/octet-stream"})
    file_payload.set_content_disposition("form-data", name="file", filename=safe_name)
    # Override: aiohttp's set_content_disposition still percent-encodes — bypass it.
    file_payload.headers["Content-Disposition"] = (
        f'form-data; name="file"; filename="{safe_name}"'
    )

    async with session.post(upload_url, data=mp, headers=auth_headers) as resp:
        import json as _json  # pylint: disable=import-outside-toplevel
        content_type = resp.headers.get("Content-Type", "")
        raw_text = await resp.text()
        if "application/json" not in content_type and "json" not in content_type:
            raise RuntimeError(
                f"GoFile upload returned non-JSON response "
                f"(HTTP {resp.status}): {raw_text[:200]}"
            )
        result = _json.loads(raw_text)

    if result.get("status") != "ok":
        raise RuntimeError(f"GoFile upload failed: {result}")

    return result["data"]

async def _progress_display_loop(
    message: Message,
    progress_queue: asyncio.Queue,
) -> tuple:
    """
    Drain progress_queue until ("done",...) or ("error",...).
    Returns (link_or_None, error_or_None).
    """
    last_progress = None
    while True:
        try:
            item = await asyncio.wait_for(
                progress_queue.get(),
                timeout=config.Dynamic.EDIT_SLEEP_TIMEOUT
            )
        except asyncio.TimeoutError:
            if last_progress:
                try:
                    await message.edit(last_progress)
                except Exception:  # pylint: disable=broad-except
                    pass
            continue

        kind, payload = item
        if kind == "done":
            return payload, None
        if kind == "error":
            return None, payload
        if kind == "progress":
            last_progress = payload
            try:
                await message.edit(payload)
            except Exception:  # pylint: disable=broad-except
                pass


def _resolve_glob_patterns(patterns: List[str], base_dir: Path) -> List[Path]:
    """Resolve glob patterns. Returns sorted, deduplicated file list."""
    found = []
    seen: set = set()
    for pat in patterns:
        pat = pat.strip()
        if not pat:
            continue
        p = Path(pat)
        if p.is_absolute():
            search_root = Path(p.root)
            rel = str(p.relative_to(search_root))
        else:
            search_root = base_dir
            rel = str(p)

        if '*' in rel or '?' in rel:
            for match in sorted(search_root.glob(rel)):
                if match.is_file() and str(match) not in seen:
                    seen.add(str(match))
                    found.append(match)
        else:
            candidate = p if p.is_absolute() else base_dir / p
            if not candidate.exists():
                candidate = Path(config.Dynamic.DOWN_PATH) / p
            if candidate.is_file() and str(candidate) not in seen:
                seen.add(str(candidate))
                found.append(candidate)
    return found

# ------------------------------------------------------------------
# Upload command  (.goup)
# ------------------------------------------------------------------
@userge.on_cmd("goup", about={
    'header': "Upload file(s) or folder to GoFile",
    'usage': (
        "{tr}goup <file_or_folder>\n"
        "{tr}goup file1.mkv | file2.mkv | *.srt\n"
        "  (optional 2nd line: GoFile URL, folder code, or internal folder UUID)"
    ),
    'examples': [
        "{tr}goup video.mkv",
        "{tr}goup file1.mkv | file2.srt",
        "{tr}goup *.mkv | *.srt\nhttps://gofile.io/d/MhvUoL",
        "{tr}goup *.srt\nf2a7a119-d9e7-4eed-909f-4719478db0a4",
    ]})
async def goup_(message: Message):
    """ upload to GoFile """ 
    if not GOFILE_TOKEN:
        await message.err(
            "Set `GOFILE_TOKEN` environment variable to enable GoFile uploads.")
        return

    raw = message.input_str.strip() if message.input_str else ""
    if not raw:
        await message.err("Provide a file path, folder path, or file patterns.")
        return

    lines = raw.splitlines()
    file_line = lines[0].strip()
    existing_folder_line = lines[1].strip() if len(lines) > 1 else ""

    existing_folder_raw: Optional[str] = None
    if existing_folder_line:
        if "gofile.io/d/" in existing_folder_line:
            existing_folder_raw = existing_folder_line.rstrip("/").split("/d/")[-1]
        else:
            existing_folder_raw = existing_folder_line.strip()

    base_dir = Path(config.Dynamic.DOWN_PATH)

    if "|" not in file_line:
        src = Path(file_line)
        if not src.is_absolute():
            src_candidate = base_dir / file_line
            if src_candidate.exists():
                src = src_candidate
        if src.is_dir():
            await _goup_folder(message, src, existing_folder_raw=existing_folder_raw)
            return
        if src.is_file():
            await _goup_file(message, src, existing_folder_raw=existing_folder_raw)
            return
        files = _resolve_glob_patterns([file_line], base_dir)
        if not files:
            await message.err(f"File not found: `{file_line}`")
            return
        if len(files) == 1:
            await _goup_file(message, files[0], existing_folder_raw=existing_folder_raw)
        else:
            await _goup_multi(message, files, existing_folder_raw=existing_folder_raw)
        return

    patterns = [p.strip() for p in file_line.split("|")]
    files = _resolve_glob_patterns(patterns, base_dir)
    if not files:
        await message.err("No matching files found.")
        return
    if len(files) == 1:
        await _goup_file(message, files[0], existing_folder_raw=existing_folder_raw)
    else:
        await _goup_multi(message, files, existing_folder_raw=existing_folder_raw)

# ------------------------------------------------------------------
# Single-file upload
# ------------------------------------------------------------------
async def _goup_file(
    message: Message,
    src: Path,
    existing_folder_raw: Optional[str] = None,
) -> None:
    display_name = src.name
    size = src.stat().st_size
    task_id = f"goup_{display_name}"

    await message.edit(f"`Starting GoFile upload: {display_name} ({humanbytes(size)})`")

    if _STATUS_AVAILABLE:
        register_task(task_id, display_name, kind="upload")

    progress_queue: asyncio.Queue = asyncio.Queue()
    start_t = datetime.now()

    async def _run():
        try:
            async with aiohttp.ClientSession() as session:
                server = await _get_best_server(session)

                existing_folder_id: Optional[str] = None
                if existing_folder_raw:
                    existing_folder_id = await _resolve_folder_id(session, existing_folder_raw)

                file_data = await _upload_single_file(
                    session, server, src, task_id, size, progress_queue,
                    folder_id=existing_folder_id,
                    completed_count=0, total_count=1,
                )
                folder_code = file_data.get("parentFolderCode") or ""
                parent_folder_id = file_data.get("parentFolder") or ""

                if parent_folder_id and not existing_folder_id:
                    await _rename_gofile_content(session, parent_folder_id, display_name)

                if existing_folder_raw:
                    link = f"https://gofile.io/d/{existing_folder_raw}"
                elif folder_code:
                    link = f"https://gofile.io/d/{folder_code}"
                else:
                    link = file_data.get("downloadPage", "")

            await progress_queue.put(("done", link))
        except Exception as exc:  # pylint: disable=broad-except
            await progress_queue.put(("error", str(exc)))

    asyncio.ensure_future(_run())
    result_link, error_msg = await _progress_display_loop(message, progress_queue)
    m_s = (datetime.now() - start_t).seconds

    if error_msg:
        if _STATUS_AVAILABLE:
            remove_task(task_id)
        await message.err(error_msg)
        return

    if _STATUS_AVAILABLE:
        complete_task(task_id)

    await message.edit(
        f"✅ **Uploaded Successfully in {m_s} seconds**\n\n"
        f"**File Name** : `{display_name}`\n"
        f"**File Size** : `{humanbytes(size)}`\n"
        f"🔗 **Link** : {result_link}"
    )

# ------------------------------------------------------------------
# Multi-file upload
# ------------------------------------------------------------------
async def _goup_multi(
    message: Message,
    files: List[Path],
    existing_folder_raw: Optional[str] = None,
) -> None:
    total_count = len(files)
    folder_name = files[0].name
    total_size = sum(f.stat().st_size for f in files)

    await message.edit(
        f"`Uploading {total_count} files → GoFile folder '{folder_name}'…`\n"
        f"`Total: {humanbytes(total_size)}`"
    )

    progress_queue: asyncio.Queue = asyncio.Queue()
    start_t = datetime.now()

    async def _run():
        try:
            async with aiohttp.ClientSession() as session:
                server = await _get_best_server(session)

                pre_resolved_id: Optional[str] = None
                if existing_folder_raw:
                    pre_resolved_id = await _resolve_folder_id(session, existing_folder_raw)

                target_folder_id: Optional[str] = pre_resolved_id
                folder_code: Optional[str] = existing_folder_raw

                for i, src in enumerate(files):
                    task_id = f"goup_{src.name}"
                    if _STATUS_AVAILABLE:
                        register_task(task_id, src.name, kind="upload")

                    file_data = await _upload_single_file(
                        session, server, src, task_id, src.stat().st_size,
                        progress_queue,
                        folder_id=target_folder_id,
                        completed_count=i,
                        total_count=total_count,
                    )

                    if _STATUS_AVAILABLE:
                        complete_task(task_id)

                    if target_folder_id is None:
                        target_folder_id = file_data.get("parentFolder") or None
                        folder_code = file_data.get("parentFolderCode") or None
                        if target_folder_id:
                            await _rename_gofile_content(session, target_folder_id, folder_name)

                if folder_code:
                    link = f"https://gofile.io/d/{folder_code}"
                else:
                    link = ""

                await progress_queue.put(("done", link))
        except Exception as exc:  # pylint: disable=broad-except
            await progress_queue.put(("error", str(exc)))

    asyncio.ensure_future(_run())
    result_link, error_msg = await _progress_display_loop(message, progress_queue)
    m_s = (datetime.now() - start_t).seconds

    if error_msg:
        await message.err(error_msg)
        return

    await message.edit(
        f"✅ **Uploaded Successfully in {m_s} seconds**\n\n"
        f"**Folder** : `{folder_name}`\n"
        f"**Files** : `{total_count}`\n"
        f"**Total Size** : `{humanbytes(total_size)}`\n"
        f"🔗 **Link** : {result_link}"
    )

# ------------------------------------------------------------------
# Folder upload
# ------------------------------------------------------------------
async def _goup_folder(
    message: Message,
    src_dir: Path,
    existing_folder_raw: Optional[str] = None,
) -> None:
    files: List[Path] = sorted(
        [p for p in src_dir.rglob("*") if p.is_file()],
        key=lambda p: str(p)
    )
    if not files:
        await message.err(f"Folder `{src_dir.name}` is empty.")
        return

    total_count = len(files)
    folder_name = src_dir.name
    total_size = sum(f.stat().st_size for f in files)

    await message.edit(
        f"`Uploading folder '{folder_name}' ({total_count} files, {humanbytes(total_size)})…`"
    )

    progress_queue: asyncio.Queue = asyncio.Queue()
    start_t = datetime.now()

    async def _run():
        try:
            async with aiohttp.ClientSession() as session:
                server = await _get_best_server(session)

                pre_resolved_id: Optional[str] = None
                if existing_folder_raw:
                    pre_resolved_id = await _resolve_folder_id(session, existing_folder_raw)

                root_folder_id: Optional[str] = pre_resolved_id
                folder_code: Optional[str] = existing_folder_raw

                folder_cache: dict = {}

                for i, src in enumerate(files):
                    rel = src.relative_to(src_dir)
                    parent_rel = str(rel.parent)

                    if parent_rel == ".":
                        target_folder_id = root_folder_id
                    else:
                        if parent_rel not in folder_cache:
                            parts = Path(parent_rel).parts
                            current_rel = "."
                            for part in parts:
                                new_rel = os.path.join(current_rel, part)
                                if new_rel not in folder_cache:
                                    parent_gf_id = folder_cache.get(current_rel, root_folder_id)
                                    if parent_gf_id is not None:
                                        gf_id = await _create_gofile_folder(
                                            session, parent_gf_id, part)
                                        folder_cache[new_rel] = gf_id
                                    else:
                                        folder_cache[new_rel] = None
                                current_rel = new_rel
                    target_folder_id = folder_cache.get(parent_rel)

                    task_id = f"goup_{src.name}"
                    if _STATUS_AVAILABLE:
                        register_task(task_id, src.name, kind="upload")

                    file_data = await _upload_single_file(
                        session, server, src, task_id, src.stat().st_size,
                        progress_queue,
                        folder_id=target_folder_id,
                        completed_count=i,
                        total_count=total_count,
                    )

                    if _STATUS_AVAILABLE:
                        complete_task(task_id)

                    if root_folder_id is None:
                        root_folder_id = file_data.get("parentFolder") or None
                        folder_code = file_data.get("parentFolderCode") or None
                        if root_folder_id and not pre_resolved_id:
                            await _rename_gofile_content(session, root_folder_id, folder_name)
                        for k in list(folder_cache.keys()):
                            if folder_cache[k] is None:
                                folder_cache[k] = root_folder_id

                if folder_code:
                    link = f"https://gofile.io/d/{folder_code}"
                else:
                    link = ""

                await progress_queue.put(("done", link))
        except Exception as exc:  # pylint: disable=broad-except
            await progress_queue.put(("error", str(exc)))

    asyncio.ensure_future(_run())
    result_link, error_msg = await _progress_display_loop(message, progress_queue)
    m_s = (datetime.now() - start_t).seconds

    if error_msg:
        await message.err(error_msg)
        return

    await message.edit(
        f"✅ **Uploaded Successfully in {m_s} seconds**\n\n"
        f"**Folder** : `{folder_name}`\n"
        f"**Files** : `{total_count}`\n"
        f"**Total Size** : `{humanbytes(total_size)}`\n"
        f"🔗 **Link** : {result_link}"
    )
