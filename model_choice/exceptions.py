"""统一异常类型。"""


class ModelChoiceError(Exception):
    """模型适配层基础异常。"""


class AuthenticationError(ModelChoiceError):
    """API 密钥无效或缺失。"""


class ModelAPIError(ModelChoiceError):
    """供应商接口返回非成功状态。"""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"模型服务请求失败（HTTP {status_code}）：{message}")
