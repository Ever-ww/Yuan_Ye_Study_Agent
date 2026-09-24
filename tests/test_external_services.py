from __future__ import annotations

import asyncio
import os

import pytest

from Agent.contracts import ModelReply
from Agent.models.providers import AnthropicProvider, OpenAICompatibleProvider


@pytest.mark.external
def test_configured_external_provider_smoke() -> None:
    """Run only when an operator explicitly supplies a real provider endpoint."""
    if os.getenv("YY_RUN_EXTERNAL_TESTS") != "1":
        pytest.skip("set YY_RUN_EXTERNAL_TESTS=1 to run real provider checks")
    base_url = os.getenv("YY_EXTERNAL_BASE_URL")
    api_key = os.getenv("YY_EXTERNAL_API_KEY")
    model = os.getenv("YY_EXTERNAL_MODEL")
    provider_name = os.getenv("YY_EXTERNAL_PROVIDER", "openai")
    if not base_url or not api_key or not model:
        pytest.skip(
            "YY_EXTERNAL_BASE_URL, YY_EXTERNAL_API_KEY and YY_EXTERNAL_MODEL are required",
        )

    if provider_name == "anthropic":
        provider = AnthropicProvider(
            base_url,
            model,
            api_key,
            streaming=False,
            reasoning_effort="none",
        )
    else:
        provider = OpenAICompatibleProvider(
            base_url,
            model,
            api_key,
            streaming=False,
            reasoning_effort="none",
        )
    result = asyncio.run(provider.complete(
        [{"role": "user", "content": "Reply with the single word: pong"}],
        [],
    ))
    assert isinstance(result, ModelReply)
    assert result.text.strip()
