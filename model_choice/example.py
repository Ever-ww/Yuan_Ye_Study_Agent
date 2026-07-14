"""运行前请先设置对应的环境变量，例如：$env:OPENAI_API_KEY = '...'。"""

from model_choice import ModelClient

# 默认模型由 config.toml 的 [model_choice] 配置；可传 ModelClient("qwen") 覆盖。
client = ModelClient.from_config()
reply = client.chat([
    {"role": "system", "content": "你是一名简洁的学习助手。"},
    {"role": "user", "content": "用一句话解释什么是 Agent。"},
])
print(reply.content)
