""" GoFile download and upload plugin """

import asyncio
import hashlib
import math
import os
import time
import urllib.parse
from pathlib import Path

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
# Both plugins share the same daemon on localhost:6800 — no second instance needed.
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
    'header': "Download a file from GoFile via aria2",
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

    for fname, fdata in files.items():
        link = fdata.get("link") or fdata.get("directLink")
        if not link:
            LOGS.warning("No link for file %s, skipping.", fname)
            continue

        await message.edit(f"`Queuing GoFile download: {fname} …`")

        options = {
            "dir": dl_dir,
            "out": fname,
            "header": [f"Cookie: accountToken={token}"],
            "max-connection-per-server": "14",
            "split": "14",
            "allow-overwrite": "true",
        }

        try:
            download = _aria2.add_uris([link], options=options)
        except Exception as e:  # pylint: disable=broad-except
            await message.err(f"Failed to queue download: {e}")
            return

        gid = download.gid
        await message.edit("`Processing…`")
        await _godl_progress(gid, message, fname)


async def _godl_progress(gid: str, message: Message, display_name: str) -> None:
    """Poll aria2 and display aria-style progress for a GoFile download."""
    if _STATUS_AVAILABLE:
        register_task(gid, display_name, kind="download")

    previous = ""

    while True:
        await asyncio.sleep(config.Dynamic.EDIT_SLEEP_TIMEOUT)

        try:
            t_file = _aria2.get_download(gid)
        except Exception:  # pylint: disable=broad-except
            if _STATUS_AVAILABLE:
                remove_task(gid)
            await message.edit("Download cancelled by user ...")
            return

        if t_file.error_message:
            if _STATUS_AVAILABLE:
                remove_task(gid)
            await message.err(str(t_file.error_message))
            return

        if t_file.is_complete:
            if _STATUS_AVAILABLE:
                complete_task(gid)
            dest = os.path.join(t_file.dir, t_file.name)
            await message.edit(
                f"✅ **Downloaded Successfully**\n\n"
                f"**Name :** `{t_file.name}`\n"
                f"**Size :** `{t_file.total_length_string()}`\n"
                f"**Path :** `{dest}`\n"
                f"**Response :** __Successfully downloaded...__"
            )
            return

        # Build aria-style progress message
        percentage = int(t_file.progress)
        downloaded = percentage * int(t_file.total_length) / 100

        if _STATUS_AVAILABLE:
            update_task(
                gid,
                name=t_file.name or display_name,
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

        msg = (
            f"`{prog_str}`\n"
            f"**Name**: `{t_file.name or display_name}`\n"
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


# ------------------------------------------------------------------
# Upload command  (.goup)
# ------------------------------------------------------------------

@userge.on_cmd("goup", about={
    'header': "Upload a file to GoFile",
    'usage': "{tr}goup <file_path>",
    'examples': ["{tr}goup /app/downloads/video.mkv",
                 "{tr}goup video.mkv"]})
async def goup_(message: Message):
    """ upload a local file to GoFile """
    if not GOFILE_TOKEN:
        await message.err(
            "Set `GOFILE_TOKEN` environment variable to enable GoFile uploads.")
        return

    path_str = message.input_str.strip() if message.input_str else ""
    if not path_str:
        await message.err("Provide a file path.")
        return

    src = Path(path_str)
    if not src.is_file():
        src = Path(config.Dynamic.DOWN_PATH) / path_str
    if not src.is_file():
        await message.err(f"File not found: `{path_str}`")
        return

    display_name = src.name
    size = src.stat().st_size
    task_id = f"goup_{display_name}"

    await message.edit(f"`Starting GoFile upload: {display_name} ({humanbytes(size)})`")

    if _STATUS_AVAILABLE:
        register_task(task_id, display_name, kind="upload")

    progress_queue: asyncio.Queue = asyncio.Queue()

    async def _run_upload():
        try:
            link = await _upload_to_gofile(src, task_id, size, progress_queue)
            await progress_queue.put(("done", link))
        except Exception as exc:  # pylint: disable=broad-except
            await progress_queue.put(("error", str(exc)))

    asyncio.ensure_future(_run_upload())

    last_progress = None
    result_link = None
    error_msg = None

    while True:
        try:
            item = await asyncio.wait_for(
                progress_queue.get(),
                timeout=config.Dynamic.EDIT_SLEEP_TIMEOUT
            )
        except asyncio.TimeoutError:
            if last_progress:
                await message.edit(last_progress)
            continue

        kind, payload = item

        if kind == "done":
            result_link = payload
            break
        elif kind == "error":
            error_msg = payload
            break
        elif kind == "progress":
            last_progress = payload
            await message.edit(payload)

    if error_msg:
        if _STATUS_AVAILABLE:
            remove_task(task_id)
        await message.err(error_msg)
        return

    if _STATUS_AVAILABLE:
        complete_task(task_id)

    await message.edit(
        f"✅ **Uploaded Successfully**\n\n"
        f"**File Name** : `{display_name}`\n"
        f"**File Size** : `{humanbytes(size)}`\n"
        f"🔗 **Link** : {result_link}"
    )


def _make_progress_str(file_name: str,
                       file_size: int,
                       uploaded: int,
                       speed: float,
                       eta: int) -> str:
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
        f"**Completed** : `0/1`\n"
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


async def _upload_to_gofile(src: Path,
                             task_id: str,
                             total_size: int,
                             progress_queue: asyncio.Queue) -> str:
    """Upload file to GoFile, push progress to queue, and return share link."""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{_GOFILE_BASE}/servers") as resp:
            data = await resp.json()

        server = data["data"]["servers"][0]["name"]
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

            async with session.post(upload_url, data=mp, headers=headers) as resp:
                result = await resp.json()

    if result.get("status") != "ok":
        raise RuntimeError(f"GoFile upload failed: {result}")

    file_data = result["data"]
    if file_data.get("parentFolderCode"):
        return f"https://gofile.io/d/{file_data['parentFolderCode']}"
    return file_data.get("downloadPage", "")
