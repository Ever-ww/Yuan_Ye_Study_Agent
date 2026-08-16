"""供后台 sidecar 使用的最小 Gateway 入口。"""

from __future__ import annotations

import argparse
from pathlib import Path

from Agent import default_agent_root
from gateway.process import run_gateway
from gateway.restart import run_restart_helper


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m gateway")
    subparsers = parser.add_subparsers(dest="command", required=True)
    internal = subparsers.add_parser("run-internal")
    internal.add_argument("--agent-root", type=Path, default=None)
    internal.add_argument("--port", type=int, default=8765)
    restart = subparsers.add_parser("restart-helper")
    restart.add_argument("--agent-root", type=Path, required=True)
    restart.add_argument("--source-root", type=Path, required=True)
    restart.add_argument("--port", type=int, required=True)
    restart.add_argument("--expected-pid", type=int, required=True)
    restart.add_argument("--request-id", required=True)
    restart.add_argument("--expected-commit", required=True)
    restart.add_argument("--timeout", type=float, default=300.0)
    values = parser.parse_args()
    if values.command == "run-internal":
        run_gateway(values.agent_root or default_agent_root(), values.port)
    elif values.command == "restart-helper":
        run_restart_helper(
            values.agent_root, values.source_root, values.port, values.expected_pid,
            values.request_id, values.expected_commit, values.timeout,
        )


if __name__ == "__main__":
    main()
