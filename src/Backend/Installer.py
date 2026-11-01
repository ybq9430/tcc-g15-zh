# (c) github.com/ybq9430/tcc-g15-zh
# GPLv3
"""自安装：把当前运行目录（PyInstaller onedir 产物）镜像到 Program Files。

需要以管理员身份运行。由 `tcc-g15.exe --install` 触发。

存在的理由：PyInstaller onedir 产物无法用「解压覆盖」以外的方式安装，
而覆盖时需要先结束正在运行的旧实例（exe 与 _internal 里的 DLL 都被映射，
无法直接写入）。这里用进程快照 + TerminateProcess 完成，不依赖外部工具。
"""

import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from typing import List

INSTALL_DIR = r"C:\Program Files (x86)\Thermal Control Center"
EXE_NAME = "tcc-g15.exe"

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
DETACHED_PROCESS = 0x00000008


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD),
        ('cntUsage', wintypes.DWORD),
        ('th32ProcessID', wintypes.DWORD),
        ('th32DefaultHeapID', ctypes.POINTER(ctypes.c_ulong)),
        ('th32ModuleID', wintypes.DWORD),
        ('cntThreads', wintypes.DWORD),
        ('th32ParentProcessID', wintypes.DWORD),
        ('pcPriClassBase', ctypes.c_long),
        ('dwFlags', wintypes.DWORD),
        ('szExeFile', ctypes.c_wchar * 260),
    ]


def _log(msg: str) -> None:
    """安装过程必须留下痕迹：打包版是 -w，没有控制台"""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    try:
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
        d = os.path.join(base, 'TCC-G15', 'logs')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'install.log'), 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def _pids_by_name(name: str) -> List[int]:
    """枚举同名进程；不依赖 taskkill（安全策略可能拦截外部工具）"""
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snaper = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snaper == wintypes.HANDLE(-1).value or not snaper:
        return []
    out: List[int] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = k32.Process32FirstW(snaper, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == name.lower():
                out.append(int(entry.th32ProcessID))
            ok = k32.Process32NextW(snaper, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snaper)
    return out


def _terminate(pid: int) -> bool:
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
    if not h:
        return False
    try:
        if not k32.TerminateProcess(h, 0):
            return False
        k32.WaitForSingleObject(h, 5000)
        return True
    finally:
        k32.CloseHandle(h)


def _stop_running_instances() -> int:
    me = os.getpid()
    victims = [p for p in _pids_by_name(EXE_NAME) if p != me]
    for pid in victims:
        ok = _terminate(pid)
        _log(f"结束旧实例 pid={pid} -> {'ok' if ok else 'fail'}")
    if victims:
        time.sleep(1.5)          # 等文件句柄真正释放
    return len(victims)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _mirror_tree(src: str, dst: str) -> None:
    """先删后建，避免旧版本残留的 DLL 干扰新版本加载"""
    if os.path.isdir(dst):
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)


def install(install_dir: str = INSTALL_DIR, launch: bool = True) -> int:
    src_dir = os.path.dirname(os.path.abspath(sys.executable))
    _log(f"=== 安装开始：{src_dir} -> {install_dir} ===")

    if not ctypes.windll.shell32.IsUserAnAdmin():
        _log("失败：需要管理员权限。请右键「以管理员身份运行」后加 --install")
        return 2

    if os.path.normcase(os.path.normpath(src_dir)) == os.path.normcase(os.path.normpath(install_dir)):
        _log("当前已在安装目录内运行，无需安装")
        return 0

    src_exe = os.path.join(src_dir, EXE_NAME)
    src_internal = os.path.join(src_dir, '_internal')
    if not os.path.isfile(src_exe) or not os.path.isdir(src_internal):
        _log(f"失败：源目录不完整（需要 {EXE_NAME} 与 _internal）")
        return 3

    _stop_running_instances()

    os.makedirs(install_dir, exist_ok=True)
    dst_exe = os.path.join(install_dir, EXE_NAME)
    dst_internal = os.path.join(install_dir, '_internal')

    try:
        shutil.copy2(src_exe, dst_exe)
        _log("已替换主程序")
        _mirror_tree(src_internal, dst_internal)
        _log(f"已镜像 _internal（{len(os.listdir(dst_internal))} 项）")
    except PermissionError as e:
        _log(f"失败：文件被占用（{e}）。请先退出托盘里的实例再重试")
        return 4
    except Exception as e:
        _log(f"失败：{type(e).__name__}: {e}")
        return 5

    try:
        same = _sha256(src_exe) == _sha256(dst_exe)
        _log(f"校验 exe SHA256 一致：{same}")
        if not same:
            return 6
    except Exception as e:
        _log(f"校验跳过：{e}")

    _log(f"=== 安装完成：{install_dir} ===")

    if launch:
        _launch(dst_exe)
    return 0


def _launch(exe: str) -> None:
    """尝试脱离父进程的作业对象启动，避免随安装器一起被回收"""
    for flags, label in ((CREATE_BREAKAWAY_FROM_JOB | DETACHED_PROCESS, 'breakaway'),
                         (DETACHED_PROCESS, 'detached'),
                         (0, 'plain')):
        try:
            subprocess.Popen([exe], creationflags=flags, close_fds=True,
                             cwd=os.path.dirname(exe))
            _log(f"已启动新版本（{label}）")
            return
        except Exception as e:
            _log(f"启动失败（{label}）：{type(e).__name__}: {e}")
