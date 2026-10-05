"""Own and reap local subprocess trees started by Agent Shuttle."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess


def _windows_process_parents() -> dict[int, int]:
    """Read one native process snapshot without invoking an external command."""
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    parents = {}
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        if not found and ctypes.get_last_error() != 18:
            raise ctypes.WinError(ctypes.get_last_error())
        while found:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return parents


def owns_pid(owner_pid: int, reported_pid: int) -> bool:
    """Accept a Windows venv launcher's Python child as the server process."""
    if owner_pid == reported_pid:
        return True
    if os.name != "nt":
        return False
    parents = _windows_process_parents()
    current = reported_pid
    for _ in range(32):
        current = parents.get(current)
        if current == owner_pid:
            return True
        if current is None or current == 0:
            return False
    return False


def _terminate_windows_tree(pid: int) -> None:
    """Snapshot descendants, then terminate leaves before their owner."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    parents = _windows_process_parents()
    descendants = []
    frontier = [pid]
    while frontier:
        parent = frontier.pop()
        children = [child for child, owner in parents.items() if owner == parent and child != parent]
        descendants.extend(children)
        frontier.extend(children)
    for child in [*reversed(descendants), pid]:
        handle = kernel.OpenProcess(0x0001 | 0x100000, False, child)
        if not handle:
            if child == pid or ctypes.get_last_error() != 87:
                raise ctypes.WinError(ctypes.get_last_error())
            continue
        try:
            if kernel.WaitForSingleObject(handle, 0) == 0x102:
                if not kernel.TerminateProcess(handle, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
                if kernel.WaitForSingleObject(handle, 5000) != 0:
                    raise TimeoutError(f"PID {child} did not exit after termination")
        finally:
            kernel.CloseHandle(handle)


def spawn_options() -> dict:
    """Place each owned child in its own process group."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


async def stop_async_process(process, *, grace: float = 5) -> None:
    """Stop an asyncio child and its descendants; safe after an earlier stop."""
    if process.returncode is not None:
        if os.name != "nt" and isinstance(getattr(process, "pid", None), int):
            _signal_group(process.pid, signal.SIGTERM)
            _signal_group(process.pid, signal.SIGKILL)
        await process.wait()
        return
    if not isinstance(getattr(process, "pid", None), int):
        process.kill()
        await process.wait()
        return
    if os.name == "nt":
        await asyncio.to_thread(_terminate_windows_tree, process.pid)
    else:
        _signal_group(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=grace)
    except asyncio.TimeoutError:
        if os.name != "nt":
            _signal_group(process.pid, signal.SIGKILL)
        else:
            process.kill()
        await process.wait()
    finally:
        if os.name != "nt":
            # The leader can exit while a descendant ignores SIGTERM.
            _signal_group(process.pid, signal.SIGKILL)


async def stop_sync_process(process: subprocess.Popen, *, grace: float = 5) -> None:
    """Stop a Popen child and its descendants without losing its process handle."""
    if process.poll() is not None:
        if os.name != "nt" and type(process).__module__ == "subprocess":
            _signal_group(process.pid, signal.SIGTERM)
            _signal_group(process.pid, signal.SIGKILL)
        await asyncio.to_thread(process.wait)
        return
    if type(process).__module__ != "subprocess":
        process.terminate()
        await asyncio.to_thread(process.wait, timeout=grace)
        return
    if os.name == "nt":
        await asyncio.to_thread(_terminate_windows_tree, process.pid)
    else:
        _signal_group(process.pid, signal.SIGTERM)
    try:
        await asyncio.to_thread(process.wait, timeout=grace)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            _signal_group(process.pid, signal.SIGKILL)
        else:
            process.kill()
        await asyncio.to_thread(process.wait)
    finally:
        if os.name != "nt":
            _signal_group(process.pid, signal.SIGKILL)
