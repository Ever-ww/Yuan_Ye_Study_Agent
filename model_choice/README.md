# model_choice

统一调用 GPT、Claude、DeepSeek、GLM、Qwen 与 Kimi 的轻量 Python 接口；只使用标准库，不需要安装 SDK。

先将项目根目录的 `config.ini.example` 复制为 `config.ini`，并按需要修改默认模型。实际配置文件已被 Git 忽略；可在供应商区块填写 `base_url` 和本地 `api_key`，也可将 `api_key` 留空并使用 `api_key_env` 环境变量（推荐）。

```ini
[model_choice]
default_model = deepseek
timeout_seconds = 60
```

```python
from model_choice import ModelClient

client = ModelClient.from_config()  # 从 config.ini 加载
response = client.chat([{"role": "user", "content": "你好"}])
print(response.content)
```

临时覆盖模型仍可使用 `ModelClient("qwen")`。配置文件还支持供应商覆盖，适用于企业网关：

```ini
[providers.qwen]
base_url = https://your-gateway.example.com/v1
api_key_env = COMPANY_LLM_API_KEY
```

| 别名 | 环境变量 | 自定义模型写法 |
| --- | --- | --- |
| `gpt` | `OPENAI_API_KEY` | `openai:gpt-4.1` |
| `claude` | `ANTHROPIC_API_KEY` | `anthropic:claude-sonnet-4-20250514` |
| `deepseek` | `DEEPSEEK_API_KEY` | `deepseek:deepseek-reasoner` |
| `glm` | `ZHIPU_API_KEY` | `zhipu:glm-4.5` |
| `qwen` | `DASHSCOPE_API_KEY` | `qwen:qwen3-235b-a22b` |
| `kimi` | `MOONSHOT_API_KEY` | `kimi:moonshot-v1-32k` |

PowerShell 示例：

```powershell
$env:DEEPSEEK_API_KEY = "你的密钥"
python -m model_choice.example
```

接口返回 `ChatResponse`，包含 `content`、`model`、`provider` 和可用时的 token 统计。传入 `api_key=` 可覆盖环境变量，便于测试或多账户场景。
