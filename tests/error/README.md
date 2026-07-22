# 错误复现快照

CLI chat 只有遇到内部代码缺陷或无法规范化的模型响应格式时，才会在本目录生成以 SHA-256 命名的 JSONL。网络、模型服务、配置、认证、权限和普通工具错误不会生成快照。快照包含当时的实际模型请求、Session 审计元数据、工具 Schema、异常栈及 Harness 处理过程。

为避免重复，system/user/assistant/tool 正文只在 `message` 记录中保存一次；`session_audit` 仅补充原 Session 的时间戳、模型指标、工具状态和参数，不重复 content/tool_calls。`incident.user_question_message_index` 指向当前问题对应的 message；只有模型请求尚未形成时才直接保存 `user_question`。

运行快照可能包含用户上下文和本机路径，所有 `*.jsonl` 均由 Git 忽略，不应手动提交。文件不会建立额外索引。
