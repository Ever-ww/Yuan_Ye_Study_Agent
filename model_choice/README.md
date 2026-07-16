# model_choice：模型适配层

`model_choice` 为项目提供统一的多模型访问接口，支持 OpenAI、Anthropic、DeepSeek、智谱 GLM、通义千问和 Kimi。同步客户端只依赖 Python 标准库；异步适配器负责把它接入事件驱动的 Agent Harness。

如果你只是使用完整的 `yy-agent`，请先阅读[项目总览](../README.md)和 [Agent 运行时文档](../Agent/README.md)。本页主要面向需要直接调用模型层或开发新 Provider 的维护者。

## 接口选择

| 接口 | 适用场景 | 返回值 | 当前能力 |
| --- | --- | --- | --- |
| `ModelClient` | 简单脚本、旧同步 Agent、Provider 连通性测试 | `ChatResponse` | 文本对话、token 统计、可注入传输层 |
| `LegacyModelProvider` | 新异步 Harness、自定义 Runtime | `ModelOutput` | 异步调用、图片数据模型、原生工具调用及 JSON 后备协议 |
| `FallbackModelProvider` | 主模型请求失败时切换备用模型 | `ModelOutput` | 捕获主 Provider 异常后调用备用 Provider |

`LegacyModelProvider` 的网络请求目前通过 `asyncio.to_thread()` 包装同步客户端。它的 `stream()` 会在完整响应返回后最多产出一个文本块，并非供应商原生的逐 token 流式传输。

## 支持的 Provider

模型既可使用简写别名，也可使用 `provider:model` 指定具体模型。

| 别名 | Provider 名称 | 默认模型 | 默认环境变量 | API 风格 |
| --- | --- | --- | --- | --- |
| `gpt` | `openai` | `gpt-4.1-mini` | `OPENAI_API_KEY` | Responses API |
| `claude` | `anthropic` | `claude-sonnet-4-20250514` | `ANTHROPIC_API_KEY` | Messages API |
| `deepseek` | `deepseek` | `deepseek-chat` | `DEEPSEEK_API_KEY` | Chat Completions |
| `glm` | `zhipu` | `glm-4.5-air` | `ZHIPU_API_KEY` | Chat Completions |
| `qwen` | `qwen` | `qwen-plus` | `DASHSCOPE_API_KEY` | Chat Completions |
| `kimi` | `kimi` | `moonshot-v1-8k` | `MOONSHOT_API_KEY` | Chat Completions |

例如：`qwen` 使用默认模型，`qwen:qwen3-235b-a22b` 使用指定模型。Provider 名称必须使用表中的值；例如 GLM 的完整写法是 `zhipu:glm-4.5-air`，不是 `glm:...`。

## 配置

### 旧同步配置：`config.ini`

从模板创建本地配置：

```powershell
Copy-Item config.ini.example config.ini
$env:DEEPSEEK_API_KEY = "your-api-key"
```

最小配置如下：

```ini
[model_choice]
default_model = deepseek
timeout_seconds = 60
fallback_model =

[providers.deepseek]
base_url = https://api.deepseek.com/v1
api_key_env = DEEPSEEK_API_KEY
```

加载配置文件的优先级为：

1. `ModelClient.from_config(config_path=...)` 显式传入的路径；
2. `MODEL_CHOICE_CONFIG` 环境变量指向的文件；
3. 仓库根目录的 `config.ini`；
4. 文件不存在时使用代码内的默认 Provider 配置。

`[providers.<name>]` 只接受以下覆盖项：

- `base_url`：Provider 或可信企业网关的基础 URL；
- `api_key_env`：读取密钥的环境变量名称；
- `api_key`：明文密钥，仅为旧配置兼容而保留，不推荐使用。

调用 `ModelClient.from_config()` 时，密钥优先级为显式 `api_key` 参数、配置中的 `api_key`、`api_key_env` 对应的环境变量。直接使用 `ModelClient("qwen")` 不会加载 `config.ini` 中的 Provider 覆盖；需要覆盖配置时应使用 `ModelClient.from_config("qwen")`。

`fallback_model` 不由 `ModelClient` 自动执行。旧同步 `Agent` 在达到最大步骤仍未完成时使用它；异步 Harness 则通过 `FallbackModelProvider` 在模型请求抛出异常时切换备用 Provider。

### 新 Harness 配置：`.yy/settings*.json`

完整 Agent Runtime 使用 `.yy/settings.json`、`.yy/settings.local.json` 和用户级 `~/.yy/settings.json`。旧配置可迁移：

```powershell
yy-agent migrate
```

迁移会把非敏感配置写入 `.yy/settings.local.json`；若旧文件包含明文密钥，则需要安装 `keyring` 支持，并将密钥保存到系统凭据存储。完整配置层级和迁移行为见[项目总览](../README.md)。

兼容边界：Harness 的 `model` 为空时，`LegacyModelProvider` 仍会从 `config.ini` 选择默认模型；
Harness 没有提供 providers 覆盖时也会复用 INI 中的连接信息。迁移后应显式设置 `model`。

## 同步调用：`ModelClient`

```python
from model_choice import ChatMessage, ModelClient

client = ModelClient.from_config("deepseek")
response = client.chat(
    [
        ChatMessage("system", "你是一名简洁的学习助手。"),
        ChatMessage("user", "用一句话解释什么是 Agent。"),
    ],
    temperature=0.2,
    max_tokens=512,
)

print(response.content)
print(response.provider, response.model)
print(response.input_tokens, response.output_tokens)
```

消息也可以使用字典：

```python
response = client.chat([
    {"role": "user", "content": "你好"},
])
```

`ChatMessage.role` 支持 `system`、`user` 和 `assistant`。`ChatResponse` 包含：

- `content`：模型生成的文本；
- `model`、`provider`：实际模型和 Provider；
- `input_tokens`、`output_tokens`：供应商返回时可用，否则为 `None`；
- `raw`：未经抽象的完整响应字典，主要用于调试。

同步接口当前不提供工具调用、图片输入或流式输出；这些能力应通过异步 Provider 或 `AgentRuntime` 使用。

## 异步调用：`LegacyModelProvider`

```python
import asyncio

from Agent.types import ModelMessage
from model_choice import FallbackModelProvider, LegacyModelProvider


async def main() -> None:
    primary = LegacyModelProvider("deepseek")
    fallback = LegacyModelProvider("qwen")
    provider = FallbackModelProvider(primary, fallback)

    output = await provider.complete(
        [ModelMessage("user", "用一句话解释什么是 Agent。")],
        tools=[],
        temperature=0,
    )
    print(output.content)
    print(output.provider, output.model)


asyncio.run(main())
```

`complete()` 接受 `list[ModelMessage]` 和工具 Schema 列表，返回 `ModelOutput`。工具 Schema 的形状为：

```python
tools = [
    {
        "name": "current_time",
        "description": "返回当前时间",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }
]
```

当 `tools` 非空时，适配器会先尝试对应 Provider 的原生工具调用；如果网关不兼容或调用失败，则追加严格 JSON 协议后重新请求。工具请求通过 `ModelOutput.tool_calls` 返回，每项包含 `id`、`name` 和 `arguments`。模型层只解析调用，不负责执行工具，也不能绕过 Runtime 的权限审批。

图片使用 `Agent.types.ImageContent` 传入。当前只有原生工具调用路径会实际发送图片；纯文本调用以及 JSON 后备路径会省略图片并附加占位说明。

`FallbackModelProvider` 仅在主 Provider 抛出异常时尝试备用 Provider。它会捕获所有普通 `Exception`，所以自定义 Provider 应避免用异常表示正常的模型决策；若没有备用 Provider，或备用 Provider 也失败，异常会继续向上传播。

## 异常与诊断

| 异常 | 触发条件 |
| --- | --- |
| `ModelChoiceError` | 模型适配层异常基类 |
| `AuthenticationError` | 默认 `*_API_KEY` 环境变量缺失，或服务返回 HTTP 401/403 |
| `ModelAPIError` | 服务返回其他 HTTP 错误；包含 `status_code` |
| `ValueError` | 模型名称、Provider 配置或 INI 内容无效；自定义密钥变量缺失时也可能直接抛出 |

网络连接、超时、JSON 解码以及供应商响应结构错误目前保留 Python 标准库的原始异常。排查时建议依次确认模型名称、环境变量、`base_url`、代理/网络连通性以及供应商返回格式。

`ModelClient` 支持注入 `transport`，便于在测试中替换真实 HTTP 请求。`_post()`、`_native_complete()` 等以下划线开头的方法属于内部实现，不是稳定公开 API。

## 密钥与数据安全

- 优先使用环境变量或系统凭据存储，不要把密钥写进源码、README、命令参数或可提交配置。
- `config.ini` 虽已被 Git 忽略，但其中的 `api_key` 仍是本机明文；提交前仍应检查 `git status` 和暂存区。
- 自定义 `base_url` 会收到对应的认证请求头，只应配置受信任的 HTTPS 服务或企业网关。
- `ChatResponse.raw` 可能包含用户输入、模型输出和供应商元数据，不应默认写入公开日志。
- 怀疑密钥泄露时应立即在供应商控制台撤销并轮换，而不是仅删除本地文件。

配置模板见 [`config.ini.example`](../config.ini.example)，完整 Agent 使用方法见[根 README](../README.md)。
