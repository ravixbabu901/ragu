# Copyright (C) 2020-2022 by UsergeTeam@Github, < https://github.com/UsergeTeam >.
#
# This file is part of < https://github.com/UsergeTeam/Userge > project,
# and is released under the "GNU v3.0 License Agreement".
# Please see < https://github.com/UsergeTeam/Userge/blob/master/LICENSE >
#
# All rights reserved.

"""system related commands"""


from os import environ, getpid, kill
from typing import Set, Optional
try:
    from signal import CTRL_C_EVENT as SIGTERM
except ImportError:
    from signal import SIGTERM

DISABLED_CHATS: Set[int] = set()


class Dynamic:
    DISABLED_ALL = False

    RUN_DYNO_SAVER = False
    STATUS = None


def get_env(key: str) -> Optional[str]:
    return environ.get(key)


async def set_env(key: str, value: str) -> None:
    environ[key] = value


async def del_env(key: str) -> Optional[str]:
    if key in environ:
        val = environ.pop(key)
        return val
    return None


def shutdown() -> None:    kill(getpid(), SIGTERM)
