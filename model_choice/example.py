"""旧版同步模型客户端的最小可运行示例。

运行前应先设置对应供应商的环境变量，例如在 PowerShell 中执行
``$env:OPENAI_API_KEY = "..."``。本模块把真实网络调用封装在 :func:`main`
中，因此可以被文档工具、测试收集器或交互式环境安全导入；只有直接执行
``python -m model_choice.example`` 时才会向模型供应商发起请求。
"""

from model_choice import ModelClient


def main() -> None:
    """读取本地模型配置，发送一轮示例对话并打印模型回复。"""

    # 默认模型来自 ``config.ini`` 的 ``[model_choice]``；也可以直接构造
    # ``ModelClient("qwen")``，仅覆盖这一次调用使用的模型别名。
    client = ModelClient.from_config()
    reply = client.chat(
        [
            {"role": "system", "content": "你是一名简洁的学习助手。"},
            {"role": "user", "content": "用一句话解释什么是 Agent。"},
        ]
    )
    print(reply.content)


if __name__ == "__main__":
    # 将副作用限制在脚本入口，避免导入示例模块时意外消耗 API 配额。
    main()
