""" get system info """

# Copyright (C) 2020-2022 by UsergeTeam@Github, < https://github.com/UsergeTeam >.
#
# This file is part of < https://github.com/UsergeTeam/Userge > project,
# and is released under the "GNU v3.0 License Agreement".
# Please see < https://github.com/UsergeTeam/Userge/blob/master/LICENSE >
#
# All rights reserved.

from datetime import datetime

import psutil
from psutil._common import bytes2human

from userge import userge, Message


async def generate_sysinfo(workdir: str) -> str:
    info = {
        'BOOT': datetime.fromtimestamp(psutil.boot_time()).strftime("%Y-%m-%d %H:%M:%S")
    }

    # CPU
    cpu_freq = psutil.cpu_freq()
    if cpu_freq:
        freq = cpu_freq.current
        freq_str = f"{round(freq / 1000, 2)}GHz" if freq >= 1000 else f"{round(freq, 2)}MHz"
    else:
        freq_str = "N/A"
    info['CPU'] = (
        f"{psutil.cpu_percent(interval=1)}% "
        f"({psutil.cpu_count()}) "
        f"{freq_str}"
    )

    # Memory
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    info['RAM']  = f"{bytes2human(vm.total)}, {bytes2human(vm.available)} available"
    info['SWAP'] = f"{bytes2human(sm.total)}, {sm.percent}%"

    # Disk
    du  = psutil.disk_usage(workdir)
    dio = psutil.disk_io_counters()
    info['DISK'] = (
        f"{bytes2human(du.used)} / {bytes2human(du.total)} ({du.percent}%)"
    )
    if dio:
        info['DISK I/O'] = f"R {bytes2human(dio.read_bytes)} | W {bytes2human(dio.write_bytes)}"

    # Network
    nio = psutil.net_io_counters()
    info['NET I/O'] = f"TX {bytes2human(nio.bytes_sent)} | RX {bytes2human(nio.bytes_recv)}"

    # Temperature (optional — not available on all hosts)
    try:
        sensors = psutil.sensors_temperatures()
        if sensors:
            # Try coretemp first, then any available sensor group
            temps = sensors.get('coretemp') or next(iter(sensors.values()), None)
            if temps:
                avg_temp = sum(t.current for t in temps) / len(temps)
                info['TEMP'] = f"{round(avg_temp, 1)}\u00b0C"
    except (AttributeError, NotImplementedError):
        pass  # sensors_temperatures not supported on this platform

    info = {f"{key}:": value for key, value in info.items()}
    max_len = max(len(k) for k in info)
    return (
        "```\n"
        + "\n".join(f"{k:<{max_len}} {v}" for k, v in info.items())
        + "```"
    )


@userge.on_cmd("sysinfo", about={
    'header': "Get system info of your host machine.",
    'usage': "{tr}sysinfo"})
async def get_sysinfo(message: Message):
    """ get system info """
    await message.edit("`Getting system information …`")
    response = await generate_sysinfo(userge.workdir)
    await message.edit("<u>**System Information**</u>:\n" + response)
