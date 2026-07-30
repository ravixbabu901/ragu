import os
from userge import Message, userge
from userge.plugins.misc.gdrive.__main__ import Worker, _GDRIVE_PROXY_URL

@userge.on_cmd("gupp", about={
    "header": "Upload to GDrive using proxy",
    "usage": "{tr}gupp <file_path_or_url>",
    "description": "Uses GUP_PROXY or GDRIVE_UPLOAD_PROXY only for this upload command"
})
async def gupp_(message: Message):
    proxy_url = os.environ.get("GUP_PROXY") or os.environ.get("GDRIVE_UPLOAD_PROXY")
    if not proxy_url:
        await message.err("Set `GUP_PROXY` or `GDRIVE_UPLOAD_PROXY`.")
        return

    token = _GDRIVE_PROXY_URL.set(proxy_url)
    try:
        await Worker(message).upload()
    finally:
        _GDRIVE_PROXY_URL.reset(token)
