"""根据 RuntimeConfig 构建可选的 OpenAI-compatible Embedding 客户端。"""

from __future__ import annotations

from typing import Any

from .embeddings import OpenAIEmbeddingProvider


def build_embedding_provider(config: Any) -> OpenAIEmbeddingProvider | None:
    model = str(getattr(config, "reference_embedding_model", "") or "").strip()
    if not model:
        return None
    base_url = str(
        getattr(config, "reference_embedding_base_url", None)
        or getattr(config, "base_url", None)
        or ""
    ).strip()
    api_key = str(
        getattr(config, "reference_embedding_api_key", None)
        or getattr(config, "api_key", None)
        or ""
    ).strip()
    if not base_url or not api_key:
        return None
    return OpenAIEmbeddingProvider(
        base_url,
        api_key,
        model,
        use_system_proxy=bool(getattr(config, "use_system_proxy", False)),
        proxy_url=getattr(config, "proxy_url", None),
    )
