""" list files and directories """

import os
from pathlib import Path

from userge import userge, Message, config


@userge.on_cmd("ls", about={
    'header': "List files in a directory",
    'usage': "{tr}ls [path]",
    'examples': ["{tr}ls", "{tr}ls /app/downloads"]})
async def ls_(message: Message):
    """ list directory contents """
    input_path = message.input_str.strip() if message.input_str else None
    base = Path(input_path) if input_path else Path(config.Dynamic.DOWN_PATH)

    if not base.exists():
        await message.edit(f"`Path not found: {base}`")
        return

    if base.is_file():
        size = base.stat().st_size
        await message.edit(f"`{base}` — {_human_size(size)}")
        return

    try:
        entries = sorted(base.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError:
        await message.edit(f"`Permission denied: {base}`")
        return

    if not entries:
        await message.edit(f"📂 `{base}` — *empty*")
        return

    lines = [f"📂 `{base}`\n"]
    for entry in entries:
        if entry.is_dir():
            lines.append(f"  📁 {entry.name}/")
        else:
            size = entry.stat().st_size
            lines.append(f"  📄 {entry.name}  ({_human_size(size)})")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n…(truncated)"
    await message.edit(text)


def _human_size(num: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} PB"
