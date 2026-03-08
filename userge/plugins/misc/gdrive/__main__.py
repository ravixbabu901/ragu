""" manage your gdrive """

# Copyright (C) 2020-2022 by UsergeTeam@Github, < https://github.com/UsergeTeam >.
#
# This file is part of < https://github.com/UsergeTeam/Userge > project,
# and is released under the "GNU v3.0 License Agreement".
# Please see < https://github.com/UsergeTeam/Userge/blob/master/LICENSE >
#
# All rights reserved.

import asyncio
import io
import math
import os
import pickle  # nosec
import re
import time
from datetime import datetime
from functools import wraps
from json import dumps
from mimetypes import guess_type
from typing import Optional
from urllib.parse import quote, urlparse, parse_qs

import requests as _requests
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from httplib2 import Http
from oauth2client.client import (
    OAuth2Credentials, OAuth2WebServerFlow, HttpAccessTokenRefreshError, FlowExchangeError)

from userge import userge, Message, config, get_collection, pool
from userge.plugins.misc.download import url_download, tg_download
from userge.utils import humanbytes, time_formatter, is_url
from userge.utils.exceptions import ProcessCanceled
from userge.utils.path_resolver import resolve_download_path
from .. import gdrive

_CREDS: Optional[OAuth2Credentials] = None
_AUTH_FLOW: Optional[OAuth2WebServerFlow] = None
_PARENT_ID = ""
OAUTH_SCOPE = ["https://www.googleapis.com/auth/drive",
               "https://www.googleapis.com/auth/drive.file",
               "https://www.googleapis.com/auth/drive.metadata"]
REDIRECT_URI = "http://localhost:5000"
G_DRIVE_DIR_MIME_TYPE = "application/vnd.google-apps.folder"
G_DRIVE_FILE_LINK = "📄 <a href='https://drive.google.com/open?id={}'>{}</a> __({})"
G_DRIVE_FOLDER_LINK = "📁 <a href='https://drive.google.com/drive/folders/{}'>{}</a> __(folder)__"
_GDRIVE_ID = re.compile(
    r'https://drive.google.com/[\w?.&=/]+([-\w]{33}|(?<=[/=])0(?:A[-\w]{17}|B[-\w]{26}))')

_LOG = userge.getLogger(__name__)
_SAVED_SETTINGS = get_collection("CONFIGS")

# ------------------------------------------------------------------
# Chunk sizes — multiples of 256 KiB per Google's requirement.
# ------------------------------------------------------------------
_UPLOAD_CHUNK   = 256 * 1024 * 1024   # 256 MiB
_DOWNLOAD_CHUNK = 128 * 1024 * 1024   # 128 MiB

# ------------------------------------------------------------------
# Reusable requests.Session for direct GDrive downloads.
# ------------------------------------------------------------------
_DL_SESSION: Optional[_requests.Session] = None


def _get_dl_session() -> _requests.Session:
    global _DL_SESSION  # pylint: disable=global-statement
    if _DL_SESSION is None:
        s = _requests.Session()
        adapter = _requests.adapters.HTTPAdapter(
            pool_connections=4,
            pool_maxsize=4,
            max_retries=3,
        )
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _DL_SESSION = s
    return _DL_SESSION


@userge.on_start
async def _init() -> None:
    global _CREDS  # pylint: disable=global-statement
    _LOG.debug("Setting GDrive DBase...")
    result = await _SAVED_SETTINGS.find_one({'_id': 'GDRIVE'}, {'creds': 1})
    _CREDS = pickle.loads(result['creds']) if result else None  # nosec


async def _set_creds(creds: object) -> str:
    global _CREDS  # pylint: disable=global-statement
    _LOG.info("Setting Creds...")
    _CREDS = creds
    result = await _SAVED_SETTINGS.update_one(
        {'_id': 'GDRIVE'}, {"$set": {'creds': pickle.dumps(creds)}}, upsert=True)
    if result.upserted_id:
        return "`Creds Added`"
    return "`Creds Updated`"


async def _clear_creds() -> str:
    global _CREDS  # pylint: disable=global-statement
    _CREDS = None
    _LOG.info("Clearing Creds...")
    if await _SAVED_SETTINGS.find_one_and_delete({'_id': 'GDRIVE'}):
        return "`Creds Cleared`"
    return "`Creds Not Found`"


async def _refresh_creds() -> None:
    try:
        _LOG.debug("Refreshing Creds...")
        _CREDS.refresh(Http())
    except HttpAccessTokenRefreshError as h_e:
        _LOG.exception(h_e)
        _LOG.info(await _clear_creds())


def creds_dec(func):
    """ decorator for check CREDS """
    @wraps(func)
    async def wrapper(self):
        # pylint: disable=protected-access
        if _CREDS:
            if _CREDS.access_token_expired:
                await _refresh_creds()
            await func(self)
        else:
            await self._message.edit("Please run `.gsetup` first", del_in=5)  # skipcq: PYL-W0212
    return wrapper


def _get_access_token() -> str:
    """Return a valid OAuth access token, refreshing if needed."""
    if _CREDS.access_token_expired:
        _CREDS.refresh(Http())
    return _CREDS.access_token


class _GDrive:
    """ GDrive Class For Search, Upload, Download, Copy, Move, Delete, EmptyTrash, ... """
    def __init__(self) -> None:
        self._parent_id = _PARENT_ID or gdrive.G_DRIVE_PARENT_ID
        self._completed = 0
        self._list = 1
        self._progress = None
        self._output = None
        self._is_canceled = False
        self._is_finished = False

    def _cancel(self) -> None:
        self._is_canceled = True

    def _finish(self) -> None:
        self._is_finished = True

    @property
    def _service(self) -> object:
        return build("drive", "v3", credentials=_CREDS, cache_discovery=False)

    @pool.run_in_thread
    def _search(self,
                search_query: str,
                flags: dict,
                parent_id: str = "",
                list_root: bool = False) -> str:
        force = '-f' in flags
        pid = parent_id or self._parent_id
        if pid and not force:
            query = f"'{pid}' in parents and (name contains '{search_query}')"
        else:
            query = f"name contains '{search_query}'"
        page_token = None
        limit = int(flags.get('-l', 20))
        page_size = limit if limit < 50 else 50
        fields = 'nextPageToken, files(id, name, mimeType, size)'
        results = []
        msg = ""
        while True:
            response = self._service.files().list(supportsTeamDrives=True,
                                                  includeTeamDriveItems=True,
                                                  q=query, spaces='drive',
                                                  corpora='allDrives', fields=fields,
                                                  pageSize=page_size,
                                                  orderBy='modifiedTime desc',
                                                  pageToken=page_token).execute()
            for file_ in response.get('files', []):
                if len(results) >= limit:
                    break
                if file_.get('mimeType') == G_DRIVE_DIR_MIME_TYPE:
                    msg += G_DRIVE_FOLDER_LINK.format(file_.get('id'), file_.get('name'))
                else:
                    msg += G_DRIVE_FILE_LINK.format(
                        file_.get('id'), file_.get('name'), humanbytes(int(file_.get('size', 0))))
                msg += '\n'
                results.append(file_)
            if len(results) >= limit:
                break
            page_token = response.get('nextPageToken', None)
            if page_token is None:
                break
        if not msg:
            return "`Not Found!`"
        if parent_id and not force:
            out = f"**List GDrive Folder** : `{parent_id}`\n"
        elif list_root and not force:
            out = f"**List GDrive Root Folder** : `{self._parent_id}`\n"
        else:
            out = f"**GDrive Search Query** : `{search_query}`\n"
        return out + f"**Limit** : `{limit}`\n\n__Results__ : \n\n" + msg

    def _set_permission(self, file_id: str) -> None:
        permissions = {'role': 'reader', 'type': 'anyone'}
        self._service.permissions().create(fileId=file_id, body=permissions,
                                           supportsTeamDrives=True).execute()
        _LOG.info("Set Permission : %s for Google-Drive File : %s", permissions, file_id)

    def _get_file_path(self, file_id: str, file_name: str) -> str:
        tmp_path = [file_name]
        while True:
            response = self._service.files().get(
                fileId=file_id, fields='parents', supportsTeamDrives=True).execute()
            if not response:
                break
            file_id = response['parents'][0]
            response = self._service.files().get(
                fileId=file_id, fields='name', supportsTeamDrives=True).execute()
            tmp_path.append(response['name'])
        return '/'.join(reversed(tmp_path[:-1]))

    def _get_output(self, file_id: str) -> str:
        file_ = self._service.files().get(
            fileId=file_id, fields="id, name, size, mimeType", supportsTeamDrives=True).execute()
        file_id = file_.get('id')
        file_name = file_.get('name')
        file_size = humanbytes(int(file_.get('size', 0)))
        mime_type = file_.get('mimeType')
        if mime_type == G_DRIVE_DIR_MIME_TYPE:
            out = G_DRIVE_FOLDER_LINK.format(file_id, file_name)
        else:
            out = G_DRIVE_FILE_LINK.format(file_id, file_name, file_size)
        if gdrive.G_DRIVE_INDEX_LINK:
            link = os.path.join(
                gdrive.G_DRIVE_INDEX_LINK.rstrip('/'),
                quote(self._get_file_path(file_id, file_name)))
            if mime_type == G_DRIVE_DIR_MIME_TYPE:
                link += '/'
            out += f"\n👥 __[Shareable Link]({link})__"
        return out

    def _upload_file(self, file_path: str, parent_id: str) -> str:
        if self._is_canceled:
            raise ProcessCanceled
        # FIX: fall back to application/octet-stream, NOT text/plain.
        # guess_type returns None for .mkv, .eac3, .ac3 and many other
        # containers — uploading them as text/plain breaks GDrive previews
        # and download MIME types. application/octet-stream is the correct
        # "unknown binary" fallback, identical to what the upstream script uses.
        mime_type = guess_type(file_path)[0] or "application/octet-stream"
        file_name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        body = {"name": file_name, "mimeType": mime_type, "description": "Uploaded using Userge"}
        if parent_id:
            body["parents"] = [parent_id]
        if file_size == 0:
            media_body = MediaFileUpload(file_path, mimetype=mime_type)
            u_file_obj = self._service.files().create(body=body, media_body=media_body,
                                                      supportsTeamDrives=True).execute()
            file_id = u_file_obj.get("id")
        else:
            media_body = MediaFileUpload(file_path, mimetype=mime_type,
                                         chunksize=_UPLOAD_CHUNK, resumable=True)
            u_file_obj = self._service.files().create(body=body, media_body=media_body,
                                                      supportsTeamDrives=True)
            c_time = time.time()
            response = None
            while response is None:
                status, response = u_file_obj.next_chunk(num_retries=5)
                if self._is_canceled:
                    raise ProcessCanceled
                if status:
                    f_size = status.total_size
                    diff = time.time() - c_time or 0.001
                    uploaded = status.resumable_progress
                    percentage = uploaded / f_size * 100
                    speed = round(uploaded / diff, 2)
                    eta = round((f_size - uploaded) / speed) if speed else 0
                    tmp = \
                        "__Uploading to GDrive...__\n" + \
                        "```\n[{}{}]({}%)```\n" + \
                        "**File Name** : `{}`\n" + \
                        "**File Size** : `{}`\n" + \
                        "**Uploaded** : `{}`\n" + \
                        "**Completed** : `{}/{}`\n" + \
                        "**Speed** : `{}/s`\n" + \
                        "**ETA** : `{}`"
                    self._progress = tmp.format(
                        "".join((config.FINISHED_PROGRESS_STR
                                 for _ in range(math.floor(percentage / 5)))),
                        "".join((config.UNFINISHED_PROGRESS_STR
                                 for _ in range(20 - math.floor(percentage / 5)))),
                        round(percentage, 2),
                        file_name,
                        humanbytes(f_size),
                        humanbytes(uploaded),
                        self._completed,
                        self._list,
                        humanbytes(speed),
                        time_formatter(eta))
            file_id = response.get("id")
        if not gdrive.G_DRIVE_IS_TD:
            self._set_permission(file_id)
        self._completed += 1
        _LOG.info(
            "Created Google-Drive File => Name: %s ID: %s Size: %s", file_name, file_id, file_size)
        return file_id

    def _create_drive_dir(self, dir_name: str, parent_id: str) -> str:
        if self._is_canceled:
            raise ProcessCanceled
        body = {"name": dir_name, "mimeType": G_DRIVE_DIR_MIME_TYPE}
        if parent_id:
            body["parents"] = [parent_id]
        file_ = self._service.files().create(body=body, supportsTeamDrives=True).execute()
        file_id = file_.get("id")
        file_name = file_.get("name")
        if not gdrive.G_DRIVE_IS_TD:
            self._set_permission(file_id)
        self._completed += 1
        _LOG.info("Created Google-Drive Folder => Name: %s ID: %s ", file_name, file_id)
        return file_id

    def _upload_dir(self, input_directory: str, parent_id: str) -> str:
        if self._is_canceled:
            raise ProcessCanceled
        list_dirs = os.listdir(input_directory)
        if len(list_dirs) == 0:
            return parent_id
        self._list += len(list_dirs)
        new_id = None
        for item in list_dirs:
            current_file_name = os.path.join(input_directory, item)
            if os.path.isdir(current_file_name):
                current_dir_id = self._create_drive_dir(item, parent_id)
                new_id = self._upload_dir(current_file_name, current_dir_id)
            else:
                new_id = self._upload_file(current_file_name, parent_id)
        return new_id

    def _upload(self, file_name: str) -> None:
        try:
            if os.path.isfile(file_name):
                self._output = self._upload_file(file_name, self._parent_id)
                self._output = self._get_output(self._output)
            elif os.path.isdir(file_name):
                dir_id = self._create_drive_dir(os.path.basename(file_name), self._parent_id)
                self._upload_dir(file_name, dir_id)
                self._output = self._get_output(dir_id)
            else:
                raise ValueError(f"{file_name} not found")
        except HttpError as h_e:
            _LOG.exception(h_e)
            self._output = h_e
        except ProcessCanceled:
            self._output = "`Process Canceled!`"
        finally:
            self._finish()

    def _download_file(self, path: str, name: str, **kwargs) -> None:
        if self._is_canceled:
            raise ProcessCanceled
        file_id = kwargs.get('file_id', '')
        file_size = kwargs.get('file_size', 0)

        # ── Direct HTTP download via requests.Session ──────────────────────
        # This is significantly faster than MediaIoBaseDownload + httplib2
        # because requests uses proper kernel TCP buffers and keep-alive.
        token = _get_access_token()
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&supportsTeamDrives=true"
        headers = {"Authorization": f"Bearer {token}"}

        session = _get_dl_session()
        downloaded = 0
        c_time = time.time()

        with session.get(url, headers=headers, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            if file_size == 0:
                content_length = resp.headers.get("Content-Length")
                if content_length:
                    file_size = int(content_length)
            with open(path, "wb") as out_file:
                for chunk in resp.iter_content(chunk_size=_DOWNLOAD_CHUNK):
                    if self._is_canceled:
                        raise ProcessCanceled
                    if not chunk:
                        continue
                    out_file.write(chunk)
                    downloaded += len(chunk)
                    diff = time.time() - c_time or 0.001
                    speed = round(downloaded / diff, 2)
                    eta = round((file_size - downloaded) / speed) if speed and file_size > downloaded else 0
                    percentage = downloaded / file_size * 100 if file_size else 0
                    tmp = \
                        "__Downloading From GDrive...__\n" + \
                        "```\n[{}{}]({}%)```\n" + \
                        "**File Name** : `{}`\n" + \
                        "**File Size** : `{}`\n" + \
                        "**Downloaded** : `{}`\n" + \
                        "**Completed** : `{}/{}`\n" + \
                        "**Speed** : `{}/s`\n" + \
                        "**ETA** : `{}`"
                    self._progress = tmp.format(
                        "".join((config.FINISHED_PROGRESS_STR
                                 for _ in range(math.floor(percentage / 5)))),
                        "".join((config.UNFINISHED_PROGRESS_STR
                                 for _ in range(20 - math.floor(percentage / 5)))),
                        round(percentage, 2),
                        name,
                        humanbytes(file_size),
                        humanbytes(downloaded),
                        self._completed,
                        self._list,
                        humanbytes(speed),
                        time_formatter(eta))

    def _list_drive_dir(self, file_id: str) -> list:
        query = f"'{file_id}' in parents and (name contains '*')"
        fields = 'nextPageToken, files(id, name, mimeType)'
        page_token = None
        page_size = 100
        files = []
        while True:
            response = self._service.files().list(supportsTeamDrives=True,
                                                  includeTeamDriveItems=True,
                                                  q=query, spaces='drive',
                                                  fields=fields, pageToken=page_token,
                                                  pageSize=page_size, corpora='allDrives',
                                                  orderBy='folder, name').execute()
            files.extend(response.get('files', []))
            page_token = response.get('nextPageToken', None)
            if page_token is None:
                break
            if self._is_canceled:
                raise ProcessCanceled
        return files

    def _create_server_dir(self, current_path: str, folder_name: str) -> str:
        path = os.path.join(current_path, folder_name)
        if not os.path.exists(path):
            os.mkdir(path)
        _LOG.info("Created Folder => Name: %s", folder_name)
        self._completed += 1
        return path

    def _download_dir(self, path: str, **kwargs) -> None:
        if self._is_canceled:
            raise ProcessCanceled
        files = self._list_drive_dir(kwargs['id'])
        if len(files) == 0:
            return
        self._list += len(files)
        for file_ in files:
            if file_['mimeType'] == G_DRIVE_DIR_MIME_TYPE:
                path_ = self._create_server_dir(path, file_['name'])
                self._download_dir(path_, **file_)
            else:
                self._download_file(path, **file_)

    def _download(self, file_id: str) -> None:
        try:
            meta = self._service.files().get(
                fileId=file_id,
                fields='id, name, mimeType, size',
                supportsTeamDrives=True).execute()
            file_name = meta.get('name')
            mime_type = meta.get('mimeType')
            file_size = int(meta.get('size', 0))

            if mime_type == G_DRIVE_DIR_MIME_TYPE:
                # Folder download — recurse
                self._download_dir(
                    os.path.join(config.Dynamic.DOWN_PATH, file_name),
                    file_id=file_id)
                self._output = os.path.join(config.Dynamic.DOWN_PATH, file_name)
            else:
                dest = os.path.join(config.Dynamic.DOWN_PATH, file_name)
                self._download_file(
                    dest, file_name,
                    file_id=file_id,
                    file_size=file_size)
                self._output = dest

        except HttpError as h_e:
            _LOG.exception(h_e)
            self._output = h_e
        except ProcessCanceled:
            self._output = "`Process Canceled!`"
        except Exception as e:  # pylint: disable=broad-except
            _LOG.exception(e)
            self._output = str(e)
        finally:
            self._finish()

    def _copy_file(self, file_id: str, parent_id: str) -> str:
        if self._is_canceled:
            raise ProcessCanceled
        body = {}
        if parent_id:
            body["parents"] = [parent_id]
        drive_file = self._service.files().copy(
            fileId=file_id, body=body, supportsTeamDrives=True).execute()
        file_id = drive_file.get("id")
        if not gdrive.G_DRIVE_IS_TD:
            self._set_permission(file_id)
        self._completed += 1
        return file_id

    def _copy_dir(self, file_id: str, parent_id: str) -> str:
        if self._is_canceled:
            raise ProcessCanceled
        files = self._list_drive_dir(file_id)
        if len(files) == 0:
            return parent_id
        self._list += len(files)
        for file_ in files:
            if file_['mimeType'] == G_DRIVE_DIR_MIME_TYPE:
                dir_id = self._create_drive_dir(file_['name'], parent_id)
                self._copy_dir(file_['id'], dir_id)
            else:
                self._copy_file(file_['id'], parent_id)
        return parent_id

    def _copy(self, file_id: str) -> None:
        try:
            drive_file = self._service.files().get(
                fileId=file_id, fields="id, name, mimeType",
                supportsTeamDrives=True).execute()
            if drive_file['mimeType'] == G_DRIVE_DIR_MIME_TYPE:
                dir_id = self._create_drive_dir(drive_file['name'], self._parent_id)
                self._copy_dir(file_id, dir_id)
                self._output = self._get_output(dir_id)
            else:
                file_id = self._copy_file(file_id, self._parent_id)
                self._output = self._get_output(file_id)
        except HttpError as h_e:
            _LOG.exception(h_e)
            self._output = h_e
        except ProcessCanceled:
            self._output = "`Process Canceled!`"
        finally:
            self._finish()

    def _create_drive_folder(self, folder_name: str, parent_id: str) -> str:
        body = {"name": folder_name, "mimeType": G_DRIVE_DIR_MIME_TYPE}
        if parent_id:
            body["parents"] = [parent_id]
        file_ = self._service.files().create(body=body, supportsTeamDrives=True).execute()
        file_id = file_.get("id")
        if not gdrive.G_DRIVE_IS_TD:
            self._set_permission(file_id)
        return file_id

    def _move(self, file_id: str) -> str:
        drive_file = self._service.files().get(
            fileId=file_id, fields='parents', supportsTeamDrives=True).execute()
        previous_parents = ",".join(drive_file.get('parents'))
        drive_file = self._service.files().update(
            fileId=file_id, addParents=self._parent_id,
            removeParents=previous_parents, fields='id, parents',
            supportsTeamDrives=True).execute()
        return self._get_output(drive_file.get('id'))

    def _delete(self, file_id: str) -> None:
        self._service.files().delete(fileId=file_id, supportsTeamDrives=True).execute()

    def _empty_trash(self) -> None:
        self._service.files().emptyTrash().execute()

    def _get(self, file_id: str) -> str:
        fields = "id, name, mimeType, size, modifiedTime, createdTime, parents, webViewLink"
        drive_file = self._service.files().get(
            fileId=file_id, fields=fields, supportsTeamDrives=True).execute()
        msg = ""
        for k, v in drive_file.items():
            msg += f"**{k}** : `{v}`\n"
        return msg

    def _get_perms(self, file_id: str) -> str:
        out = ""
        permissions = self._service.permissions().list(
            fileId=file_id, fields="permissions(id, role, type, emailAddress)",
            supportsTeamDrives=True).execute()
        for perm in permissions.get('permissions', []):
            out += (f"**ID** : `{perm.get('id')}`\n"
                    f"**role** : `{perm.get('role')}`\n"
                    f"**type** : `{perm.get('type')}`\n"
                    f"**email** : `{perm.get('emailAddress', 'N/A')}`\n\n")
        return out or "`No Permissions Found!`"

    def _set_perms(self, file_id: str) -> str:
        permissions = {'role': 'reader', 'type': 'anyone'}
        self._service.permissions().create(
            fileId=file_id, body=permissions, supportsTeamDrives=True).execute()
        return self._get_output(file_id)

    def _del_perms(self, file_id: str) -> str:
        permissions = self._service.permissions().list(
            fileId=file_id, fields="permissions(id, role, type)",
            supportsTeamDrives=True).execute()
        for perm in permissions.get('permissions', []):
            if perm.get('type') == 'anyone':
                self._service.permissions().delete(
                    fileId=file_id, permissionId=perm['id'],
                    supportsTeamDrives=True).execute()
        return self._get_output(file_id)


class Worker(_GDrive):
    """ Worker Class for GDrive """
    def __init__(self, message: Message) -> None:
        self._message = message
        super().__init__()

    def _get_file_id(self, filter_str: bool = False) -> tuple:
        link = self._message.input_str
        if filter_str:
            link = self._message.filtered_input_str
        if not link:
            raise ValueError("No Link Provided!")
        # Try to extract a Google Drive file/folder ID from a URL
        found = _GDRIVE_ID.findall(link)
        if found:
            candidate = found[0][0] if isinstance(found[0], tuple) else found[0]
            # Google Drive IDs are always at least 25 characters.
            # If the regex matched something shorter it's a false positive.
            if len(candidate) >= 25:
                return candidate, True
        # If it looks like a Drive URL but regex failed, try query param 'id'
        if link.startswith("https://drive.google.com"):
            from urllib.parse import urlparse, parse_qs  # pylint: disable=import-outside-toplevel
            parsed = urlparse(link)
            file_id = parse_qs(parsed.query).get('id', [None])[0]
            if file_id and len(file_id) >= 25:
                return file_id, True
        # Treat the raw input as a bare file ID
        return link.strip(), False

    async def setup(self) -> None:
        global _AUTH_FLOW  # pylint: disable=global-statement
        _AUTH_FLOW = OAuth2WebServerFlow(
            client_id=gdrive.G_DRIVE_CLIENT_ID,
            client_secret=gdrive.G_DRIVE_CLIENT_SECRET,
            scope=OAUTH_SCOPE,
            redirect_uri=REDIRECT_URI)
        flow_url = _AUTH_FLOW.step1_get_authorize_url()
        await self._message.edit(
            f"Please visit the following link:\n{flow_url}\n\n"
            "After authorizing, run `.gconf <code>`", disable_web_page_preview=True)

    async def confirm_setup(self) -> None:
        global _AUTH_FLOW  # pylint: disable=global-statement
        if not _AUTH_FLOW:
            await self._message.edit("Please run `.gsetup` first", del_in=5)
            return
        code = self._message.filtered_input_str
        if not code:
            await self._message.edit("No auth code provided!", del_in=5)
            return
        try:
            creds = _AUTH_FLOW.step2_exchange(code)
        except FlowExchangeError as f_e:
            await self._message.err(str(f_e))
        else:
            _AUTH_FLOW = None
            await self._message.edit(await _set_creds(creds))

    async def clear(self) -> None:
        await self._message.edit(await _clear_creds())

    async def set_parent(self) -> None:
        global _PARENT_ID  # pylint: disable=global-statement
        file_id, _ = self._get_file_id()
        _PARENT_ID = file_id
        await self._message.edit(f"**GDrive Parent ID** : `{_PARENT_ID}`", del_in=5)

    async def reset_parent(self) -> None:
        global _PARENT_ID  # pylint: disable=global-statement
        _PARENT_ID = ""
        await self._message.edit("`Parent ID Reset`", del_in=5)

    async def share(self) -> None:
        await self._message.edit("`Loading GDrive Share...`")
        file_id, _ = self._get_file_id()
        try:
            out = await pool.run_in_thread(self._set_perms)(file_id)
        except HttpError as h_e:
            _LOG.exception(h_e)
            await self._message.err(h_e._get_reason())  # pylint: disable=protected-access
        else:
            await self._message.edit(
                f"**Shared Successfully**\n\n{out}", disable_web_page_preview=True, log=__name__)

    @creds_dec
    async def search(self) -> None:
        await self._message.edit("`Loading GDrive Search...`")
        flags = self._message.flags
        search_query = self._message.filtered_input_str
        if not search_query:
            await self._message.err("Search query not provided!")
            return
        out = await self._search(search_query, flags)
        await self._message.edit_or_send_as_file(
            out, disable_web_page_preview=True, log=__name__)

    @creds_dec
    async def make_folder(self) -> None:
        await self._message.edit("`Loading GDrive MakeFolder...`")
        folder_name = self._message.filtered_input_str
        if not folder_name:
            await self._message.err("Folder name not provided!")
            return
        try:
            folder_id = await pool.run_in_thread(
                self._create_drive_folder)(folder_name, self._parent_id)
        except HttpError as h_e:
            _LOG.exception(h_e)
            await self._message.err(h_e._get_reason())  # pylint: disable=protected-access
        else:
            await self._message.edit(
                G_DRIVE_FOLDER_LINK.format(folder_id, folder_name),
                disable_web_page_preview=True, log=__name__)

    @creds_dec
    async def list_folder(self) -> None:
        await self._message.edit("`Loading GDrive ListFolder...`")
        flags = self._message.flags
        try:
            file_id, _ = self._get_file_id(filter_str=True)
        except ValueError:
            file_id = ""
        if file_id:
            out = await self._search('*', flags, file_id)
        else:
            out = await self._search('*', flags, list_root=True)
        await self._message.edit_or_send_as_file(
            out, disable_web_page_preview=True, log=__name__)

    @creds_dec
    async def upload(self) -> None:
        await self._message.edit("`Loading GDrive Upload...`")
        file_name = self._message.filtered_input_str
        if not file_name:
            await self._message.err("File path not provided!")
            return
        if is_url(file_name):
            try:
                file_name, _ = await url_download(self._message, file_name)
            except ProcessCanceled:
                await self._message.canceled()
                return
            except Exception as e_e:  # pylint: disable=broad-except
                await self._message.err(str(e_e))
                return
        if not os.path.exists(file_name):
            await self._message.err(f"`{file_name}` not found!")
            return
        pool.submit_thread(self._upload, file_name)
        start_t = datetime.now()
        with self._message.cancel_callback(self._cancel):
            while not self._is_finished:
                if self._progress is not None:
                    await self._message.edit(self._progress)
                await asyncio.sleep(config.Dynamic.EDIT_SLEEP_TIMEOUT)
        end_t = datetime.now()
        m_s = (end_t - start_t).seconds
        if isinstance(self._output, HttpError):
            out = f"**ERROR** : `{self._output._get_reason()}`"  # pylint: disable=protected-access
        elif self._output is not None and not self._is_canceled:
            out = f"**Uploaded Successfully** __in {m_s} seconds__\n\n{self._output}"
        elif self._output is not None and self._is_canceled:
            out = self._output
        else:
            out = "`failed to upload.. check logs?`"
        await self._message.edit(out, disable_web_page_preview=True, log=__name__)

    @creds_dec
    async def download(self) -> None:
        await self._message.edit("`Loading GDrive Download...`")
        file_id, _ = self._get_file_id()
        pool.submit_thread(self._download, file_id)
        start_t = datetime.now()
        with self._message.cancel_callback(self._cancel):
            while not self._is_finished:
                if self._progress is not None:
                    await self._message.edit(self._progress)
                await asyncio.sleep(config.Dynamic.EDIT_SLEEP_TIMEOUT)
        end_t = datetime.now()
        m_s = (end_t - start_t).seconds
        if isinstance(self._output, HttpError):
            out = f"**ERROR** : `{self._output._get_reason()}`"  # pylint: disable=protected-access
        elif self._output is not None and not self._is_canceled:
            file_name = os.path.basename(str(self._output))
            out = f"**Downloaded Successfully** __in {m_s} seconds__\n\n`{file_name}`"
        elif self._output is not None and self._is_canceled:
            out = self._output
        else:
            out = "`failed to download.. check logs?`"
        await self._message.edit(out, disable_web_page_preview=True, log=__name__)

    @creds_dec
    async def copy(self) -> None:
        await self._message.edit("`Loading GDrive Copy...`")
        if not self._parent_id:
            await self._message.edit("First set parent path by `.gset`", del_in=5)
            return
        file_id, _ = self._get_file_id()
        pool.submit_thread(self._copy, file_id)
        start_t = datetime.now()
        with self._message.cancel_callback(self._cancel):
            while not self._is_finished:
                if self._progress is not None:
                    await self._message.edit(self._progress)
                await asyncio.sleep(config.Dynamic.EDIT_SLEEP_TIMEOUT)
        end_t = datetime.now()
        m_s = (end_t - start_t).seconds
        if isinstance(self._output, HttpError):
            out = f"**ERROR** : `{self._output._get_reason()}`"  # pylint: disable=protected-access
        elif self._output is not None and not self._is_canceled:
            out = f"**Copied Successfully** __in {m_s} seconds__\n\n{self._output}"
        elif self._output is not None and self._is_canceled:
            out = self._output
        else:
            out = "`failed to copy.. check logs?`"
        await self._message.edit(out, disable_web_page_preview=True, log=__name__)

    @creds_dec
    async def move(self) -> None:
        """ Move file/folder in GDrive """
        if not self._parent_id:
            await self._message.edit("First set parent path by `.gset`", del_in=5)
            return
        await self._message.edit("`Loading GDrive Move...`")
        file_id, _ = self._get_file_id()
        try:
            link = await pool.run_in_thread(self._move)(file_id)
        except HttpError as h_e:
            _LOG.exception(h_e)
            await self._message.err(h_e._get_reason())  # pylint: disable=protected-access
        else:
            await self._message.edit(
                f"`{file_id}` **Moved Successfully**\n\n{link}", log=__name__)

    @creds_dec
    async def delete(self) -> None:
        """ Delete file/folder in GDrive """
        await self._message.edit("`Loading GDrive Delete...`")
        file_id, _ = self._get_file_id()
        try:
            await pool.run_in_thread(self._delete)(file_id)
        except HttpError as h_e:
            _LOG.exception(h_e)
            await self._message.err(h_e._get_reason())  # pylint: disable=protected-access
        else:
            await self._message.edit(
                f"`{file_id}` **Deleted Successfully**", del_in=5, log=__name__)

    @creds_dec
    async def empty(self) -> None:
        """ Empty GDrive Trash """
        await self._message.edit("`Loading GDrive Empty Trash...`")
        try:
            await pool.run_in_thread(self._empty_trash)()
        except HttpError as h_e:
            _LOG.exception(h_e)
            await self._message.err(h_e._get_reason())  # pylint: disable=protected-access
        else:
            await self._message.edit(
                "`Empty the Trash Successfully`", del_in=5, log=__name__)

    @creds_dec
    async def get(self) -> None:
        """ Get details for file/folder in GDrive """
        await self._message.edit("`Loading GDrive GetDetails...`")
        file_id, _ = self._get_file_id()
        try:
            meta_data = await pool.run_in_thread(self._get)(file_id)
        except HttpError as h_e:
            _LOG.exception(h_e)
            await self._message.err(h_e._get_reason())  # pylint: disable=protected-access
            return
        out = f"**I Found these Details for** `{file_id}`\n\n{meta_data}"
        await self._message.edit_or_send_as_file(
            out, disable_web_page_preview=True, log=__name__)

    @creds_dec
    async def get_perms(self) -> None:
        await self._message.edit("`Loading GDrive GetPermissions...`")
        file_id, _ = self._get_file_id()
        try:
            out = await pool.run_in_thread(self._get_perms)(file_id)
        except HttpError as h_e:
            _LOG.exception(h_e)
            await self._message.err(h_e._get_reason())  # pylint: disable=protected-access
        else:
            await self._message.edit_or_send_as_file(
                out, disable_web_page_preview=True, log=__name__)

    @creds_dec
    async def set_perms(self) -> None:
        await self._message.edit("`Loading GDrive SetPermissions...`")
        file_id, _ = self._get_file_id()
        try:
            out = await pool.run_in_thread(self._set_perms)(file_id)
        except HttpError as h_e:
            _LOG.exception(h_e)
            await self._message.err(h_e._get_reason())  # pylint: disable=protected-access
        else:
            await self._message.edit(out, disable_web_page_preview=True, log=__name__)

    @creds_dec
    async def del_perms(self) -> None:
        await self._message.edit("`Loading GDrive DeletePermissions...`")
        file_id, _ = self._get_file_id()
        try:
            out = await pool.run_in_thread(self._del_perms)(file_id)
        except HttpError as h_e:
            _LOG.exception(h_e)
            await self._message.err(h_e._get_reason())  # pylint: disable=protected-access
        else:
            await self._message.edit(out, disable_web_page_preview=True, log=__name__)


@userge.on_cmd("gsetup", about={
    'header': "Setup GDrive",
    'usage': "{tr}gsetup"})
async def gsetup_(message: Message):
    """ setup gdrive """
    await Worker(message).setup()


@userge.on_cmd("gconf", about={
    'header': "Confirm GDrive Auth",
    'usage': "{tr}gconf <auth_code>"})
async def gconf_(message: Message):
    """ confirm gdrive auth """
    await Worker(message).confirm_setup()


@userge.on_cmd("gclear", about={
    'header': "Clear GDrive Creds",
    'usage': "{tr}gclear"})
async def gclear_(message: Message):
    """ clear gdrive creds """
    await Worker(message).clear()


@userge.on_cmd("gset", about={
    'header': "Set GDrive Parent ID",
    'usage': "{tr}gset <drive_url_or_id>"})
async def gset_(message: Message):
    """ set gdrive parent """
    await Worker(message).set_parent()


@userge.on_cmd("greset", about={
    'header': "Reset GDrive Parent ID",
    'usage': "{tr}greset"})
async def greset_(message: Message):
    """ reset gdrive parent """
    await Worker(message).reset_parent()


@userge.on_cmd("gfind", about={
    'header': "Search files in GDrive",
    'flags': {'-l': "limit results (default 20)", '-f': "force search all drive"},
    'usage': "{tr}gfind [-l<num>] [-f] <query>"})
async def gfind_(message: Message):
    """ search gdrive """
    await Worker(message).search()


@userge.on_cmd("gls", about={
    'header': "List GDrive folder",
    'flags': {'-l': "limit results (default 20)", '-f': "list all"},
    'usage': "{tr}gls [drive_url_or_id]"})
async def gls_(message: Message):
    """ list gdrive folder """
    await Worker(message).list_folder()


@userge.on_cmd("gmake", about={
    'header': "Create a GDrive folder",
    'usage': "{tr}gmake <folder_name>"})
async def gmake_(message: Message):
    """ make gdrive folder """
    await Worker(message).make_folder()


@userge.on_cmd("gshare", about={
    'header': "Share a GDrive file/folder",
    'usage': "{tr}gshare <drive_url_or_id>"})
async def gshare_(message: Message):
    """ share gdrive file """
    await Worker(message).share()


@userge.on_cmd("gup", about={
    'header': "Upload to GDrive",
    'usage': "{tr}gup <file_path_or_url>"})
async def gup_(message: Message):
    """ upload to gdrive """
    await Worker(message).upload()


@userge.on_cmd("gdown", about={
    'header': "Download from GDrive",
    'usage': "{tr}gdown <drive_url_or_id>"})
async def gdown_(message: Message):
    """ download from gdrive """
    await Worker(message).download()


@userge.on_cmd("gcopy", about={
    'header': "Copy a GDrive file/folder",
    'usage': "{tr}gcopy <drive_url_or_id>"})
async def gcopy_(message: Message):
    """ copy gdrive file """
    await Worker(message).copy()


@userge.on_cmd("gmove", about={
    'header': "Move a GDrive file/folder",
    'usage': "{tr}gmove <drive_url_or_id>"})
async def gmove_(message: Message):
    """ move gdrive file """
    await Worker(message).move()


@userge.on_cmd("gdel", about={
    'header': "Delete a GDrive file/folder",
    'usage': "{tr}gdel <drive_url_or_id>"})
async def gdel_(message: Message):
    """ delete gdrive file """
    await Worker(message).delete()


@userge.on_cmd("gempty", about={
    'header': "Empty GDrive Trash",
    'usage': "{tr}gempty"})
async def gempty_(message: Message):
    """ empty gdrive trash """
    await Worker(message).empty()


@userge.on_cmd("gget", about={
    'header': "Get GDrive file/folder details",
    'usage': "{tr}gget <drive_url_or_id>"})
async def gget_(message: Message):
    """ get gdrive file details """
    await Worker(message).get()


@userge.on_cmd("ggetperm", about={
    'header': "Get GDrive permissions",
    'usage': "{tr}ggetperm <drive_url_or_id>"})
async def ggetperm_(message: Message):
    """ get gdrive permissions """
    await Worker(message).get_perms()


@userge.on_cmd("gsetperm", about={
    'header': "Set GDrive permissions (make public)",
    'usage': "{tr}gsetperm <drive_url_or_id>"})
async def gsetperm_(message: Message):
    """ set gdrive permissions """
    await Worker(message).set_perms()


@userge.on_cmd("gdelperm", about={
    'header': "Remove GDrive permissions",
    'usage': "{tr}gdelperm <drive_url_or_id>"})
async def gdelperm_(message: Message):
    """ delete gdrive permissions """
    await Worker(message).del_perms()
