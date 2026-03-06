""" GoFile download and upload plugin """

import asyncio
import hashlib
import math
import os
import time
import urllib.parse
from pathlib import Path
from typing import Optional

import aiohttp
import aria2p
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

# Connect to the aria2c RPC daemon already started by the aria plugin.
_aria2 = aria2p.API(
    aria2p.Client(host="http://localhost", port=6800, secret="")
)

# ------------------------------------------------------------------
# GoFile 2026 auth helpers
# ------------------------------------------------------------------

_GOFILE_UA = "Mozilla/5.0"
_GOFILE_LANG = "en-US"
_GOFILE_STATIC_SECRET = "gf2026x"


def _generate_x_website_token(bearer_token: str) -> str:
    """Compute the GoFile X-Website-Token (SHA-256, 4-hour bucket)."""
    time_bucket = str(math.floor(int(time.time()) / 14400))
    raw = (
        f"{_GOFILE_UA}::{_GOFILE_LANG}::{bearer_token}"
        f"::{time_bucket}::{_GOFILE_STATIC_SECRET}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _gofile_get_download_info(content_id: str) -> tuple:
    """
    Returns (account_token, files_dict) where files_dict maps
    child-key -> child-data for all file-type children (or the
    single file if the content IS a file).
    """
    async with aiohttp.ClientSession() as session:
        headers = {
            "User-Agent": _GOFILE_UA,
            "Origin": "https://gofile.io",
        }

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
            download = _aria2.add_uris([link], options=options)
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

    for idx, (gid, api_fname, size) in enumerate(queued, start=1):
        await message.edit(
            f"`[{idx}/{total_files}] Starting: {api_fname} ({humanbytes(size)}) …`"
        )
        result = await _godl_progress(gid, message, api_fname, idx, total_files)
        if result:
            completed_files.append(result)

    if completed_files:
        lines = [f"✅ **Downloaded {len(completed_files)}/{total_files} file(s)**\n"]
        for i, (fname, size, dest) in enumerate(completed_files, start=1):
            lines.append(
                f"**{i}. Name :** `{fname}`\n"
                f"   **Size :** `{humanbytes(size)}`\n"
                f"   **Path :** `{dest}`"
            )
        await message.edit("\n\n".join(lines))


async def _godl_progress(
    gid: str,
    message: Message,
    api_name: str,
    file_index: int = 1,
    total_files: int = 1,
):
    """Poll aria2 and display aria-style progress for a GoFile download."""
    if _STATUS_AVAILABLE:
        register_task(gid, api_name, kind="download")

    previous = ""

    while True:
        try:
            t_file = _aria2.get_download(gid)
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
    """Return the hostname of the best GoFile upload server."""
    async with session.get(f"{_GOFILE_BASE}/servers") as resp:
        data = await resp.json()
    return data["data"]["servers"][0]["name"]


async def _rename_gofile_content(
    session: aiohttp.ClientSession,
    content_id: str,
    new_name: str,
) -> None:
    """Rename a GoFile file or folder via PUT /contents/{id}/update."""
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
    """Build a GDrive-style progress string for GoFile uploads."""
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
    - If folder_id is None  → GoFile auto-creates a new folder.
    - If folder_id is given → file goes into that existing folder.
    Returns result["data"] dict (contains parentFolderId, parentFolderCode, etc.).
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

        if folder_id:
            fid_part = mp.append(folder_id)
            fid_part.set_content_disposition("form-data", name="folderId")

        async with session.post(upload_url, data=mp, headers=headers) as resp:
            result = await resp.json()

    if result.get("status") != "ok":
        raise RuntimeError(f"GoFile upload failed: {result}")

    return result["data"]


# ------------------------------------------------------------------
# Upload command  (.goup)  — single file OR entire folder
# ------------------------------------------------------------------

@userge.on_cmd("goup", about={
    'header': "Upload a file or folder to GoFile",
    'usage': "{tr}goup <file_or_folder_path>",
    'examples': [
        "{tr}goup /app/downloads/video.mkv",
        "{tr}goup video.mkv",
        "{tr}goup /app/downloads/Season1",
        "{tr}goup Season1",
    ]})
async def goup_(message: Message):
    """ upload a local file or folder to GoFile """
    if not GOFILE_TOKEN:
        await message.err(
            "Set `GOFILE_TOKEN` environment variable to enable GoFile uploads.")
        return

    path_str = message.input_str.strip() if message.input_str else ""
    if not path_str:
        await message.err("Provide a file or folder path.")
        return

    src = Path(path_str)
    if not src.exists():
        src = Path(config.Dynamic.DOWN_PATH) / path_str
    if not src.exists():
        await message.err(f"Path not found: `{path_str}`")
        return

    if src.is_dir():
        await _goup_folder(message, src)
    else:
        await _goup_file(message, src)


# ------------------------------------------------------------------
# Single-file upload
# ------------------------------------------------------------------

async def _goup_file(message: Message, src: Path) -> None:
    """
    Upload a single file to GoFile.
    GoFile auto-creates a folder; we then rename that folder to the filename.
    """
    display_name = src.name
    size = src.stat().st_size
    task_id = f"goup_{display_name}"

    await message.edit(
        f"`Starting GoFile upload: {display_name} ({humanbytes(size)})`"
    )
    if _STATUS_AVAILABLE:
        register_task(task_id, display_name, kind="upload")

    progress_queue: asyncio.Queue = asyncio.Queue()

    async def _run():
        async with aiohttp.ClientSession() as session:
            try:
                server = await _get_best_server(session)
                # Upload with no folder_id → GoFile auto-creates a folder
                file_data = await _upload_single_file(
                    session, server, src, task_id, size, progress_queue,
                    folder_id=None,
                    completed_count=0,
                    total_count=1,
                )
                folder_code = file_data.get("parentFolderCode", "")
                folder_id   = file_data.get("parentFolder", "")

                # Rename the auto-created folder to the filename
                if folder_id:
                    await _rename_gofile_content(session, folder_id, display_name)

                link = (
                    f"https://gofile.io/d/{folder_code}"
                    if folder_code
                    else file_data.get("downloadPage", "")
                )
                await progress_queue.put(("done", link))
            except Exception as exc:  # pylint: disable=broad-except
                await progress_queue.put(("error", str(exc)))

    asyncio.ensure_future(_run())

    last_progress = None
    while True:
        try:
            item = await asyncio.wait_for(
                progress_queue.get(),
                timeout=config.Dynamic.EDIT_SLEEP_TIMEOUT,
            )
        except asyncio.TimeoutError:
            if last_progress:
                await message.edit(last_progress)
            continue

        kind, payload = item
        if kind == "done":
            if _STATUS_AVAILABLE:
                complete_task(task_id)
            await message.edit(
                f"✅ **Uploaded Successfully**\n\n"
                f"**File Name** : `{display_name}`\n"
                f"**File Size** : `{humanbytes(size)}`\n"
                f"🔗 **Link** : {payload}"
            )
            return
        elif kind == "error":
            if _STATUS_AVAILABLE:
                remove_task(task_id)
            await message.err(payload)
            return
        elif kind == "progress":
            last_progress = payload
            await message.edit(payload)


# ------------------------------------------------------------------
# Folder upload
# ------------------------------------------------------------------

async def _goup_folder(message: Message, src_dir: Path) -> None:
    """
    Upload a local folder to GoFile, mirroring the directory structure.

    Strategy — no folder-creation API calls needed
    -----------------------------------------------
    GoFile does NOT support creating a folder without a parentFolderId,
    and the /accounts/me endpoint does not work with upload tokens.

    Instead we bootstrap the entire tree by uploading the FIRST file with
    NO folderId. GoFile auto-creates a folder and returns its parentFolderId
    and parentFolderCode. We rename that folder to the local folder name,
    then use parentFolderId as the root for all remaining uploads.

    For sub-directories we upload the first file of each sub-dir into the
    parent folder (no folderId restriction enforced by GoFile for sub-folders
    created implicitly) — actually GoFile's v2 API doesn't support nested
    folders via the upload endpoint, so ALL files go into the single top-level
    folder. This matches what gofile.io shows for multi-file uploads.
    """
    folder_name = src_dir.name

    # Collect all files (flattened — GoFile upload API creates one flat folder)
    all_files: list[Path] = []
    for root, _dirs, filenames in os.walk(src_dir):
        for fname in sorted(filenames):
            all_files.append(Path(root) / fname)

    if not all_files:
        await message.err(f"Folder `{folder_name}` is empty — nothing to upload.")
        return

    total_count = len(all_files)
    total_bytes = sum(f.stat().st_size for f in all_files)

    await message.edit(
        f"`📁 Preparing: {folder_name}`\n"
        f"`Files: {total_count}  |  Total: {humanbytes(total_bytes)}`"
    )

    if _STATUS_AVAILABLE:
        register_task(f"goup_{folder_name}", folder_name, kind="upload")

    top_folder_link = ""
    top_folder_id   = ""
    uploaded_count  = 0
    uploaded_bytes  = 0
    failed: list[str] = []

    try:
        async with aiohttp.ClientSession() as session:
            server = await _get_best_server(session)
            progress_queue: asyncio.Queue = asyncio.Queue()

            for idx, local_path in enumerate(all_files):
                file_size = local_path.stat().st_size
                task_id   = f"goup_{local_path.name}"

                if _STATUS_AVAILABLE:
                    register_task(task_id, local_path.name, kind="upload")

                await message.edit(
                    f"`[{idx + 1}/{total_count}] "
                    f"{local_path.name} ({humanbytes(file_size)})`\n"
                    f"`Done: {humanbytes(uploaded_bytes)} / {humanbytes(total_bytes)}`"
                )

                try:
                    # First file → no folderId, GoFile creates the folder
                    # Subsequent files → pass the folderId from the first upload
                    file_data = await _upload_single_file(
                        session, server, local_path,
                        task_id, file_size, progress_queue,
                        folder_id=top_folder_id if top_folder_id else None,
                        completed_count=uploaded_count,
                        total_count=total_count,
                    )

                    # Drain progress messages
                    while not progress_queue.empty():
                        try:
                            item = progress_queue.get_nowait()
                            if item[0] == "progress":
                                await message.edit(item[1])
                        except asyncio.QueueEmpty:
                            break

                    # Bootstrap: grab folder info from the first successful upload
                    if not top_folder_id:
                        top_folder_id   = file_data.get("parentFolder", "")
                        folder_code     = file_data.get("parentFolderCode", "")
                        top_folder_link = f"https://gofile.io/d/{folder_code}"
                        # Rename the auto-created folder to the local folder name
                        if top_folder_id:
                            await _rename_gofile_content(
                                session, top_folder_id, folder_name
                            )

                    if _STATUS_AVAILABLE:
                        complete_task(task_id)
                    uploaded_count += 1
                    uploaded_bytes += file_size

                except Exception as exc:  # pylint: disable=broad-except
                    LOGS.exception("Failed to upload %s: %s", local_path, exc)
                    if _STATUS_AVAILABLE:
                        remove_task(task_id)
                    failed.append(local_path.name)

    except Exception as exc:  # pylint: disable=broad-except
        if _STATUS_AVAILABLE:
            remove_task(f"goup_{folder_name}")
        await message.err(str(exc))
        return

    if _STATUS_AVAILABLE:
        complete_task(f"goup_{folder_name}")

    lines = [
        "✅ **Folder Uploaded Successfully**\n",
        f"**Folder Name** : `{folder_name}`\n"
        f"**Files Uploaded** : `{uploaded_count}/{total_count}`\n"
        f"**Total Size** : `{humanbytes(uploaded_bytes)}`\n"
        f"🔗 **Link** : {top_folder_link}",
    ]
    if failed:
        lines.append(
            f"\n⚠️ **Failed ({len(failed)}):**\n"
            + "\n".join(f"  • `{f}`" for f in failed)
        )
    await message.edit("\n".join(lines))
