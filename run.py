"""Agent 项目启动入口。运行前设置对应模型 API Key。"""

from __future__ import annotations

import argparse

from Agent import Agent, AgentConfig, CalculatorTool, CurrentTimeTool
from run_ui import DynamicCLI


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 ReAct Agent")
    parser.add_argument("task", nargs="?", help="需要 Agent 完成的任务")
    parser.add_argument("--model", help="临时覆盖 config.ini 中的默认模型")
    parser.add_argument("--max-steps", type=int, default=20, help="最大工具调用轮数")
    args = parser.parse_args()

    def create_agent() -> Agent:
        return Agent(
            system_prompt="你是一个严谨、简洁的学习助手。",
            tools=[CalculatorTool(), CurrentTimeTool()],
            model=args.model,
            config=AgentConfig(max_steps=args.max_steps),
        )

    DynamicCLI(create_agent).start(args.task)


if __name__ == "__main__":
    main()
