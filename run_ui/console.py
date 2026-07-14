"""无需第三方依赖的动态终端界面。"""

from __future__ import annotations

import itertools
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from Agent import Agent, AgentResult


@dataclass
class _RunState:
    result: AgentResult | None = None
    error: BaseException | None = None


def run_with_spinner(agent: Agent, task: str) -> AgentResult:
    """后台执行 Agent，并在前台持续显示运行动画。"""
    state = _RunState()

    def worker() -> None:
        try:
            state.result = agent.run(task)
        except BaseException as exc:  # 入口需要将模型和网络异常展示给用户
            state.error = exc

    thread = threading.Thread(target=worker, name="agent-runner", daemon=True)
    thread.start()
    frames = itertools.cycle("|/-\\")
    while thread.is_alive():
        print(f"\r  {next(frames)} Agent 正在思考与调用工具...", end="", flush=True)
        time.sleep(0.12)
    thread.join()
    print("\r  [OK] Agent 执行结束。              ")

    if state.error:
        raise state.error
    if state.result is None:
        raise RuntimeError("Agent 未返回结果")
    return state.result


class DynamicCLI:
    """支持连续提问的终端 UI。输入 /help 或 /exit 查看和结束会话。"""

    def __init__(self, create_agent: Callable[[], Agent]) -> None:
        self.create_agent = create_agent

    def start(self, initial_task: str | None = None) -> None:
        self._banner()
        task = initial_task
        while True:
            if task is None:
                try:
                    task = input("\n你 > ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\n会话已结束。")
                    return
            if not task:
                task = None
                continue
            if task in {"/exit", "/quit"}:
                print("会话已结束。")
                return
            if task == "/help":
                print("输入任务后回车执行；/exit 退出；/help 显示本帮助。")
                task = None
                continue

            try:
                result = run_with_spinner(self.create_agent(), task)
                self._show_result(result)
            except KeyboardInterrupt:
                print("\n已请求中断；当前网络请求可能仍需等待返回。")
            except Exception as exc:
                print(f"\n运行失败：{exc}", file=sys.stderr)
            if initial_task is not None:
                return
            task = None

    @staticmethod
    def _banner() -> None:
        print("=" * 52)
        print(" Yuan Ye Study Agent · ReAct CLI")
        print(" 输入 /help 查看命令，输入 /exit 退出")
        print("=" * 52)

    @staticmethod
    def _show_result(result: AgentResult) -> None:
        print(f"\nAgent > {result.answer}")
        if result.steps:
            print("\n工具轨迹：")
            for step in result.steps:
                print(f"  {step.index}. {step.action}({step.action_input}) -> {step.observation}")
