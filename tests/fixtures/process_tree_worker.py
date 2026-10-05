"""Offline process-tree fixture for native platform smoke tests."""

import signal
import subprocess
import sys
import time
from pathlib import Path


if sys.argv[1] == "child":
    if sys.argv[3] == "ignore":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    Path(sys.argv[2]).write_text(str(__import__("os").getpid()), encoding="ascii")
    while True:
        time.sleep(0.1)
else:
    pid_file, mode = sys.argv[2:4]
    subprocess.Popen([sys.executable, __file__, "child", pid_file, mode],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    if mode == "ignore":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.1)
