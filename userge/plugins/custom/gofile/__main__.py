""" GoFile download and upload plugin """

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


async def _get_account_token(session: aiohttp.ClientSession) -> str:
    """Create a guest GoFile account and return the account token."""
    async with session.post(f"{_GOFILE_BASE}/accounts") as resp:
        data = await resp.json()
    if data.get("status") == "ok":
        return data["data"]["token"]
    raise RuntimeError(f"GoFile account creation failed: {data}")


async def _get_content(session: aiohttp.ClientSession,
                        content_id: str,
                        account_token: str) -> dict:
    """Fetch content metadata for a given content ID."""
    headers = {
        "Authorization": f"Bearer {account_token}",
        "Cookie": f"accountToken={account_token}",
    }
    url = f"{_GOFILE_BASE}/contents/{content_id}"
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
            # 1. Create guest account and get account token
            account_token = await _get_account_token(session)

            # 2. Fetch content metadata
            content = await _get_content(session, content_id, account_token)

            # 3. Collect files
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
                "Authorization": f"Bearer {account_token}",
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
        # Try relative to downloads
        src = Path(config.Dynamic.DOWN_PATH) / path_str
    if not src.is_file():
        await message.err(f"File not found: `{path_str}`")
        return

    # Preserve the original unencoded filename for display
    display_name = src.name
    size = src.stat().st_size
    task_id = f"goup_{display_name}"
    await message.edit(f"`Uploading to GoFile: {display_name} ({humanbytes(size)})`")

    if _STATUS_AVAILABLE:
        register_task(task_id, display_name, kind="upload")

    try:
        link = await _upload_to_gofile(src, task_id, size)
        if _STATUS_AVAILABLE:
            complete_task(task_id)
        await message.edit(
            f"✅ Uploaded: `{display_name}`\n"
            f"🔗 Link: {link}"
        )
    except Exception as e:  # pylint: disable=broad-except
        if _STATUS_AVAILABLE:
            remove_task(task_id)
        await message.err(str(e))


import urllib.parse
from aiohttp import MultipartWriter, payload

async def _upload_to_gofile(src: Path, task_id: str, total_size: int) -> str:
    """Upload file to GoFile and return share link."""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{_GOFILE_BASE}/servers") as resp:
            data = await resp.json()
        server = data["data"]["servers"][0]["name"]
        upload_url = f"https://{server}.gofile.io/contents/uploadfile"

        headers = {"Authorization": f"Bearer {GOFILE_TOKEN}"}

        filename = src.name
        # RFC 5987 filename* (UTF-8) value
        filename_star = "UTF-8''" + urllib.parse.quote(filename, safe="")

        mp = MultipartWriter("form-data")

        with open(src, "rb") as f:
            part = mp.append(payload.BufferedReaderPayload(f, content_type="application/octet-stream"))

            # Force Content-Disposition similar to curl -F file=@...
            part.set_content_disposition(
                "form-data",
                name="file",
                filename=filename,
            )
            # Also add filename* for servers that prefer it
            part.headers["Content-Disposition"] += f"; filename*={filename_star}"

            async with session.post(upload_url, data=mp, headers=headers) as resp:
                result = await resp.json()

    if result.get("status") != "ok":
        raise RuntimeError(f"GoFile upload failed: {result}")

    file_data = result["data"]


    if file_data.get("parentFolderCode"):
        return f"https://gofile.io/d/{file_data['parentFolderCode']}"
    return file_data.get("downloadPage", "")
