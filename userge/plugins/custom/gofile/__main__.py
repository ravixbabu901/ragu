""" GoFile download and upload plugin """

import asyncio
import glob as _glob
import hashlib
import math
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import aiohttp

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

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


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
    """
    Returns (account_token, files_dict) using the 2026 auth flow.
    """
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


async def _resolve_folder_id(session: aiohttp.ClientSession, code_or_uuid: str) -> str:
    """
    Resolve a GoFile share-code or UUID to an internal folder UUID.
    If it already looks like a UUID, returns it as-is.
    """
    if _UUID_RE.match(code_or_uuid):
        return code_or_uuid

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
        f"{_GOFILE_BASE}/contents/{code_or_uuid}", headers=headers
    ) as resp:
        data = await resp.json()

    if data.get("status") != "ok":
        raise RuntimeError(f"Could not resolve GoFile folder '{code_or_uuid}': {data}")

    folder_id = data["data"].get("id")
    if not folder_id:
        raise RuntimeError(
            f"GoFile folder '{code_or_uuid}' has no 'id' field in response."
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


# ------------------------------------------------------------------ #
#  _upload_single_file — THE CANONICAL IMPLEMENTATION                 #
#                                                                      #
#  SOURCE OF TRUTH: commit 2228596946146266f3d1bcc42c3954823a68dd94   #
#                                                                      #
#  NEVER change this function's upload mechanism.                      #
#  It uses aiohttp.FormData with the raw (unencoded) filename.         #
#  DO NOT add urllib.parse.quote / filename_star / MultipartWriter.    #
#  aiohttp handles RFC 5987 Content-Disposition encoding internally.  #
#  Any manual pre-encoding causes double-encoding on GoFile's side     #
#  → filenames become "Ingrid%20Goes%20West%20%282017%29...".          #
# ------------------------------------------------------------------ #
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
    Upload one file to GoFile using aiohttp.FormData (supports folderId).
    Returns result["data"] dict.
    """
    upload_url = f"https://{server}.gofile.io/contents/uploadfile"
    headers = {"Authorization": f"Bearer {GOFILE_TOKEN}"}

    filename = src.name          # raw, unencoded — NEVER call quote() on this
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

    async def _file_gen():
        with open(src, "rb") as f:
            while True:
                chunk = f.read(256 * 1024)
                if not chunk:
                    break
                on_bytes(len(chunk))
                yield chunk

    form = aiohttp.FormData()
    # folderId MUST come before file in the form
    if folder_id:
        form.add_field("folderId", folder_id)
    form.add_field(
        "file",
        _file_gen(),
        filename=filename,                        # raw filename — aiohttp encodes correctly
        content_type="application/octet-stream",
    )

    async with session.post(upload_url, data=form, headers=headers) as resp:
        result = await resp.json()

    if result.get("status") != "ok":
        raise RuntimeError(f"GoFile upload failed: {result}")

    return result["data"]


async def _progress_display_loop(
    message: Message,
    progress_queue: asyncio.Queue,
) -> tuple:
    """
    Drain the progress queue until a ("done",...) or ("error",...) sentinel arrives.
    Returns (result_link_or_None, error_msg_or_None).
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
    """Resolve glob patterns, returning a sorted deduplicated list of file Paths."""
    found: List[Path] = []
    seen: set = set()

    for pat in patterns:
        pat = pat.strip()
        if not pat:
            continue
        p = Path(pat)
        # Try as absolute/relative glob first
        matches = sorted(_glob.glob(pat))
        if not matches:
            # Try relative to DOWN_PATH
            matches = sorted(_glob.glob(str(base_dir / pat)))
        if matches:
            for m in matches:
                mp = Path(m).resolve()
                if mp not in seen:
                    seen.add(mp)
                    found.append(Path(m))
        else:
            # Literal path
            if p.is_absolute():
                candidates = [p]
            else:
                candidates = [p, base_dir / pat]
            for candidate in candidates:
                rp = candidate.resolve()
                if rp not in seen and (candidate.is_file() or candidate.is_dir()):
                    seen.add(rp)
                    found.append(candidate)
                    break

    return found


async def _create_gofile_folder(
    session: aiohttp.ClientSession,
    folder_name: str,
) -> tuple:
    """
    Create a new GoFile folder under the account root.
    Returns (folder_uuid, share_code).
    """
    auth_headers = {
        "Authorization": f"Bearer {GOFILE_TOKEN}",
        "Content-Type": "application/json",
    }

    # Step 1 — get account ID
    async with session.get(
        f"{_GOFILE_BASE}/accounts/getid",
        headers=auth_headers,
    ) as resp:
        acc_info = await resp.json()

    if acc_info.get("status") != "ok":
        raise RuntimeError(f"GoFile account info failed: {acc_info}")

    account_id = acc_info["data"]["id"]

    # Step 2 — get root folder ID
    async with session.get(
        f"{_GOFILE_BASE}/accounts/{account_id}",
        headers=auth_headers,
    ) as resp:
        acc_detail = await resp.json()

    if acc_detail.get("status") != "ok":
        raise RuntimeError(f"GoFile account detail failed: {acc_detail}")

    root_folder_id = acc_detail["data"]["rootFolder"]

    # Step 3 — create sub-folder
    async with session.post(
        f"{_GOFILE_BASE}/contents/createFolder",
        headers=auth_headers,
        json={"parentFolderId": root_folder_id, "folderName": folder_name},
    ) as resp:
        folder_resp = await resp.json()

    if folder_resp.get("status") != "ok":
        raise RuntimeError(f"GoFile folder creation failed: {folder_resp}")

    folder_data = folder_resp["data"]
    folder_uuid = folder_data.get("id") or folder_data.get("folderId") or ""
    share_code  = folder_data.get("code") or folder_data.get("folderCode") or ""

    return folder_uuid, share_code


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
        "{tr}goup file1.mkv | file2.srt",
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

    # ── Optional existing-folder on 2nd line ──────────────────────────────
    existing_folder_ref: Optional[str] = None
    if "\n" in raw:
        raw, second_line = raw.split("\n", 1)
        raw = raw.strip()
        second_line = second_line.strip()
        if second_line:
            if "gofile.io/d/" in second_line:
                existing_folder_ref = second_line.rstrip("/").split("/d/")[-1]
            else:
                existing_folder_ref = second_line

    base_dir = Path(config.Dynamic.DOWN_PATH)
    segments = [s.strip() for s in raw.split("|") if s.strip()]
    srcs = _resolve_glob_patterns(segments, base_dir)

    if not srcs:
        await message.err("No files matched the given pattern(s).")
        return

    # Single directory → folder upload
    if len(srcs) == 1 and srcs[0].is_dir():
        await _goup_folder(message, srcs[0], existing_folder_ref)
        return

    # One or more files
    file_srcs = [p for p in srcs if p.is_file()]
    if not file_srcs:
        await message.err("No uploadable files found.")
        return

    if len(file_srcs) == 1 and existing_folder_ref is None:
        await _goup_file(message, file_srcs[0], None)
    else:
        await _goup_multi(message, file_srcs, existing_folder_ref)


# ------------------------------------------------------------------
# Single-file upload
# ------------------------------------------------------------------

async def _goup_file(
    message: Message,
    src: Path,
    existing_folder_ref: Optional[str],
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

                folder_id: Optional[str] = None
                if existing_folder_ref:
                    folder_id = await _resolve_folder_id(session, existing_folder_ref)

                file_data = await _upload_single_file(
                    session, server, src, task_id, size, progress_queue,
                    folder_id=folder_id,
                )

                # Rename the auto-created GoFile folder to match the filename
                if not existing_folder_ref:
                    parent_folder_id = file_data.get("parentFolder")
                    if parent_folder_id:
                        await _rename_gofile_content(
                            session, parent_folder_id, display_name
                        )

                share_code = (
                    file_data.get("parentFolderCode")
                    or file_data.get("foldersCode")
                    or ""
                )
                if not share_code:
                    dl_page = file_data.get("downloadPage", "")
                    share_code = (
                        dl_page.rstrip("/").split("/d/")[-1]
                        if "/d/" in dl_page else ""
                    )
                link = (
                    f"https://gofile.io/d/{share_code}"
                    if share_code else
                    file_data.get("downloadPage", "N/A")
                )
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
# Multi-file upload (pipe-separated patterns)
# ------------------------------------------------------------------

async def _goup_multi(
    message: Message,
    srcs: List[Path],
    existing_folder_ref: Optional[str],
) -> None:
    total_count = len(srcs)
    total_size  = sum(s.stat().st_size for s in srcs)
    folder_display_name = srcs[0].name  # GoFile folder named after first file

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

                if existing_folder_ref:
                    folder_id  = await _resolve_folder_id(session, existing_folder_ref)
                    share_code = existing_folder_ref
                else:
                    folder_id, share_code = await _create_gofile_folder(
                        session, folder_display_name
                    )

                for idx, src in enumerate(srcs):
                    task_id = f"goup_{src.name}"
                    if _STATUS_AVAILABLE:
                        register_task(task_id, src.name, kind="upload")

                    file_data = await _upload_single_file(
                        session, server, src, task_id,
                        src.stat().st_size, progress_queue,
                        folder_id=folder_id,
                        completed_count=idx,
                        total_count=total_count,
                    )

                    if _STATUS_AVAILABLE:
                        complete_task(task_id)

                    if not share_code:
                        share_code = (
                            file_data.get("parentFolderCode")
                            or file_data.get("foldersCode")
                            or ""
                        )
                        if not share_code:
                            dl_page = file_data.get("downloadPage", "")
                            share_code = (
                                dl_page.rstrip("/").split("/d/")[-1]
                                if "/d/" in dl_page else ""
                            )

                link = f"https://gofile.io/d/{share_code}" if share_code else "N/A"
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
        f"**Folder Name** : `{folder_display_name}`\n"
        f"**Total Size** : `{humanbytes(total_size)}`\n"
        f"🔗 **Link** : {result_link}"
    )


# ------------------------------------------------------------------
# Folder upload
# ------------------------------------------------------------------

async def _goup_folder(
    message: Message,
    src_dir: Path,
    existing_folder_ref: Optional[str],
) -> None:
    all_files = sorted(f for f in src_dir.rglob("*") if f.is_file())
    if not all_files:
        await message.err(f"No files found in `{src_dir}`.")
        return

    total_count = len(all_files)
    total_size  = sum(f.stat().st_size for f in all_files)
    folder_name = src_dir.name

    await message.edit(
        f"`Uploading folder '{folder_name}' — "
        f"{total_count} file(s) ({humanbytes(total_size)})…`"
    )

    progress_queue: asyncio.Queue = asyncio.Queue()
    start_t = datetime.now()

    async def _run():
        try:
            async with aiohttp.ClientSession() as session:
                server = await _get_best_server(session)

                if existing_folder_ref:
                    folder_id  = await _resolve_folder_id(session, existing_folder_ref)
                    share_code = existing_folder_ref
                else:
                    folder_id, share_code = await _create_gofile_folder(
                        session, folder_name
                    )

                for idx, src in enumerate(all_files):
                    task_id = f"goup_{src.name}"
                    if _STATUS_AVAILABLE:
                        register_task(task_id, src.name, kind="upload")

                    file_data = await _upload_single_file(
                        session, server, src, task_id,
                        src.stat().st_size, progress_queue,
                        folder_id=folder_id,
                        completed_count=idx,
                        total_count=total_count,
                    )

                    if _STATUS_AVAILABLE:
                        complete_task(task_id)

                    if not share_code:
                        share_code = (
                            file_data.get("parentFolderCode")
                            or file_data.get("foldersCode")
                            or ""
                        )
                        if not share_code:
                            dl_page = file_data.get("downloadPage", "")
                            share_code = (
                                dl_page.rstrip("/").split("/d/")[-1]
                                if "/d/" in dl_page else ""
                            )

                link = f"https://gofile.io/d/{share_code}" if share_code else "N/A"
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
        f"✅ **Folder Uploaded Successfully in {m_s} seconds**\n\n"
        f"**Folder Name** : `{folder_name}`\n"
        f"**Files** : `{total_count}`\n"
        f"**Total Size** : `{humanbytes(total_size)}`\n"
        f"🔗 **Link** : {result_link}"
    )
