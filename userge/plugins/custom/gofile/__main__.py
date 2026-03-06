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

        # 1. Create guest account
        async with session.post(
            f"{_GOFILE_BASE}/accounts", headers=headers, json={}
        ) as resp:
            acc_data = await resp.json()

        if acc_data.get("status") != "ok":
            raise RuntimeError(f"GoFile account creation failed: {acc_data}")

        token = acc_data["data"]["token"]

        # 2. Add 2026 required auth headers
        headers["Authorization"] = f"Bearer {token}"
        headers["Cookie"] = f"accountToken={token}"
        headers["X-Website-Token"] = _generate_x_website_token(token)
        headers["X-bl"] = _GOFILE_LANG

        # 3. Fetch content metadata
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

    # Extract content ID from full URL
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

    # Queue all files into aria2 — NO `out` option, let aria2 use
    # the server's Content-Disposition header for the real filename,
    # exactly like your working gofile_2026.py does with plain aria2c.
    queued = []  # list of (gid, api_filename, size_bytes)
    for fname, fdata in files.items():
        link = fdata.get("link") or fdata.get("directLink")
        if not link:
            LOGS.warning("No link for file %s, skipping.", fname)
            continue

        size = fdata.get("size", 0)

        options = {
            "dir": dl_dir,
            # NO "out" — let aria2 pick up the real name from
            # Content-Disposition, same as bare aria2c -s14 -x14
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

    # Track each download sequentially — show live aria-style progress
    for idx, (gid, api_fname, size) in enumerate(queued, start=1):
        await message.edit(
            f"`[{idx}/{total_files}] Starting: {api_fname} ({humanbytes(size)}) …`"
        )
        result = await _godl_progress(gid, message, api_fname, idx, total_files)
        if result:
            completed_files.append(result)

    # Final summary — ALL downloaded files
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
    """
    Poll aria2 and display aria-style progress for a GoFile download.
    Returns (real_name, size, dest_path) tuple on success, None on failure.
    """
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

        # Use whatever name aria2 has resolved so far; fall back to the API name
        resolved_name = t_file.name if t_file.name else api_name

        if t_file.is_complete:
            if _STATUS_AVAILABLE:
                complete_task(gid)
            dest = os.path.join(t_file.dir, t_file.name or api_name)
            final_size = t_file.total_length or 0
            return (resolved_name, final_size, dest)

        # ---- build progress message ----
        percentage = int(t_file.progress)
        downloaded = percentage * int(t_file.total_length) / 100 if t_file.total_length else 0

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
# Upload helpers — shared between single-file and folder upload
# ------------------------------------------------------------------

async def _get_best_server(session: aiohttp.ClientSession) -> str:
    """Return the hostname of the best GoFile upload server."""
    async with session.get(f"{_GOFILE_BASE}/servers") as resp:
        data = await resp.json()
    return data["data"]["servers"][0]["name"]


async def _create_gofile_folder(
    session: aiohttp.ClientSession,
    folder_name: str,
    parent_folder_id: str,
) -> str:
    """Create a GoFile folder under parent_folder_id and return its folderId."""
    headers = {"Authorization": f"Bearer {GOFILE_TOKEN}"}
    payload = {
        "parentFolderId": parent_folder_id,
        "folderName": folder_name,
    }
    async with session.put(
        f"{_GOFILE_BASE}/contents/createFolder",
        headers=headers,
        json=payload,
    ) as resp:
        data = await resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"GoFile folder creation failed: {data}")
    return data["data"]["folderId"]


async def _get_root_folder_id(session: aiohttp.ClientSession) -> str:
    """Return the authenticated user's root folder ID."""
    headers = {"Authorization": f"Bearer {GOFILE_TOKEN}"}
    async with session.get(f"{_GOFILE_BASE}/accounts/me", headers=headers) as resp:
        data = await resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"GoFile account info failed: {data}")
    return data["data"]["rootFolder"]


def _make_progress_str(file_name: str,
                       file_size: int,
                       uploaded: int,
                       speed: float,
                       eta: int,
                       completed: int = 0,
                       total_count: int = 1) -> str:
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
) -> str:
    """
    Upload one file to GoFile.  Returns the share/folder link.
    Pushes ("progress", str) tuples onto progress_queue while uploading.
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

        # If uploading into a specific folder, tell GoFile via form field
        if folder_id:
            mp.append(folder_id).set_content_disposition(
                "form-data", name="folderId"
            )

        async with session.post(upload_url, data=mp, headers=headers) as resp:
            result = await resp.json()

    if result.get("status") != "ok":
        raise RuntimeError(f"GoFile upload failed: {result}")

    file_data = result["data"]
    if file_data.get("parentFolderCode"):
        return f"https://gofile.io/d/{file_data['parentFolderCode']}"
    return file_data.get("downloadPage", "")


# ------------------------------------------------------------------
# Upload command  (.goup)  — single file OR entire folder
# ------------------------------------------------------------------

@userge.on_cmd("goup", about={
    'header': "Upload a file or folder to GoFile",
    'usage': "{tr}goup <file_path_or_folder_path>",
    'examples': ["{tr}goup /app/downloads/video.mkv",
                 "{tr}goup video.mkv",
                 "{tr}goup /app/downloads/my_folder",
                 "{tr}goup my_folder"]})
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

    # Resolve relative paths against the download directory
    if not src.exists():
        src = Path(config.Dynamic.DOWN_PATH) / path_str
    if not src.exists():
        await message.err(f"Path not found: `{path_str}`")
        return

    if src.is_dir():
        await _goup_folder(message, src)
    else:
        await _goup_file(message, src)


async def _goup_file(message: Message, src: Path, folder_id: Optional[str] = None,
                     completed_count: int = 0, total_count: int = 1,
                     progress_queue: Optional[asyncio.Queue] = None,
                     session: Optional[aiohttp.ClientSession] = None,
                     server: Optional[str] = None) -> Optional[str]:
    """
    Upload a single file.  When called standalone (no session/server provided)
    it manages the full lifecycle including progress display and final message.
    When called from _goup_folder it uses the shared session/server and returns
    the link without touching the message.
    """
    standalone = session is None

    display_name = src.name
    size = src.stat().st_size
    task_id = f"goup_{display_name}"

    if standalone:
        await message.edit(
            f"`Starting GoFile upload: {display_name} ({humanbytes(size)})`"
        )
        if _STATUS_AVAILABLE:
            register_task(task_id, display_name, kind="upload")

        progress_queue = asyncio.Queue()

        async def _run():
            async with aiohttp.ClientSession() as _sess:
                _server = await _get_best_server(_sess)
                try:
                    link = await _upload_single_file(
                        _sess, _server, src, task_id, size,
                        progress_queue, folder_id=folder_id,
                        completed_count=completed_count, total_count=total_count,
                    )
                    await progress_queue.put(("done", link))
                except Exception as exc:  # pylint: disable=broad-except
                    await progress_queue.put(("error", str(exc)))

        asyncio.ensure_future(_run())
        return await _consume_progress_queue(message, task_id, display_name, size, progress_queue)

    else:
        # Called from folder upload — session/server already provided
        try:
            link = await _upload_single_file(
                session, server, src, task_id, size,
                progress_queue, folder_id=folder_id,
                completed_count=completed_count, total_count=total_count,
            )
            return link
        except Exception as exc:  # pylint: disable=broad-except
            raise exc


async def _consume_progress_queue(
    message: Message,
    task_id: str,
    display_name: str,
    size: int,
    progress_queue: asyncio.Queue,
) -> Optional[str]:
    """Drive the message-editing loop for a single-file upload."""
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
            return payload
        elif kind == "error":
            if _STATUS_AVAILABLE:
                remove_task(task_id)
            await message.err(payload)
            return None
        elif kind == "progress":
            last_progress = payload
            await message.edit(payload)


async def _goup_folder(message: Message, src_dir: Path) -> None:
    """
    Recursively upload a local folder to GoFile, mirroring the directory
    structure as GoFile sub-folders.

    Strategy
    --------
    1. Get the authenticated user's root folder ID.
    2. Create a top-level GoFile folder named after src_dir.
    3. Walk the local tree; for every sub-directory create a matching
       GoFile folder; for every file upload it into its parent GoFile folder.
    4. Show live per-file progress and a final summary.
    """
    folder_name = src_dir.name

    # Collect all files first so we know the total count and size
    all_files: list[tuple[Path, Path]] = []  # (local_path, relative_path_from_src_dir)
    for root, _dirs, filenames in os.walk(src_dir):
        for fname in filenames:
            local = Path(root) / fname
            rel = local.relative_to(src_dir)
            all_files.append((local, rel))

    if not all_files:
        await message.err(f"Folder `{folder_name}` is empty — nothing to upload.")
        return

    total_count = len(all_files)
    total_bytes = sum(f.stat().st_size for f, _ in all_files)

    await message.edit(
        f"`📁 Uploading folder: {folder_name}`\n"
        f"`Files: {total_count}  |  Total size: {humanbytes(total_bytes)}`"
    )

    if _STATUS_AVAILABLE:
        register_task(f"goup_{folder_name}", folder_name, kind="upload")

    async with aiohttp.ClientSession() as session:
        try:
            server = await _get_best_server(session)
            root_folder_id = await _get_root_folder_id(session)

            # Create the top-level folder on GoFile
            top_folder_id = await _create_gofile_folder(
                session, folder_name, root_folder_id
            )
            top_folder_link = f"https://gofile.io/d/{top_folder_id}"

            # Cache: relative dir path (as string) -> GoFile folderId
            # "" maps to the top-level folder we just created
            folder_id_map: dict[str, str] = {"": top_folder_id}

            uploaded_count = 0
            uploaded_bytes = 0
            failed: list[str] = []

            progress_queue: asyncio.Queue = asyncio.Queue()

            for local_path, rel_path in all_files:
                file_size = local_path.stat().st_size

                # Ensure all ancestor GoFile folders exist
                parent_rel = str(rel_path.parent) if str(rel_path.parent) != "." else ""
                if parent_rel not in folder_id_map:
                    # Build the chain from root down
                    parts = Path(parent_rel).parts
                    current_rel = ""
                    for part in parts:
                        child_rel = str(Path(current_rel) / part) if current_rel else part
                        if child_rel not in folder_id_map:
                            parent_id = folder_id_map[current_rel]
                            new_fid = await _create_gofile_folder(
                                session, part, parent_id
                            )
                            folder_id_map[child_rel] = new_fid
                        current_rel = child_rel

                parent_folder_id = folder_id_map[parent_rel]
                task_id = f"goup_{local_path.name}"

                if _STATUS_AVAILABLE:
                    register_task(task_id, local_path.name, kind="upload")

                # Drain the queue before starting the next file so the caller's
                # progress display loop sees the latest message
                while not progress_queue.empty():
                    try:
                        item = progress_queue.get_nowait()
                        if item[0] == "progress":
                            await message.edit(item[1])
                    except asyncio.QueueEmpty:
                        break

                await message.edit(
                    f"`[{uploaded_count + 1}/{total_count}] Uploading: "
                    f"{local_path.name} ({humanbytes(file_size)})`\n"
                    f"`Total progress: {humanbytes(uploaded_bytes)}/{humanbytes(total_bytes)}`"
                )

                try:
                    await _upload_single_file(
                        session, server, local_path,
                        task_id, file_size, progress_queue,
                        folder_id=parent_folder_id,
                        completed_count=uploaded_count,
                        total_count=total_count,
                    )
                    # Drain progress updates from this file
                    while not progress_queue.empty():
                        try:
                            item = progress_queue.get_nowait()
                            if item[0] == "progress":
                                await message.edit(item[1])
                        except asyncio.QueueEmpty:
                            break

                    if _STATUS_AVAILABLE:
                        complete_task(task_id)
                    uploaded_count += 1
                    uploaded_bytes += file_size

                except Exception as exc:  # pylint: disable=broad-except
                    LOGS.exception("Failed to upload %s: %s", local_path, exc)
                    if _STATUS_AVAILABLE:
                        remove_task(task_id)
                    failed.append(str(rel_path))

        except Exception as exc:  # pylint: disable=broad-except
            if _STATUS_AVAILABLE:
                remove_task(f"goup_{folder_name}")
            await message.err(str(exc))
            return

    if _STATUS_AVAILABLE:
        complete_task(f"goup_{folder_name}")

    # Final summary
    lines = [
        f"✅ **Folder Uploaded Successfully**\n",
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
