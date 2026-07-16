"""模型适配层对外暴露的稳定异常层次。

调用者可以只捕获 :class:`ModelChoiceError`，而无需依赖 ``urllib`` 或某家 SDK
的异常类型。传输超时等尚未归一化的底层异常会继续透传，以保留完整诊断信息。
"""


class ModelChoiceError(Exception):
    """所有已归一化模型选择/调用错误的基类。"""


class AuthenticationError(ModelChoiceError):
    """API 密钥缺失、无效，或当前凭据没有访问权限。"""


class ModelAPIError(ModelChoiceError):
    """供应商接口返回除认证失败之外的 HTTP 错误。"""

    def __init__(self, status_code: int, message: str) -> None:
        """保存 HTTP 状态码，并生成统一且便于终端显示的错误消息。"""

        self.status_code = status_code
        super().__init__(f"模型服务请求失败（HTTP {status_code}）：{message}")
