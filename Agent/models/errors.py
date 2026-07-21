"""模型调用的结构化异常，供重试和 Harness 分类使用。"""

from __future__ import annotations


class ModelError(RuntimeError):
    """所有模型调用异常的公共基类。"""


class ModelNetworkError(ModelError):
    """连接、超时、读写或传输协议层的临时网络故障。"""


class ModelServiceError(ModelError):
    """模型服务返回的非成功 HTTP 状态。"""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def retryable(self) -> bool:
        """只把明确具有临时性的状态视为可重试。"""
        return self.status_code in {408, 429} or 500 <= self.status_code <= 599


class ModelResponseFormatError(ModelError):
    """模型响应已到达，但无法规范化为正式 ModelReply。"""

    def __init__(self, message: str, response_excerpt: str = "") -> None:
        super().__init__(message)
        self.response_excerpt = response_excerpt


def is_retryable_model_error(error: BaseException) -> bool:
    """判断异常是否允许按网络策略重新发起 API 请求。"""
    return isinstance(error, ModelNetworkError) or (
        isinstance(error, ModelServiceError) and error.retryable
    )
