"""
猜测前台是不是在跑全屏游戏。

Dell 的 WMI 不提供「当前是什么应用」，所以只能用启发式：
前台窗口铺满整个显示器 + 进程名不在排除表里 = 大概率是游戏。

这个判断必然有误判（比如浏览器按 F11 全屏看视频），所以：
1. 默认关闭，由用户在设置里主动打开
2. 排除表可编辑
3. 调用方需要自己做防抖，避免窗口切换时频繁切换模式
"""

import ctypes
import os
from ctypes import wintypes
from typing import List, Optional, Tuple

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MONITOR_DEFAULTTONEAREST = 2
_TOLERANCE_PX = 6

DEFAULT_EXCLUSIONS: List[str] = [
    "explorer", "chrome", "msedge", "firefox", "opera", "brave", "iexplore",
    "code", "devenv", "idea64", "pycharm64", "goland64", "webstorm64",
    "windowsterminal", "powershell", "pwsh", "cmd", "conhost",
    "wechat", "qq", "telegram", "discord", "slack",
    "potplayermini64", "vlc", "mpc-hc64", "spotify",
]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", wintypes.DWORD)]


def foregroundProcessName() -> Optional[str]:
    """前台窗口所属进程名（小写，不含 .exe）。拿不到时返回 None。"""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    hProc = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not hProc:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not _kernel32.QueryFullProcessImageNameW(hProc, 0, buf, ctypes.byref(size)):
            return None
        return os.path.splitext(os.path.basename(buf.value))[0].lower()
    finally:
        _kernel32.CloseHandle(hProc)


def isForegroundFullscreen() -> bool:
    """前台窗口是否铺满所在显示器"""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd or not _user32.IsWindowVisible(hwnd):
        return False
    rect = _RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    hMonitor = _user32.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST)
    if not hMonitor:
        return False
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if not _user32.GetMonitorInfoW(hMonitor, ctypes.byref(info)):
        return False
    mon = info.rcMonitor
    return (
        abs(rect.left - mon.left) <= _TOLERANCE_PX and
        abs(rect.top - mon.top) <= _TOLERANCE_PX and
        abs(rect.right - mon.right) <= _TOLERANCE_PX and
        abs(rect.bottom - mon.bottom) <= _TOLERANCE_PX
    )


def detectFullscreenGame(exclusions: Optional[List[str]] = None) -> Tuple[bool, Optional[str]]:
    """返回 (是否判定为全屏游戏, 进程名)"""
    if exclusions is None:
        exclusions = DEFAULT_EXCLUSIONS
    name = foregroundProcessName()
    if name is None:
        return (False, None)
    if name in {e.strip().lower() for e in exclusions if e.strip()}:
        return (False, name)
    if name in ("progman", "workerw", "shellexperiencehost", "textinputhost"):
        return (False, name)
    return (isForegroundFullscreen(), name)
