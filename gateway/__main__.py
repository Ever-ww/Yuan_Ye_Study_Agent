"""供后台 sidecar 使用的最小 Gateway 入口。"""

from __future__ import annotations

import argparse
from pathlib import Path

from Agent import default_agent_root
from gateway.process import run_gateway


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m gateway")
    subparsers = parser.add_subparsers(dest="command", required=True)
    internal = subparsers.add_parser("run-internal")
    internal.add_argument("--agent-root", type=Path, default=None)
    internal.add_argument("--port", type=int, default=8765)
    values = parser.parse_args()
    if values.command == "run-internal":
        run_gateway(values.agent_root or default_agent_root(), values.port)


if __name__ == "__main__":
    main()
