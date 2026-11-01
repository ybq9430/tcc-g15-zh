"""读取笔记本电源状态：是否接着交流电、电池电量。"""

import ctypes
from ctypes import wintypes
from typing import Optional, Tuple


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", wintypes.BYTE),
        ("BatteryFlag", wintypes.BYTE),
        ("BatteryLifePercent", wintypes.BYTE),
        ("SystemStatusFlag", wintypes.BYTE),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


def powerStatus() -> Tuple[Optional[bool], Optional[int]]:
    """返回 (是否接着交流电, 电池百分比)。读不到时对应位置为 None。

    ACLineStatus: 0=电池供电, 1=交流电, 255=未知
    BatteryLifePercent: 0~100, 255=未知
    """
    status = _SYSTEM_POWER_STATUS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return (None, None)
    onAc = None if status.ACLineStatus == 255 else (status.ACLineStatus == 1)
    percent = None if status.BatteryLifePercent == 255 else int(status.BatteryLifePercent)
    return (onAc, percent)
