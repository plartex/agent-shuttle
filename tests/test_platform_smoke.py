"""Native, offline process and authenticated server smoke tests."""

import asyncio
import ctypes
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent_shuttle import HarnessLaunch, connect_harness
from agent_shuttle.client import BridgeClient
from agent_shuttle.managed import HarnessConfigurationMismatch
from agent_shuttle.process_lifecycle import spawn_options, stop_async_process, stop_sync_process


FIXTURE = Path(__file__).parent / "fixtures" / "process_tree_worker.py"
ACP_TREE_FIXTURE = Path(__file__).parent / "fixtures" / "acp_tree_worker.py"


def _alive(pid: int) -> bool:
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x102
        finally:
            kernel.CloseHandle(handle)
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                            capture_output=True, text=True, check=False)
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


async def _gone(pid: int) -> None:
    for _ in range(50):
        if not _alive(pid):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"PID {pid} survived process-tree cleanup")


class PlatformSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_failure_is_reported(self):
        process = SimpleNamespace(pid=123, returncode=None, wait=AsyncMock())
        target = ("agent_shuttle.process_lifecycle._terminate_windows_tree" if os.name == "nt"
                  else "agent_shuttle.process_lifecycle._signal_group")
        with patch(target, side_effect=OSError("cleanup denied")):
            with self.assertRaisesRegex(OSError, "cleanup denied"):
                await stop_async_process(process)

    async def test_process_tree_terminates_and_stop_is_idempotent(self):
        for mode in ("normal", "ignore"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                pid_file = Path(folder) / "child.pid"
                process = subprocess.Popen(
                    [sys.executable, str(FIXTURE), "parent", str(pid_file), mode],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, **spawn_options(),
                )
                try:
                    for _ in range(50):
                        if pid_file.exists():
                            break
                        await asyncio.sleep(0.1)
                    self.assertTrue(pid_file.exists())
                    child_pid = int(pid_file.read_text(encoding="ascii"))
                    self.assertTrue(_alive(process.pid))
                    self.assertTrue(_alive(child_pid))
                finally:
                    await stop_sync_process(process, grace=0.2)
                await _gone(process.pid)
                await _gone(child_pid)
                await stop_sync_process(process, grace=0.2)

    async def test_authenticated_server_identity_and_foreign_workspace(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / "workspace"
            foreign = root / "foreign"
            workspace.mkdir()
            foreign.mkdir()
            profile = root / "profile.json"
            child_pid_file = root / "worker-child.pid"
            profile.write_text(json.dumps({
                "id": "smoke", "runtime": "acp", "workspace": str(workspace),
                "command": [sys.executable, str(ACP_TREE_FIXTURE), str(child_pid_file)],
            }), encoding="utf-8")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            url = f"http://127.0.0.1:{port}"
            launch = HarnessLaunch("acp", url, workspace, profile_path=profile)
            client = BridgeClient()
            async with connect_harness(launch, client=client) as connection:
                self.assertTrue(connection.started)
                identity = await client.identity(url)
                self.assertEqual(identity["backend"], "acp")
                self.assertTrue(Path(identity["workspace"]).samefile(workspace))
                self.assertIsInstance(identity["pid"], int)
                await client.info(url)
                self.assertTrue(child_pid_file.exists())
                await _gone(int(child_pid_file.read_text(encoding="ascii")))
                with self.assertRaises(HarnessConfigurationMismatch):
                    async with connect_harness(HarnessLaunch(
                        "acp", url, foreign, profile_path=profile,
                        start_if_missing=False,
                    ), client=client):
                        pass
                self.assertEqual((await client.identity(url))["pid"], identity["pid"])
            await _gone(identity["pid"])
            with socket.socket() as probe:
                self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)
