# pylint: disable=missing-module-docstring
#
# Copyright (C) 2020-2022 by UsergeTeam@Github, < https://github.com/UsergeTeam >.
#
# This file is part of < https://github.com/UsergeTeam/Userge > project,
# and is released under the "GNU v3.0 License Agreement".
# Please see < https://github.com/UsergeTeam/Userge/blob/master/LICENSE >
#
# All rights reserved.

__all__ = ['Restart']

import os
import sys

from userge import logging
from ...ext import RawClient

_LOG = logging.getLogger(__name__)


def _restart(hard: bool = False) -> None:
    """Restart the current process by re-executing it."""
    if hard:
        os.execl(sys.executable, sys.executable, *sys.argv)
    else:
        os.kill(os.getpid(), 15)  # SIGTERM


class Restart(RawClient):  # pylint: disable=missing-class-docstring
    @staticmethod
    async def restart(hard: bool = False, **_) -> None:
        """ Restart the Userge """
        _LOG.info(f"Restarting Userge [{'HARD' if hard else 'SOFT'}]")
        _restart(hard)
