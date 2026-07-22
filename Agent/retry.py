"""模型 API 调用的显式重试策略。"""

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class ModelRetryPolicy(BaseModel):
    """每个待完成模型调用独立使用的重试额度。"""

    model_config = ConfigDict(frozen=True, strict=True)

    max_attempts: StrictInt = Field(default=3, ge=1)
    delay_seconds: float = Field(default=2.0, ge=0.0)
