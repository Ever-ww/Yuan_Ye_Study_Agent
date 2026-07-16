"""旧同步 Agent 使用的零第三方依赖终端界面。

该模块刻意不依赖 Typer/Rich，便于原来的 ``Agent.run()`` 调用方继续使用。不过它没有
持久会话、审批队列、Memory 或 Cron；新代码应使用 :mod:`run_ui.cli`。线程仅用于让
主线程绘制旋转动画，并不会让同步 Agent 获得真正的取消或并行执行能力。
"""

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
    """工作线程与显示线程之间传递结果或异常的最小共享状态。"""

    result: AgentResult | None = None
    error: BaseException | None = None


def run_with_spinner(agent: Agent, task: str) -> AgentResult:
    """在线程中运行同步 Agent，同时在调用线程刷新旋转动画。

    工作线程捕获 ``BaseException`` 是为了把 ``KeyboardInterrupt`` 等异常也带回调用
    线程统一处理；异常不会被吞掉。由于底层 HTTP 请求本身是阻塞的，用户按 Ctrl+C 后
    不能保证请求立刻停止。
    """
    state = _RunState()

    def worker() -> None:
        """在线程中执行同步调用，并把返回值或异常写回共享状态。"""
        try:
            state.result = agent.run(task)
        except BaseException as exc:  # 入口需要将模型和网络异常展示给用户
            state.error = exc

    thread = threading.Thread(target=worker, name="agent-runner", daemon=True)
    thread.start()
    # cycle() 避免维护帧索引；\r 在同一终端行覆盖旧字符，减少输出噪声。
    frames = itertools.cycle("|/-\\")
    while thread.is_alive():
        print(f"\r  {next(frames)} Agent 正在思考与调用工具...", end="", flush=True)
        time.sleep(0.12)
    thread.join()
    print("\r  [OK] Agent 执行结束。              ")

    if state.error:
        # 在调用线程重抛，确保上层 CLI 能决定如何展示错误和设置退出码。
        raise state.error
    if state.result is None:
        raise RuntimeError("Agent 未返回结果")
    return state.result


class DynamicCLI:
    """为旧同步 Agent 提供连续输入循环。

    ``create_agent`` 每轮都会创建新实例，因此这里只复用用户交互循环，不保留模型上下文。
    ``initial_task`` 用于一次性执行：完成一轮后立即返回。
    """

    def __init__(self, create_agent: Callable[[], Agent]) -> None:
        """保存 Agent 工厂，延迟到每次任务真正执行时再创建实例。"""
        self.create_agent = create_agent

    def start(self, initial_task: str | None = None) -> None:
        """启动交互循环，或执行传入的单个初始任务。"""
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
                # 每个任务创建独立旧 Agent，避免一次失败污染后续任务的内部消息列表。
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
        """输出旧 ReAct CLI 的简短启动提示。"""
        print("=" * 52)
        print(" Yuan Ye Study Agent · ReAct CLI")
        print(" 输入 /help 查看命令，输入 /exit 退出")
        print("=" * 52)

    @staticmethod
    def _show_result(result: AgentResult) -> None:
        """展示最终文本以及旧 ReAct 执行器记录的工具 Observation。"""
        print(f"\nAgent > {result.answer}")
        if result.steps:
            print("\n工具轨迹：")
            for step in result.steps:
                print(f"  {step.index}. {step.action}({step.action_input}) -> {step.observation}")
