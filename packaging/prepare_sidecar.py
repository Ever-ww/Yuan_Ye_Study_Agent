"""把 PyInstaller 输出复制为 Tauri 要求的 target-triple sidecar 名称。"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


root = Path(__file__).resolve().parents[1]
host = next(
    line.split(":", 1)[1].strip()
    for line in subprocess.check_output(["rustc", "-vV"], text=True, encoding="utf-8").splitlines()
    if line.startswith("host:")
)
suffix = ".exe" if sys.platform == "win32" else ""
source = root / "dist" / f"yy-agent{suffix}"
destination = root / "desktop" / "src-tauri" / "binaries" / f"yy-agent-{host}{suffix}"
destination.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(source, destination)
print(destination)
