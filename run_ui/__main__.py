"""支持 ``python -m run_ui`` 的模块执行入口。"""

from .cli import app


if __name__ == "__main__":
    # Typer 会读取 sys.argv 并负责命令分派、帮助文本和退出码。
    app()
