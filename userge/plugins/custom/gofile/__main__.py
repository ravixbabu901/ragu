""" GoFile download and upload plugin """

import asyncio
import hashlib
import os
import time
from pathlib import Path

import aiohttp

from userge import userge, Message, config
from userge.utils import humanbytes

try:
    from userge.plugins.custom.status import (
        register_task, update_task, complete_task, remove_task)
    _STATUS_AVAILABLE = True
except Exception:  # pylint: disable=broad-except
    _STATUS_AVAILABLE = False

LOGS = userge.getLogger(__name__)

GOFILE_TOKEN = os.environ.get("GOFILE_TOKEN", "")
_GOFILE_BASE = "https://api.gofile.io"
_GOFILE_STATIC_SECRET = "gf2026x"
_GOFILE_TIME_BUCKET_SECONDS = 3600  # token rotates every hour


def _generate_x_website_token() -> str:
    """Generate the X-Website-Token using current time bucket and static secret."""
    time_bucket = str(int(time.time() / _GOFILE_TIME_BUCKET_SECONDS))
    return hashlib.sha256((time_bucket + _GOFILE_STATIC_SECRET).encode()).hexdigest()


async def _get_account_token(session: aiohttp.ClientSession,
                              x_website_token: str) -> str:
    """Create a guest GoFile account and return the account token."""
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://gofile.io",
        "X-Website-Token": x_website_token,
    }
    async with session.post(f"{_GOFILE_BASE}/accounts", headers=headers) as resp:
        data = await resp.json()
    if data.get("status") == "ok":
        return data["data"]["token"]
    raise RuntimeError(f"GoFile account creation failed: {data}")


async def _get_content(session: aiohttp.ClientSession,
                        content_id: str,
                        account_token: str,
                        x_website_token: str) -> dict:
    """Fetch content metadata for a given content ID."""
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://gofile.io",
        "Authorization": f"Bearer {account_token}",
        "X-Website-Token": x_website_token,
        "X-bl": "true",
    }
    url = f"{_GOFILE_BASE}/contents/{content_id}?wt={x_website_token}"
    async with session.get(url, headers=headers) as resp:
        data = await resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"GoFile content fetch failed: {data}")
    return data["data"]


@userge.on_cmd("godl", about={
    'header': "Download a file from GoFile",
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

    # Extract content ID from URL if full URL given
    if "gofile.io/d/" in inp:
        content_id = inp.rstrip("/").split("/d/")[-1]
    else:
        content_id = inp

    await message.edit(f"`Fetching GoFile content: {content_id} …`")

    try:
        async with aiohttp.ClientSession() as session:
            # 1. Generate X-Website-Token from time bucket
            x_website_token = _generate_x_website_token()

            # 2. Create guest account and get account token
            account_token = await _get_account_token(session, x_website_token)

            # 3. Fetch content metadata
            content = await _get_content(session, content_id, account_token, x_website_token)

            # 4. Collect files
            if content.get("type") == "file":
                files = {content["name"]: content}
            else:
                files = content.get("children", {})
                # Filter to only file-type children
                files = {k: v for k, v in files.items()
                         if v.get("type") == "file"}

            if not files:
                await message.err("No downloadable files found.")
                return

            dl_dir = Path(config.Dynamic.DOWN_PATH)
            dl_dir.mkdir(parents=True, exist_ok=True)

            dl_headers = {
                "User-Agent": "Mozilla/5.0",
                "Origin": "https://gofile.io",
                "Authorization": f"Bearer {account_token}",
                "X-Website-Token": x_website_token,
                "X-bl": "true",
                "Cookie": f"accountToken={account_token}",
            }

            for fname, fdata in files.items():
                link = fdata.get("link") or fdata.get("directLink")
                if not link:
                    continue
                size = fdata.get("size", 0)
                dest = dl_dir / fname
                task_id = f"godl_{fname}"

                await message.edit(f"`Downloading: {fname} ({humanbytes(size)})`")

                if _STATUS_AVAILABLE:
                    register_task(task_id, fname, kind="download")

                await _download_file(session, link, dest, task_id, size, dl_headers)

                if _STATUS_AVAILABLE:
                    complete_task(task_id)

                await message.edit(
                    f"✅ Downloaded: `{fname}`\n"
                    f"📦 Size: `{humanbytes(size)}`\n"
                    f"📂 Path: `{dest}`"
                )

    except Exception as e:  # pylint: disable=broad-except
        await message.err(str(e))


async def _download_file(session: aiohttp.ClientSession,
                         url: str,
                         dest: Path,
                         task_id: str,
                         total_size: int,
                         headers: dict) -> None:
    chunk_size = 1024 * 256  # 256 KB
    downloaded = 0
    start = time.time()

    async with session.get(url, headers=headers) as resp:
        resp.raise_for_status()
        if total_size == 0:
            total_size = int(resp.headers.get("Content-Length", 0))
        with open(dest, "wb") as f:
            async for chunk in resp.content.iter_chunked(chunk_size):
                f.write(chunk)
                downloaded += len(chunk)
                elapsed = time.time() - start or 0.001
                speed = int(downloaded / elapsed)
                if _STATUS_AVAILABLE:
                    update_task(task_id,
                                speed=speed,
                                done=downloaded,
                                total=total_size)


@userge.on_cmd("goup", about={
    'header': "Upload a file to GoFile",
    'usage': "{tr}goup <file_path>",
    'examples': ["{tr}goup /app/downloads/video.mkv"]})
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
        # Try relative to downloads
        src = Path(config.Dynamic.DOWN_PATH) / path_str
    if not src.is_file():
        await message.err(f"File not found: `{path_str}`")
        return

    size = src.stat().st_size
    task_id = f"goup_{src.name}"
    await message.edit(f"`Uploading to GoFile: {src.name} ({humanbytes(size)})`")

    if _STATUS_AVAILABLE:
        register_task(task_id, src.name, kind="upload")

    try:
        link = await _upload_to_gofile(src, task_id, size)
        if _STATUS_AVAILABLE:
            complete_task(task_id)
        await message.edit(
            f"✅ Uploaded: `{src.name}`\n"
            f"🔗 Link: {link}"
        )
    except Exception as e:  # pylint: disable=broad-except
        if _STATUS_AVAILABLE:
            remove_task(task_id)
        await message.err(str(e))


async def _upload_to_gofile(src: Path, task_id: str, total_size: int) -> str:
    """Upload file to GoFile and return share link."""
    # Get best server
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{_GOFILE_BASE}/servers") as resp:
            data = await resp.json()
        server = data["data"]["servers"][0]["name"]
        upload_url = f"https://{server}.gofile.io/contents/uploadfile"

        headers = {"Authorization": f"Bearer {GOFILE_TOKEN}"}
        start = time.time()
        uploaded_bytes = 0

        async def _file_generator():
            nonlocal uploaded_bytes
            with open(src, "rb") as f:
                while True:
                    chunk = f.read(1024 * 256)
                    if not chunk:
                        break
                    uploaded_bytes += len(chunk)
                    elapsed = time.time() - start or 0.001
                    speed = int(uploaded_bytes / elapsed)
                    if _STATUS_AVAILABLE:
                        update_task(task_id,
                                    speed=speed,
                                    done=uploaded_bytes,
                                    total=total_size)
                    yield chunk

        form = aiohttp.FormData()
        form.add_field("file", _file_generator(), filename=src.name,
                       content_type="application/octet-stream")
        async with session.post(upload_url, data=form, headers=headers) as resp:
            result = await resp.json()

    if result.get("status") != "ok":
        raise RuntimeError(f"GoFile upload failed: {result}")

    file_data = result["data"]
    if file_data.get("parentFolder"):
        return f"https://gofile.io/d/{file_data['parentFolder']}"
    return file_data.get("downloadPage", "")
