"""ACP fixture that leaves a sleeping child for lifecycle cleanup to reap."""

import runpy
import subprocess
import sys
from pathlib import Path


pid_file = sys.argv[1]
child = subprocess.Popen(
    [sys.executable, str(Path(__file__).with_name("process_tree_worker.py")),
     "child", pid_file, "ignore"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
Path(pid_file).write_text(str(child.pid), encoding="ascii")
sys.argv = [str(Path(__file__).with_name("fake_acp_worker.py"))]
runpy.run_path(sys.argv[0], run_name="__main__")
