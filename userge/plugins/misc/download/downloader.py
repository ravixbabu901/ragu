# pylint: disable=missing-module-docstring
#
# Copyright (C) 2020-2022 by UsergeTeam@Github, < https://github.com/UsergeTeam >.
#
# This file is part of < https://github.com/UsergeTeam/Userge > project,
# and is released under the "GNU v3.0 License Agreement".
# Please see < https://github.com/UsergeTeam/Userge/blob/master/LICENSE >
#
# All rights reserved.

import asyncio
import math
import os
import re
from datetime import datetime
from json import dumps
from typing import Tuple, Union
from urllib.parse import unquote_plus

from pySmartDL import SmartDL
from pyrogram.types import Message as PyroMessage
from pyrogram import enums

from userge import Message, config
from userge.utils import progress, humanbytes, extract_entities
from userge.utils.exceptions import ProcessCanceled


def _get_media_filename(to_download: PyroMessage) -> str:
    """
    Return the best available filename for a Pyrogram media message.
    Tries document.file_name first, then falls back to photo/video/audio
    attributes, then to a generic name derived from the media type.
    """
    media = (
        to_download.document
        or to_download.video
        or to_download.audio
        or to_download.voice
        or to_download.video_note
        or to_download.sticker
        or to_download.animation
    )
    if media:
        # document and audio carry a file_name attribute
        fname = getattr(media, "file_name", None)
        if fname:
            return fname
        # Build a fallback like "video_<id>.mp4"
        mime = getattr(media, "mime_type", "")
        ext = mime.split("/")[-1] if mime else "bin"
        return f"{to_download.media.value}_{media.file_unique_id}.{ext}"
    if to_download.photo:
        return f"photo_{to_download.photo.file_unique_id}.jpg"
    return f"media_{to_download.id}"


async def handle_download(message: Message, resource: Union[Message, str],
                          from_url: bool = False) -> Tuple[str, int]:
    """ download from resource """
    if not isinstance(resource, PyroMessage):
        return await url_download(message, resource)
    if resource.media_group_id:
        resources = await message.client.get_media_group(
            resource.chat.id,
            resource.id
        )
        dlloc, din = [], 0
        for res in resources:
            dl_loc, d_in = await tg_download(message, res, from_url)
            din += d_in
            dlloc.append(dl_loc)
        return dumps(dlloc), din
    return await tg_download(message, resource)


async def url_download(message: Message, url: str) -> Tuple[str, int]:
    """ download from link """
    pattern = r"^(?:(?:https|tg):\/\/)?(?:www\.)?(?:t\.me\/|openmessage\?)(?:(?:c\/(\d+))|(\w+)|(?:user_id\=(\d+)))(?:\/|&message_id\=)(\d+)(\?single)?$"  # noqa
    # group 1: private supergroup id, group 2: chat username,
    # group 3: private group/chat id, group 4: message id
    # group 5: check for download single media from media group
    match = re.search(pattern, url.split('|', 1)[0].strip())
    if match:
        chat_id = None
        msg_id = int(match.group(4))
        if match.group(1):
            chat_id = int("-100" + match.group(1))
        elif match.group(2):
            chat_id = match.group(2)
        elif match.group(3):
            chat_id = int(match.group(3))
        if chat_id and msg_id:
            resource = await message.client.get_messages(chat_id, msg_id)
            if resource.media_group_id and not bool(match.group(5)):
                output = await handle_download(message, resource, True)
            elif resource.media:
                output = await tg_download(message, resource, True)
            else:
                raise Exception("given tg link doesn't have any media")
            return output
        raise Exception("invalid telegram message link!")
    await message.edit("`Downloading From URL...`")
    start_t = datetime.now()
    custom_file_name = unquote_plus(os.path.basename(url))
    if "|" in url:
        url, c_file_name = url.split("|", maxsplit=1)
        url = url.strip()
        if c_file_name:
            custom_file_name = c_file_name.strip()
    dl_loc = os.path.join(config.Dynamic.DOWN_PATH, custom_file_name)
    downloader = SmartDL(url, dl_loc, progress_bar=False)
    downloader.start(blocking=False)
    with message.cancel_callback(downloader.stop):
        while not downloader.isFinished():
            total_length = downloader.filesize if downloader.filesize else 0
            downloaded = downloader.get_dl_size()
            percentage = downloader.get_progress() * 100
            speed = downloader.get_speed(human=True)
            estimated_total_time = downloader.get_eta(human=True)
            progress_str = \
                "__{}__\n" + \
                "```\n[{}{}]```\n" + \
                "**Progress** : `{}%`\n" + \
                "**URL** : `{}`\n" + \
                "**FILENAME** : `{}`\n" + \
                "**Completed** : `{}`\n" + \
                "**Total** : `{}`\n" + \
                "**Speed** : `{}`\n" + \
                "**ETA** : `{}`"
            progress_str = progress_str.format(
                "trying to download",
                ''.join((config.FINISHED_PROGRESS_STR
                         for _ in range(math.floor(percentage / 5)))),
                ''.join((config.UNFINISHED_PROGRESS_STR
                         for _ in range(20 - math.floor(percentage / 5)))),
                round(percentage, 2),
                url,
                custom_file_name,
                humanbytes(downloaded),
                humanbytes(total_length),
                speed,
                estimated_total_time)
            await message.edit(progress_str, disable_web_page_preview=True)
            await asyncio.sleep(config.Dynamic.EDIT_SLEEP_TIMEOUT)
    if message.process_is_canceled:
        raise ProcessCanceled
    # SmartDL may have followed redirects and saved to a different path
    final_path = downloader.get_dest()
    return final_path or dl_loc, (datetime.now() - start_t).seconds


async def tg_download(
    message: Message, to_download: Message, from_url: bool = False
) -> Tuple[str, int]:
    """ download from tg file """
    if not to_download.media:
        dl_loc, mite = [], 0
        ets = extract_entities(
            to_download, [
                enums.MessageEntityType.URL, enums.MessageEntityType.TEXT_LINK])
        if len(ets) == 0:
            raise Exception("nothing found to download")
        for uarl in ets:
            _dl_loc, b_ = await url_download(message, uarl)
            dl_loc.append(_dl_loc)
            mite += b_
        return dumps(dl_loc), mite
    await message.edit("`Downloading From TG...`")
    start_t = datetime.now()

    # Determine the save path:
    #   1. Explicit custom name via "|" syntax  →  DOWN_PATH/custom_name
    #   2. Explicit name as input (no "|")      →  DOWN_PATH/input_name
    #   3. No input (or called from_url)        →  DOWN_PATH/real_media_filename
    #      (passing the full path prevents Pyrogram from using "bot.temp")
    filtered = message.filtered_input_str or ""
    if "|" in filtered and not from_url:
        _, c_file_name = filtered.split("|", maxsplit=1)
        c_file_name = c_file_name.strip()
        custom_file_name = (
            os.path.join(config.Dynamic.DOWN_PATH, c_file_name)
            if c_file_name
            else os.path.join(config.Dynamic.DOWN_PATH, _get_media_filename(to_download))
        )
    elif filtered and not from_url:
        custom_file_name = os.path.join(
            config.Dynamic.DOWN_PATH, filtered.strip()
        )
    else:
        # Key fix: supply the full file path including the real filename so
        # Pyrogram does NOT fall back to "bot.temp"
        custom_file_name = os.path.join(
            config.Dynamic.DOWN_PATH, _get_media_filename(to_download)
        )

    with message.cancel_callback():
        dl_loc = await message.client.download_media(
            message=to_download,
            file_name=custom_file_name,
            progress=progress,
            progress_args=(message, "trying to download")
        )
    if message.process_is_canceled:
        raise ProcessCanceled
    if not isinstance(dl_loc, str):
        raise TypeError("File Corrupted!")
    dl_loc = os.path.relpath(dl_loc)
    return dl_loc, (datetime.now() - start_t).seconds
