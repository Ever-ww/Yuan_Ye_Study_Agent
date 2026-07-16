"""项目源码树中的便捷启动入口。

正式安装后应优先使用 ``yy-agent`` 命令；这个文件主要服务两类场景：

1. 尚未执行 editable install 时，直接用 ``python run.py`` 启动 CLI；
2. 兼容旧项目的 ``python run.py "任务"`` 一次性任务写法。

这里不创建 Runtime，也不处理模型配置，所有实际行为都委托给
``run_ui.cli``，从而避免入口之间出现不同的权限或安全策略。
"""

import sys

try:
    from run_ui.cli import app
except ModuleNotFoundError as exc:
    # 只把“项目声明的可安装依赖缺失”转换成友好提示。其他模块缺失通常代表代码或
    # 安装结构损坏，继续抛出原异常能保留完整堆栈，便于开发者定位问题。
    if exc.name in {"typer", "rich", "yaml", "croniter", "fastapi"}:
        raise SystemExit('缺少运行依赖。请先执行：python -m pip install -e .') from exc
    raise


# Typer 已注册的一级命令。只有第一个参数不属于这些命令、且也不是 ``--help`` 之类
# 的选项时，才把它解释为旧式任务文本并自动补入 ``run`` 子命令。
_COMMANDS = {
    "agent", "auth", "chat", "corpus", "cron", "doctor", "hooks", "lsp",
    "mcp", "memory", "migrate", "plugin", "prompt", "run", "sandbox",
    "scheduler", "serve", "session", "skill", "team",
}


def main() -> None:
    """规范化旧命令行参数后进入统一 Typer 应用。"""
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-") and sys.argv[1] not in _COMMANDS:
        sys.argv.insert(1, "run")
    app()


if __name__ == "__main__":
    main()
