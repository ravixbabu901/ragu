# Copyright (C) 2020-2022 by UsergeTeam@Github, < https://github.com/UsergeTeam >.
#
# This file is part of < https://github.com/UsergeTeam/Userge > project,
# and is released under the "GNU v3.0 License Agreement".
# Please see < https://github.com/UsergeTeam/Userge/blob/master/LICENSE >
#
# All rights reserved.

"""Loader plugin stub — external loader not used in this standalone build."""

from userge import userge, Message

CHANNEL = userge.getCLogger(__name__)


@userge.on_cmd("update", about={
    'header': "Check for updates",
    'usage': "{tr}update"}, del_pre=True, allow_channels=False)
async def update(message: Message):
    """ check or do updates (standalone mode — git pull manually) """
    await message.edit(
        "This is a **standalone build**. Update by pulling the latest Git commits "
        "and rebuilding the Docker image.",
        del_in=10)
