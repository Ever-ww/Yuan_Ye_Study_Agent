"""模型 API 调用的显式重试策略。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelRetryPolicy:
    """每个待完成模型调用独立使用的重试额度。"""

    max_attempts: int = 3
    delay_seconds: float = 2.0

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or self.max_attempts < 1:
            raise ValueError("max_attempts 必须是大于等于 1 的整数")
        if self.delay_seconds < 0:
            raise ValueError("delay_seconds 必须大于等于 0")
