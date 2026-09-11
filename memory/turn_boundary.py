"""Shared structural protection boundary for compression and Tool previews."""

from typing import Any


def protected_turn_start(
    messages: list[dict[str, Any]], *, current_turn_present: bool = True,
) -> int:
    """Keep the previous full user block and, when present, the current block.

    Provider projections always contain the incoming user. Canonical history
    only contains it after persistence; callers supply that fact explicitly.
    A block includes every assistant/tool message up to the next user.
    """
    starts = [i for i, message in enumerate(messages) if message.get("role") == "user"]
    count = 2 if current_turn_present else 1
    return starts[-count] if len(starts) >= count else 0
