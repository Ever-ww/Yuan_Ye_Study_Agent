"""DREAM-only Harness helpers."""

from harness_runtime.internal_tools import HarnessPreflightTool


def build_tools(trigger):
    return [HarnessPreflightTool(trigger)]
