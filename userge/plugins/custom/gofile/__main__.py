""" GoFile download and upload plugin """

import asyncio
import glob as _glob
import hashlib
import math
import os
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import aiohttp
from aiohttp import MultipartWriter
from aiohttp.payload import BufferedReaderPayload

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

# Lazy aria2p client — only connected when first needed by .godl
# so that gofile loads even if the aria plugin hasn't started aria2c yet.
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
                f"aria2p client not available. Make sure the aria plugin is loaded. ({e})"
            ) from e
    return _aria2


# ------------------------------------------------------------------
# GoFile 2026 auth helpers
# ------------------------------------------------------------------

_GOFILE_UA = "Mozilla/5.0"
_GOFILE_LANG = "en-US"
_GOFILE_STATIC_SECRET = "gf2026x"


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
            f"✅ **Successfully Downloaded "
            f"{len(completed_files)}/{total_files} file(s) in {m_s} seconds**\n"
        ]
        for i, (fname, size, _dest) in enumerate(completed_files, start=1):
            lines.append(
                f"**{i}. Name :** `{fname}`\n"
                f"   **Size :** `{humanbytes(size)}`"
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
        f"```\n[{bar}]({round(percentage, 2)}%)```\n"
        f"**File Name** : `{file_name}`\n"
        f"**File Size** : `{humanbytes(file_size)}`\n"
        f"**Uploaded** : `{humanbytes(uploaded)}`\n"
        f"**Completed** : `{completed}/{total_count}`\n"
        f"**Speed** : `{humanbytes(int(speed))}/s`\n"
        f"**ETA** : `{time_formatter(eta)}`"
    )


# -----------------------------------------------------------------------
#  ProgressPayload — from commit fa4dc5f (the version with correct naming)
#  This streams file bytes while calling the progress callback.
# -----------------------------------------------------------------------
class ProgressPayload(BufferedReaderPayload):
    """BufferedReaderPayload with upload progress callback."""

    def __init__(self, fp, *, callback, content_type, filename):
        super().__init__(fp, content_type=content_type, filename=filename)
        self._cb = callback

    async def write(self, writer):
        chunk_size = 1024 * 256
        while True:
            chunk = self._value.read(chunk_size)
            if not chunk:
                break
            await writer.write(chunk)
            self._cb(len(chunk))


# -----------------------------------------------------------------------
#  _upload_single_file — naming mechanism ported from fa4dc5f
#
#  fa4dc5f used MultipartWriter + filename_star (RFC 5987 encoding).
#  That is the only approach confirmed to produce correct filenames on
#  GoFile's servers.  folderId support has been added here via an extra
#  multipart field prepended before the file part.
#
#  DO NOT replace MultipartWriter with aiohttp.FormData here — FormData's
#  internal RFC 5987 encoding produces filenames that GoFile mis-parses
#  as percent-encoded strings (e.g. "Ingrid%20Goes%20West...").
# -----------------------------------------------------------------------
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
    Upload one file to GoFile.
    Returns result["data"] dict.
    Naming: uses MultipartWriter + RFC-5987 filename* (from fa4dc5f).
    folderId: prepended as a plain text multipart field when provided.
    """
    upload_url = f"https://{server}.gofile.io/contents/uploadfile"
    headers = {"Authorization": f"Bearer {GOFILE_TOKEN}"}

    filename = src.name
    filename_star = "UTF-8''" + urllib.parse.quote(filename, safe="")

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

    mp = MultipartWriter("form-data")

    # folderId MUST come before the file field
    if folder_id:
        fid_part = mp.append(folder_id)
        fid_part.set_content_disposition("form-data", name="folderId")

    with open(src, "rb") as f:
        part = mp.append(
            ProgressPayload(
                f,
                callback=on_bytes,
                content_type="application/octet-stream",
                filename=filename,
            )
        )
        part.set_content_disposition("form-data", name="file", filename=filename)
        part.headers["Content-Disposition"] += f"; filename*={filename_star}"

        async with session.post(upload_url, data=mp, headers=headers) as resp:
            result = await resp.json()

    if result.get("status") != "ok":
        raise RuntimeError(f"GoFile upload failed: {result}")

    return result["data"]


async def _progress_display_loop(
    message: Message,
    progress_queue: asyncio.Queue,
) -> tuple:
    """
    Drain the progress queue until a ("done",...) or ("error",...) sentinel.
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
    """Resolve glob/literal patterns, returning a deduplicated ordered file list."""
    found: List[Path] = []
    seen: set = set()

    for pat in patterns:
        pat = pat.strip()
        if not pat:
            continue

        # Try absolute / cwd glob first
        matches = sorted(_glob.glob(pat))
        if not matches:
            # Then relative to DOWN_PATH
            matches = sorted(_glob.glob(str(base_dir / pat)))

        if matches:
            for m in matches:
                rp = Path(m).resolve()
                if rp not in seen:
                    seen.add(rp)
                    found.append(Path(m))
        else:
            # Literal path
            for candidate in ([Path(pat)] if Path(pat).is_absolute()
                              else [Path(pat), base_dir / pat]):
                rp = candidate.resolve()
                if rp not in seen and (candidate.is_file() or candidate.is_dir()):
                    seen.add(rp)
                    found.append(candidate)
                    break

    return found


# ------------------------------------------------------------------
# Upload command  (.goup)
# ------------------------------------------------------------------

@userge.on_cmd("goup", about={
    'header': "Upload file(s) or a folder to GoFile",
    'usage': (
        "{tr}goup <file_or_folder>\n"
        "{tr}goup file1.mkv | file2.mkv | *.srt\n"
        "  (optional 2nd line: existing GoFile URL or folder ID/code)"
    ),
    'examples': [
        "{tr}goup video.mkv",
        "{tr}goup /app/downloads/Season1",
        "{tr}goup file1.mkv | *.srt",
        "{tr}goup *.mkv | *.srt\nhttps://gofile.io/d/AbCdEf",
        "{tr}goup file.mkv\nAbCdEf",
    ]})
async def goup_(message: Message):
    """ upload file(s) or folder to GoFile """
    if not GOFILE_TOKEN:
        await message.err(
            "Set `GOFILE_TOKEN` environment variable to enable GoFile uploads.")
        return

    raw = message.input_str.strip() if message.input_str else ""
    if not raw:
        await message.err("Provide a file path, folder path, or pipe-separated patterns.")
        return

    # Optional existing-folder on 2nd line
    existing_folder_id: Optional[str] = None
    if "\n" in raw:
        raw, second_line = raw.split("\n", 1)
        raw = raw.strip()
        second_line = second_line.strip()
        if second_line:
            if "gofile.io/d/" in second_line:
                existing_folder_id = second_line.rstrip("/").split("/d/")[-1]
            else:
                existing_folder_id = second_line

    base_dir = Path(config.Dynamic.DOWN_PATH)
    segments = [s.strip() for s in raw.split("|") if s.strip()]
    srcs = _resolve_glob_patterns(segments, base_dir)

    if not srcs:
        await message.err("No files matched the given pattern(s).")
        return

    # Single directory → folder upload
    if len(srcs) == 1 and Path(srcs[0]).is_dir():
        await _goup_folder(message, Path(srcs[0]), existing_folder_id)
        return

    file_srcs = [Path(p) for p in srcs if Path(p).is_file()]
    if not file_srcs:
        await message.err("No uploadable files found.")
        return

    if len(file_srcs) == 1 and existing_folder_id is None:
        await _goup_file(message, file_srcs[0], None)
    else:
        await _goup_multi(message, file_srcs, existing_folder_id)


# ------------------------------------------------------------------
# Single-file upload
# ------------------------------------------------------------------

async def _goup_file(
    message: Message,
    src: Path,
    existing_folder_id: Optional[str],
) -> None:
    display_name = src.name
    size = src.stat().st_size
    task_id = f"goup_{display_name}"

    await message.edit(
        f"`Starting GoFile upload: {display_name} ({humanbytes(size)})`"
    )

    if _STATUS_AVAILABLE:
        register_task(task_id, display_name, kind="upload")

    progress_queue: asyncio.Queue = asyncio.Queue()
    start_t = datetime.now()

    async def _run():
        try:
            async with aiohttp.ClientSession() as session:
                server = await _get_best_server(session)
                file_data = await _upload_single_file(
                    session, server, src, task_id, size, progress_queue,
                    folder_id=existing_folder_id,
                    completed_count=0, total_count=1,
                )
                folder_code = file_data.get("parentFolderCode") or ""
                parent_folder_id = file_data.get("parentFolder") or ""

                # Rename the auto-created GoFile folder to the filename
                # (skip if the user explicitly supplied an existing folder)
                if parent_folder_id and not existing_folder_id:
                    await _rename_gofile_content(
                        session, parent_folder_id, display_name
                    )

                if existing_folder_id:
                    link = f"https://gofile.io/d/{existing_folder_id}"
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
# Multi-file upload (pipe-separated patterns or single file + existing folder)
# ------------------------------------------------------------------

async def _goup_multi(
    message: Message,
    srcs: List[Path],
    existing_folder_id: Optional[str],
) -> None:
    """Upload a list of files into one GoFile folder."""
    total_count = len(srcs)
    folder_name = srcs[0].name   # GoFile folder named after first file
    total_size  = sum(s.stat().st_size for s in srcs)

    await message.edit(
        f"`Uploading {total_count} file(s) to GoFile "
        f"({humanbytes(total_size)} total)…`"
    )

    progress_queue: asyncio.Queue = asyncio.Queue()
    start_t = datetime.now()

    async def _run():
        try:
            async with aiohttp.ClientSession() as session:
                server = await _get_best_server(session)

                if existing_folder_id:
                    target_folder_id = existing_folder_id
                    folder_code      = existing_folder_id
                else:
                    target_folder_id = None
                    folder_code      = None

                for i, src in enumerate(srcs):
                    task_id = f"goup_{src.name}"
                    if _STATUS_AVAILABLE:
                        register_task(task_id, src.name, kind="upload")

                    file_data = await _upload_single_file(
                        session, server, src, task_id,
                        src.stat().st_size, progress_queue,
                        folder_id=target_folder_id,
                        completed_count=i,
                        total_count=total_count,
                    )

                    if _STATUS_AVAILABLE:
                        complete_task(task_id)

                    # After the FIRST upload, lock in the folder ID so all
                    # subsequent files land in the SAME GoFile folder.
                    if target_folder_id is None:
                        target_folder_id = file_data.get("parentFolder") or None
                        folder_code      = file_data.get("parentFolderCode") or None
                        if target_folder_id:
                            await _rename_gofile_content(
                                session, target_folder_id, folder_name
                            )

                link = (
                    f"https://gofile.io/d/{folder_code}"
                    if folder_code else
                    (f"https://gofile.io/d/{existing_folder_id}"
                     if existing_folder_id else "")
                )
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
        f"✅ **Uploaded {total_count} file(s) Successfully in {m_s} seconds**\n\n"
        f"**Folder Name** : `{folder_name}`\n"
        f"**Total Size** : `{humanbytes(total_size)}`\n"
        f"🔗 **Link** : {result_link}"
    )


# ------------------------------------------------------------------
# Folder upload
# ------------------------------------------------------------------

async def _goup_folder(
    message: Message,
    src_dir: Path,
    existing_folder_id: Optional[str],
) -> None:
    """Upload all files in a local folder flat into one GoFile folder."""
    all_files: List[Path] = []
    for root, _dirs, filenames in os.walk(src_dir):
        for fname in sorted(filenames):
            all_files.append(Path(root) / fname)

    if not all_files:
        await message.err(f"Folder `{src_dir.name}` is empty — nothing to upload.")
        return

    folder_name  = src_dir.name
    total_count  = len(all_files)
    total_bytes  = sum(f.stat().st_size for f in all_files)

    if _STATUS_AVAILABLE:
        register_task(f"goup_{folder_name}", folder_name, kind="upload")

    await message.edit(
        f"`📁 Uploading folder: {folder_name}`\n"
        f"`Files: {total_count}  |  Total: {humanbytes(total_bytes)}`"
    )

    progress_queue: asyncio.Queue = asyncio.Queue()
    start_t = datetime.now()

    async def _run():
        top_folder_id   = existing_folder_id
        top_folder_code = existing_folder_id  # share code or UUID if given
        top_folder_link = (
            f"https://gofile.io/d/{existing_folder_id}"
            if existing_folder_id else ""
        )

        try:
            async with aiohttp.ClientSession() as session:
                server = await _get_best_server(session)

                for idx, local_path in enumerate(all_files):
                    file_size = local_path.stat().st_size
                    task_id   = f"goup_{local_path.name}"

                    if _STATUS_AVAILABLE:
                        register_task(task_id, local_path.name, kind="upload")

                    file_data = await _upload_single_file(
                        session, server, local_path,
                        task_id, file_size, progress_queue,
                        folder_id=top_folder_id,
                        completed_count=idx,
                        total_count=total_count,
                    )

                    if _STATUS_AVAILABLE:
                        complete_task(task_id)

                    # Bootstrap the folder from the first successful upload
                    if not top_folder_id:
                        top_folder_id   = file_data.get("parentFolder") or ""
                        top_folder_code = file_data.get("parentFolderCode") or ""
                        top_folder_link = (
                            f"https://gofile.io/d/{top_folder_code}"
                            if top_folder_code else ""
                        )
                        if top_folder_id and not existing_folder_id:
                            await _rename_gofile_content(
                                session, top_folder_id, folder_name
                            )

            await progress_queue.put(("done", top_folder_link))
        except Exception as exc:  # pylint: disable=broad-except
            await progress_queue.put(("error", str(exc)))

    asyncio.ensure_future(_run())
    result_link, error_msg = await _progress_display_loop(message, progress_queue)
    m_s = (datetime.now() - start_t).seconds

    if _STATUS_AVAILABLE:
        complete_task(f"goup_{folder_name}")

    if error_msg:
        await message.err(error_msg)
        return

    await message.edit(
        f"✅ **Folder Uploaded Successfully in {m_s} seconds**\n\n"
        f"**Folder Name** : `{folder_name}`\n"
        f"**Files** : `{total_count}`\n"
        f"**Total Size** : `{humanbytes(total_bytes)}`\n"
        f"🔗 **Link** : {result_link}"
    )
