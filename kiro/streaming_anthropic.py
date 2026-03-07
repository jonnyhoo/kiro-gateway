# -*- coding: utf-8 -*-

# Kiro Gateway
# https://github.com/jwadow/kiro-gateway
# Copyright (C) 2025 Jwadow
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
Streaming logic for converting Kiro stream to Anthropic Messages API format.

This module formats Kiro events into Anthropic SSE format:
- event: message_start
- event: content_block_start
- event: content_block_delta
- event: content_block_stop
- event: message_delta
- event: message_stop

Reference: https://docs.anthropic.com/en/api/messages-streaming
"""

import inspect
import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncGenerator, Dict, List, Optional, Any

import httpx
from loguru import logger

from kiro.streaming_core import (
    parse_kiro_stream,
    collect_stream_to_result,
    FirstTokenTimeoutError,
    calculate_tokens_from_context_usage,
    stream_with_first_token_retry,
)
from kiro.tokenizer import count_tokens, count_message_tokens
from kiro.parsers import parse_bracket_tool_calls
from kiro.config import (
    FIRST_TOKEN_TIMEOUT,
    FIRST_TOKEN_MAX_RETRIES,
    FAKE_REASONING_HANDLING,
)

if TYPE_CHECKING:
    from kiro.auth import KiroAuthManager
    from kiro.cache import ModelInfoCache

# Import debug_logger for logging
try:
    from kiro.debug_logger import debug_logger
except ImportError:
    debug_logger = None


def generate_message_id() -> str:
    """Generate unique message ID in Anthropic format."""
    return f"msg_{uuid.uuid4().hex[:24]}"


def format_sse_event(event_type: str, data: Dict[str, Any]) -> str:
    """
    Format data as Anthropic SSE event.

    Anthropic SSE format:
    event: {event_type}
    data: {json_data}

    Args:
        event_type: Event type (message_start, content_block_delta, etc.)
        data: Event data dictionary

    Returns:
        Formatted SSE string
    """
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def generate_thinking_signature() -> str:
    """
    Generate a placeholder signature for thinking content blocks.

    In real Anthropic API, this is a cryptographic signature for verification.
    Since we're using fake reasoning via tag injection, we generate a placeholder.

    Returns:
        Placeholder signature string
    """
    return f"sig_{uuid.uuid4().hex[:32]}"


def _extract_text_from_blocks(content_blocks: List[Dict[str, Any]]) -> str:
    """Concatenate Anthropic text blocks into one visible text string."""
    parts = []
    for block in content_blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def _truncate_text_blocks(
    content_blocks: List[Dict[str, Any]], char_limit: int
) -> List[Dict[str, Any]]:
    """Trim Anthropic text blocks to a character boundary while preserving non-text blocks."""
    truncated_blocks: List[Dict[str, Any]] = []
    remaining = max(0, char_limit)

    for block in content_blocks:
        if not isinstance(block, dict) or block.get("type") != "text":
            truncated_blocks.append(block)
            continue

        if remaining <= 0:
            continue

        text = block.get("text", "")
        if len(text) <= remaining:
            truncated_blocks.append(block)
            remaining -= len(text)
            continue

        updated_block = dict(block)
        updated_block["text"] = text[:remaining]
        truncated_blocks.append(updated_block)
        remaining = 0

    return truncated_blocks


def _find_earliest_stop_sequence(
    text: str, stop_sequences: Optional[List[str]]
) -> Optional[tuple[int, str]]:
    """Return the earliest matching stop sequence in visible text."""
    earliest_match: Optional[tuple[int, str]] = None

    for sequence in stop_sequences or []:
        if not sequence:
            continue
        index = text.find(sequence)
        if index < 0:
            continue
        if earliest_match is None or index < earliest_match[0]:
            earliest_match = (index, sequence)

    return earliest_match


def _truncate_text_to_token_limit(text: str, max_tokens: int) -> tuple[str, bool]:
    """Trim text to the largest prefix that stays within the requested token budget."""
    if max_tokens <= 0:
        return "", bool(text)

    if count_tokens(text) <= max_tokens:
        return text, False

    low = 0
    high = len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if count_tokens(text[:mid]) <= max_tokens:
            low = mid
        else:
            high = mid - 1

    truncated_text = text[:low]
    while truncated_text and count_tokens(truncated_text) > max_tokens:
        truncated_text = truncated_text[:-1]

    return truncated_text, True


def _get_stop_sequence_holdback(stop_sequences: Optional[List[str]]) -> int:
    """Keep enough trailing chars to detect cross-chunk stop sequences."""
    non_empty_lengths = [len(sequence) for sequence in stop_sequences or [] if sequence]
    if not non_empty_lengths:
        return 0
    return max(non_empty_lengths) - 1


def _truncate_appended_text_to_token_limit(
    existing_text: str, appended_text: str, max_tokens: Optional[int]
) -> tuple[str, bool]:
    """Fit appended text within a total token budget against already-emitted text."""
    if max_tokens is None:
        return appended_text, False

    existing_tokens = count_tokens(existing_text)
    if existing_tokens >= max_tokens:
        return "", True

    combined_text = existing_text + appended_text
    combined_tokens = count_tokens(combined_text)
    if combined_tokens < max_tokens:
        return appended_text, False
    if combined_tokens == max_tokens:
        return appended_text, True

    low = 0
    high = len(appended_text)
    while low < high:
        mid = (low + high + 1) // 2
        if count_tokens(existing_text + appended_text[:mid]) <= max_tokens:
            low = mid
        else:
            high = mid - 1

    truncated_text = appended_text[:low]
    while truncated_text and count_tokens(existing_text + truncated_text) > max_tokens:
        truncated_text = truncated_text[:-1]

    return truncated_text, True


def _calculate_text_overlap(existing_text: str, continuation_text: str) -> int:
    """Find the longest suffix/prefix overlap for stitched continuation text."""
    max_overlap = min(len(existing_text), len(continuation_text), 512)
    for overlap in range(max_overlap, 0, -1):
        if existing_text[-overlap:] == continuation_text[:overlap]:
            return overlap
    return 0


def _stitch_continued_text(
    existing_text: str, continuation_text: str
) -> tuple[str, int]:
    """Stitch continuation text while dropping duplicated overlap."""
    overlap = _calculate_text_overlap(existing_text, continuation_text)
    return existing_text + continuation_text[overlap:], overlap


def _count_unescaped_quotes(text: str, quote: str = '"') -> int:
    """Count non-escaped quote characters in a string."""
    count = 0
    escaped = False
    for char in text:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == quote:
            count += 1
    return count


def _text_looks_incomplete_for_continuation(text: str) -> bool:
    """Detect obviously incomplete text tails worth hidden continuation."""
    stripped = (text or "").rstrip()
    if not stripped:
        return False

    tail = stripped[-1200:]
    if tail.count("```") % 2 == 1:
        return True
    if _count_unescaped_quotes(tail) % 2 == 1:
        return True

    for opener, closer in (("(", ")"), ("[", "]"), ("{", "}")):
        if tail.count(opener) > tail.count(closer):
            return True

    last_line = tail.splitlines()[-1].strip()
    if not last_line:
        return False

    if last_line.startswith(
        (
            "export ",
            "import ",
            "const ",
            "let ",
            "var ",
            "function ",
            "class ",
            "interface ",
            "type ",
            "enum ",
        )
    ) and not last_line.endswith((";", "{", "}", ")", "]")):
        return True

    return last_line[-1] in {"(", "[", "{", ",", ":", "\\", "+", "-", "*", "/", "="}


def _plan_stream_text_controls(
    emitted_text: str,
    pending_text: str,
    stop_sequences: Optional[List[str]],
    requested_max_tokens: Optional[int],
    is_final: bool,
) -> tuple[str, str, Optional[str], Optional[str]]:
    """
    Decide how much text can be emitted now and whether the stream should end.

    Returns:
        (emit_text, remaining_buffer, stop_reason, stop_sequence)
    """
    if not pending_text:
        return "", "", None, None

    stop_match = _find_earliest_stop_sequence(pending_text, stop_sequences)
    token_limited_text, token_limit_hit = _truncate_appended_text_to_token_limit(
        emitted_text, pending_text, requested_max_tokens
    )

    if stop_match and (not token_limit_hit or stop_match[0] <= len(token_limited_text)):
        return pending_text[: stop_match[0]], "", "stop_sequence", stop_match[1]

    if token_limit_hit:
        return token_limited_text, "", "max_tokens", None

    safe_emit_length = len(pending_text)
    if not is_final:
        safe_emit_length = max(
            0,
            len(pending_text) - _get_stop_sequence_holdback(stop_sequences),
        )

    return (
        pending_text[:safe_emit_length],
        pending_text[safe_emit_length:],
        None,
        None,
    )


def _apply_non_streaming_output_controls(
    content_blocks: List[Dict[str, Any]],
    requested_max_tokens: Optional[int],
    stop_sequences: Optional[List[str]],
) -> tuple[List[Dict[str, Any]], Optional[str], Optional[str]]:
    """
    Apply Anthropic-compatible response-side controls without touching tool calls.

    Kiro ignores Anthropic stop controls upstream. For plain-text non-streaming
    responses we can still enforce them locally and keep tool flows untouched.
    """
    if any(
        isinstance(block, dict) and block.get("type") == "tool_use"
        for block in content_blocks
    ):
        return content_blocks, None, None

    visible_text = _extract_text_from_blocks(content_blocks)
    if not visible_text:
        return content_blocks, None, None

    stop_match = _find_earliest_stop_sequence(visible_text, stop_sequences)
    max_token_cut: Optional[int] = None

    if requested_max_tokens is not None:
        truncated_text, was_truncated = _truncate_text_to_token_limit(
            visible_text, requested_max_tokens
        )
        if was_truncated:
            max_token_cut = len(truncated_text)

    stop_reason = None
    stop_sequence = None
    final_char_limit: Optional[int] = None

    if stop_match and (max_token_cut is None or stop_match[0] <= max_token_cut):
        final_char_limit = stop_match[0]
        stop_reason = "stop_sequence"
        stop_sequence = stop_match[1]
    elif max_token_cut is not None:
        final_char_limit = max_token_cut
        stop_reason = "max_tokens"

    if final_char_limit is None:
        return content_blocks, None, None

    return (
        _truncate_text_blocks(content_blocks, final_char_limit),
        stop_reason,
        stop_sequence,
    )


@dataclass
class StreamingAutoContinuation:
    """Opaque continuation request state for internal streaming recovery."""

    response: httpx.Response
    request_messages: Optional[list] = None


async def _maybe_aclose_response(response: Any) -> None:
    """Close upstream responses when they expose a close hook."""
    close_method = getattr(response, "aclose", None)
    if not callable(close_method):
        return

    close_result = close_method()
    if inspect.isawaitable(close_result):
        await close_result


async def stream_kiro_to_anthropic(
    response: httpx.Response,
    model: str,
    model_cache: "ModelInfoCache",
    auth_manager: "KiroAuthManager",
    first_token_timeout: float = FIRST_TOKEN_TIMEOUT,
    request_messages: Optional[list] = None,
    conversation_id: Optional[str] = None,
    prompt_cache_usage: Optional[dict] = None,
    requested_max_tokens: Optional[int] = None,
    stop_sequences: Optional[List[str]] = None,
    enable_local_text_controls: bool = False,
    auto_continue_callback=None,
    max_auto_continuation_rounds: int = 0,
) -> AsyncGenerator[str, None]:
    """
    Generator for converting Kiro stream to Anthropic SSE format.

    Parses Kiro AWS SSE stream and converts events to Anthropic format.
    Supports thinking content blocks when FAKE_REASONING_HANDLING=as_reasoning_content.

    Args:
        response: HTTP response with data stream
        model: Model name to include in response
        model_cache: Model cache for getting token limits
        auth_manager: Authentication manager
        first_token_timeout: First token wait timeout (seconds)
        request_messages: Original request messages (for token counting)
        conversation_id: Stable conversation ID for truncation recovery (optional)

    Yields:
        Strings in Anthropic SSE format

    Raises:
        FirstTokenTimeoutError: If first token not received within timeout
    """
    message_id = generate_message_id()
    input_tokens = 0
    output_tokens = 0
    full_content = ""
    full_thinking_content = ""
    emitted_content = ""
    pending_text = ""
    local_stop_reason: Optional[str] = None
    local_stop_sequence: Optional[str] = None
    terminated_early = False

    # Count input tokens from request messages
    if request_messages:
        input_tokens = count_message_tokens(
            request_messages, apply_claude_correction=False
        )

    # Track content blocks - thinking block is index 0, text block is index 1 (when thinking enabled)
    current_block_index = 0
    thinking_block_started = False
    thinking_block_index: Optional[int] = None
    text_block_started = False
    text_block_index: Optional[int] = None
    tool_blocks: List[Dict[str, Any]] = []

    # Generate signature for thinking block (used if thinking is present)
    thinking_signature = generate_thinking_signature()

    # Track context usage for token calculation
    context_usage_percentage: Optional[float] = None

    # Track truncated tool calls for recovery
    truncated_tools: List[Dict[str, Any]] = []
    current_response = response
    recovery_round = 0

    try:
        # Send message_start event
        yield format_sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": input_tokens, "output_tokens": 0}
                    | (prompt_cache_usage or {}),
                },
            },
        )

        while True:
            segment_is_continuation = recovery_round > 0
            segment_context_usage_percentage: Optional[float] = None
            segment_content_buffer = ""
            segment_added_content = False

            async for event in parse_kiro_stream(current_response, first_token_timeout):
                if segment_is_continuation:
                    if event.type == "content":
                        segment_content_buffer += event.content or ""
                    elif (
                        event.type == "context_usage"
                        and event.context_usage_percentage is not None
                    ):
                        context_usage_percentage = event.context_usage_percentage
                        segment_context_usage_percentage = (
                            event.context_usage_percentage
                        )
                    elif event.type == "thinking":
                        # Hidden continuation thinking should not leak into the
                        # visible stream or distort output-token accounting.
                        continue
                    elif event.type == "tool_use" and event.tool_use:
                        logger.warning(
                            "Auto-continuation produced unexpected tool_use; "
                            "ignoring continuation tool block"
                        )
                    continue

                if event.type == "content":
                    content = event.content or ""
                    full_content += content
                    segment_added_content = segment_added_content or bool(content)
                    pending_text += content

                    # Close thinking block if it was open and we're now getting regular content
                    if thinking_block_started and thinking_block_index is not None:
                        yield format_sse_event(
                            "content_block_stop",
                            {
                                "type": "content_block_stop",
                                "index": thinking_block_index,
                            },
                        )
                        thinking_block_started = False
                        current_block_index += 1

                    # Start text block if not started
                    if pending_text and not text_block_started:
                        text_block_index = current_block_index
                        yield format_sse_event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": text_block_index,
                                "content_block": {"type": "text", "text": ""},
                            },
                        )
                        text_block_started = True

                    emit_text = pending_text
                    remaining_text = ""
                    stop_reason = None
                    stop_sequence = None
                    if enable_local_text_controls:
                        (
                            emit_text,
                            remaining_text,
                            stop_reason,
                            stop_sequence,
                        ) = _plan_stream_text_controls(
                            emitted_text=emitted_content,
                            pending_text=pending_text,
                            stop_sequences=stop_sequences,
                            requested_max_tokens=requested_max_tokens,
                            is_final=False,
                        )

                    if emit_text:
                        yield format_sse_event(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": text_block_index,
                                "delta": {"type": "text_delta", "text": emit_text},
                            },
                        )
                        emitted_content += emit_text

                    pending_text = remaining_text
                    if stop_reason:
                        local_stop_reason = stop_reason
                        local_stop_sequence = stop_sequence
                        terminated_early = True
                        break

                elif event.type == "thinking":
                    thinking_content = event.thinking_content or ""
                    full_thinking_content += thinking_content

                    # Handle thinking content based on mode
                    if FAKE_REASONING_HANDLING == "as_reasoning_content":
                        # Use native Anthropic thinking content blocks
                        if not thinking_block_started:
                            thinking_block_index = current_block_index
                            yield format_sse_event(
                                "content_block_start",
                                {
                                    "type": "content_block_start",
                                    "index": thinking_block_index,
                                    "content_block": {
                                        "type": "thinking",
                                        "thinking": "",
                                        "signature": thinking_signature,
                                    },
                                },
                            )
                            thinking_block_started = True

                        if thinking_content:
                            yield format_sse_event(
                                "content_block_delta",
                                {
                                    "type": "content_block_delta",
                                    "index": thinking_block_index,
                                    "delta": {
                                        "type": "thinking_delta",
                                        "thinking": thinking_content,
                                    },
                                },
                            )

                    elif FAKE_REASONING_HANDLING == "include_as_text":
                        # Include thinking as regular text content
                        # Close thinking block if it was open (shouldn't happen in this mode)
                        if thinking_block_started and thinking_block_index is not None:
                            yield format_sse_event(
                                "content_block_stop",
                                {
                                    "type": "content_block_stop",
                                    "index": thinking_block_index,
                                },
                            )
                            thinking_block_started = False
                            current_block_index += 1

                        # Start text block if not started
                        if not text_block_started:
                            text_block_index = current_block_index
                            yield format_sse_event(
                                "content_block_start",
                                {
                                    "type": "content_block_start",
                                    "index": text_block_index,
                                    "content_block": {"type": "text", "text": ""},
                                },
                            )
                            text_block_started = True

                        if thinking_content:
                            yield format_sse_event(
                                "content_block_delta",
                                {
                                    "type": "content_block_delta",
                                    "index": text_block_index,
                                    "delta": {
                                        "type": "text_delta",
                                        "text": thinking_content,
                                    },
                                },
                            )
                    # For "strip" mode, we just skip the thinking content

                elif event.type == "tool_use" and event.tool_use:
                    if pending_text:
                        emit_text = pending_text
                        stop_reason = None
                        stop_sequence = None
                        if enable_local_text_controls:
                            emit_text, pending_text, stop_reason, stop_sequence = (
                                _plan_stream_text_controls(
                                    emitted_text=emitted_content,
                                    pending_text=pending_text,
                                    stop_sequences=stop_sequences,
                                    requested_max_tokens=requested_max_tokens,
                                    is_final=True,
                                )
                            )

                        if (
                            emit_text
                            and text_block_started
                            and text_block_index is not None
                        ):
                            yield format_sse_event(
                                "content_block_delta",
                                {
                                    "type": "content_block_delta",
                                    "index": text_block_index,
                                    "delta": {"type": "text_delta", "text": emit_text},
                                },
                            )
                            emitted_content += emit_text

                        pending_text = ""
                        if stop_reason:
                            local_stop_reason = stop_reason
                            local_stop_sequence = stop_sequence
                            terminated_early = True
                            break

                    # Close thinking block if open
                    if thinking_block_started and thinking_block_index is not None:
                        yield format_sse_event(
                            "content_block_stop",
                            {
                                "type": "content_block_stop",
                                "index": thinking_block_index,
                            },
                        )
                        thinking_block_started = False
                        current_block_index += 1

                    # Close text block if open
                    if text_block_started and text_block_index is not None:
                        yield format_sse_event(
                            "content_block_stop",
                            {"type": "content_block_stop", "index": text_block_index},
                        )
                        text_block_started = False
                        current_block_index += 1

                    tool = event.tool_use
                    tool_id = tool.get("id") or f"toolu_{uuid.uuid4().hex[:24]}"
                    tool_name = tool.get("function", {}).get("name", "") or tool.get(
                        "name", ""
                    )
                    tool_input = tool.get("function", {}).get(
                        "arguments", {}
                    ) or tool.get("input", {})

                    # Check if this tool was truncated
                    if tool.get("_truncation_detected"):
                        truncated_tools.append(
                            {
                                "id": tool_id,
                                "name": tool_name,
                                "truncation_info": tool.get("_truncation_info", {}),
                            }
                        )

                    # Parse arguments if string
                    if isinstance(tool_input, str):
                        try:
                            tool_input = json.loads(tool_input)
                        except json.JSONDecodeError:
                            tool_input = {}

                    # Send tool_use block start
                    yield format_sse_event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": current_block_index,
                            "content_block": {
                                "type": "tool_use",
                                "id": tool_id,
                                "name": tool_name,
                                "input": {},
                            },
                        },
                    )

                    # Send tool input as delta
                    input_json = json.dumps(tool_input, ensure_ascii=False)
                    yield format_sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": current_block_index,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": input_json,
                            },
                        },
                    )

                    # Close tool block
                    yield format_sse_event(
                        "content_block_stop",
                        {"type": "content_block_stop", "index": current_block_index},
                    )

                    tool_blocks.append(
                        {"id": tool_id, "name": tool_name, "input": tool_input}
                    )
                    current_block_index += 1

                elif (
                    event.type == "context_usage"
                    and event.context_usage_percentage is not None
                ):
                    context_usage_percentage = event.context_usage_percentage
                    segment_context_usage_percentage = event.context_usage_percentage

            if segment_is_continuation and segment_content_buffer:
                stitched_content, overlap = _stitch_continued_text(
                    full_content, segment_content_buffer
                )
                continuation_delta = stitched_content[len(full_content) :]
                full_content = stitched_content
                pending_text += continuation_delta
                segment_added_content = bool(continuation_delta)
                if overlap:
                    logger.debug(
                        "Auto-continuation stitched duplicated prefix with "
                        f"{overlap} overlapping chars"
                    )

            segment_was_truncated = (
                not terminated_early
                and segment_context_usage_percentage is None
                and segment_added_content
                and not tool_blocks
            )
            segment_looks_incomplete = (
                not tool_blocks
                and _text_looks_incomplete_for_continuation(full_content)
            )

            can_auto_continue = (
                (segment_was_truncated or segment_looks_incomplete)
                and callable(auto_continue_callback)
                and recovery_round < max_auto_continuation_rounds
                and bool(full_content.strip())
            )

            if can_auto_continue and pending_text:
                emit_text = pending_text
                remaining_text = ""
                if enable_local_text_controls:
                    (
                        emit_text,
                        remaining_text,
                        _,
                        _,
                    ) = _plan_stream_text_controls(
                        emitted_text=emitted_content,
                        pending_text=pending_text,
                        stop_sequences=stop_sequences,
                        requested_max_tokens=requested_max_tokens,
                        is_final=False,
                    )

                if emit_text:
                    if not text_block_started:
                        text_block_index = current_block_index
                        yield format_sse_event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": text_block_index,
                                "content_block": {"type": "text", "text": ""},
                            },
                        )
                        text_block_started = True
                    yield format_sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": text_block_index,
                            "delta": {"type": "text_delta", "text": emit_text},
                        },
                    )
                    emitted_content += emit_text

                pending_text = remaining_text

            if can_auto_continue:
                continuation = await auto_continue_callback(
                    full_content,
                    recovery_round + 1,
                )
                await _maybe_aclose_response(current_response)
                if continuation is not None:
                    recovery_round += 1
                    current_response = continuation.response
                    logger.info(
                        "Streaming auto-continuation triggered for text-only "
                        f"output (round {recovery_round}, "
                        f"reason={'truncated' if segment_was_truncated else 'incomplete_tail'})"
                    )
                    continue

            if pending_text:
                emit_text = pending_text
                stop_reason = None
                stop_sequence = None
                if enable_local_text_controls:
                    emit_text, pending_text, stop_reason, stop_sequence = (
                        _plan_stream_text_controls(
                            emitted_text=emitted_content,
                            pending_text=pending_text,
                            stop_sequences=stop_sequences,
                            requested_max_tokens=requested_max_tokens,
                            is_final=True,
                        )
                    )

                if emit_text:
                    if not text_block_started:
                        text_block_index = current_block_index
                        yield format_sse_event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": text_block_index,
                                "content_block": {"type": "text", "text": ""},
                            },
                        )
                        text_block_started = True
                    yield format_sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": text_block_index,
                            "delta": {"type": "text_delta", "text": emit_text},
                        },
                    )
                    emitted_content += emit_text

                pending_text = ""
                if stop_reason:
                    local_stop_reason = stop_reason
                    local_stop_sequence = stop_sequence
                    terminated_early = True

            await _maybe_aclose_response(current_response)
            break

        # Track completion signals for truncation detection
        stream_completed_normally = (
            context_usage_percentage is not None or terminated_early
        )

        # Check for bracket-style tool calls in full content
        bracket_tool_calls = (
            [] if terminated_early else parse_bracket_tool_calls(full_content)
        )
        if bracket_tool_calls:
            # Close thinking block if open
            if thinking_block_started and thinking_block_index is not None:
                yield format_sse_event(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": thinking_block_index},
                )
                thinking_block_started = False
                current_block_index += 1

            # Close text block if open
            if text_block_started and text_block_index is not None:
                yield format_sse_event(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": text_block_index},
                )
                text_block_started = False
                current_block_index += 1

            for tc in bracket_tool_calls:
                tool_id = tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}"
                tool_name = tc.get("function", {}).get("name", "")
                tool_input = tc.get("function", {}).get("arguments", {})

                if isinstance(tool_input, str):
                    try:
                        tool_input = json.loads(tool_input)
                    except json.JSONDecodeError:
                        tool_input = {}

                yield format_sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": current_block_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": tool_name,
                            "input": {},
                        },
                    },
                )

                input_json = json.dumps(tool_input, ensure_ascii=False)
                yield format_sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": current_block_index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": input_json,
                        },
                    },
                )

                yield format_sse_event(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": current_block_index},
                )

                tool_blocks.append(
                    {"id": tool_id, "name": tool_name, "input": tool_input}
                )
                current_block_index += 1

        # Close thinking block if still open
        if thinking_block_started and thinking_block_index is not None:
            yield format_sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": thinking_block_index},
            )
            current_block_index += 1

        # Close text block if still open
        if text_block_started and text_block_index is not None:
            yield format_sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": text_block_index},
            )

        # Detect content truncation (missing completion signals)
        content_was_truncated = (
            not stream_completed_normally
            and len(full_content) > 0
            and not tool_blocks  # Don't confuse with tool call truncation
        )

        if content_was_truncated:
            from kiro.config import TRUNCATION_RECOVERY

            logger.error(
                f"Content truncated by Kiro API: stream ended without completion signals, "
                f"length={len(full_content)} chars. "
                f"{'Model will be notified automatically about truncation.' if TRUNCATION_RECOVERY else 'Set TRUNCATION_RECOVERY=true in .env to auto-notify model about truncation.'}"
            )

        # Calculate output tokens
        output_tokens = count_tokens(emitted_content + full_thinking_content)

        # Calculate total tokens from context usage if available
        if context_usage_percentage is not None:
            prompt_tokens, total_tokens, _, _ = calculate_tokens_from_context_usage(
                context_usage_percentage, output_tokens, model_cache, model
            )
            input_tokens = prompt_tokens

        # Determine stop reason
        stop_reason = local_stop_reason or ("tool_use" if tool_blocks else "end_turn")

        # Send message_delta with stop_reason and usage
        yield format_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": stop_reason,
                    "stop_sequence": local_stop_sequence,
                },
                "usage": {"output_tokens": output_tokens},
            },
        )

        # Send message_stop
        yield format_sse_event("message_stop", {"type": "message_stop"})

        # Save truncation info for recovery (tracked by stable identifiers)
        from kiro.truncation_recovery import should_inject_recovery
        from kiro.truncation_state import save_tool_truncation, save_content_truncation

        if should_inject_recovery():
            # Save tool truncations (tracked by tool_call_id)
            if truncated_tools:
                for truncated_tool in truncated_tools:
                    save_tool_truncation(
                        tool_call_id=truncated_tool["id"],
                        tool_name=truncated_tool["name"],
                        truncation_info=truncated_tool["truncation_info"],
                    )

            # Save content truncation (tracked by content hash)
            if content_was_truncated:
                save_content_truncation(full_content)

            if truncated_tools or content_was_truncated:
                logger.info(
                    f"Truncation detected: {len(truncated_tools)} tool(s), "
                    f"content={content_was_truncated}. Will be handled when client sends next request."
                )

        logger.debug(
            f"[Anthropic Streaming] Completed: "
            f"input_tokens={input_tokens}, output_tokens={output_tokens}, "
            f"tool_blocks={len(tool_blocks)}, stop_reason={stop_reason}"
        )

    except FirstTokenTimeoutError:
        raise
    except GeneratorExit:
        logger.debug("Client disconnected (GeneratorExit)")
        raise
    except Exception as e:
        error_type = type(e).__name__
        error_msg = str(e) if str(e) else "(empty message)"
        logger.error(
            f"Error during Anthropic streaming: [{error_type}] {error_msg}",
            exc_info=True,
        )

        # Send error event
        yield format_sse_event(
            "error",
            {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": f"Internal error: {error_msg}",
                },
            },
        )
        raise
    finally:
        try:
            await _maybe_aclose_response(current_response)
        except Exception as close_error:
            logger.debug(f"Error closing response: {close_error}")


async def collect_anthropic_response(
    response: httpx.Response,
    model: str,
    model_cache: "ModelInfoCache",
    auth_manager: "KiroAuthManager",
    request_messages: Optional[list] = None,
    prompt_cache_usage: Optional[dict] = None,
    requested_max_tokens: Optional[int] = None,
    stop_sequences: Optional[List[str]] = None,
) -> dict:
    """
    Collect full response from Kiro stream in Anthropic format.

    Used for non-streaming mode.

    Args:
        response: HTTP response with stream
        model: Model name
        model_cache: Model cache
        auth_manager: Authentication manager
        request_messages: Original request messages (for token counting)

    Returns:
        Dictionary with full response in Anthropic Messages format
    """
    message_id = generate_message_id()

    # Count input tokens
    input_tokens = 0
    if request_messages:
        input_tokens = count_message_tokens(
            request_messages, apply_claude_correction=False
        )

    # Collect stream result
    result = await collect_stream_to_result(response)

    # Build content blocks
    content_blocks = []

    # Add thinking block FIRST if there's thinking content and mode is as_reasoning_content
    if result.thinking_content and FAKE_REASONING_HANDLING == "as_reasoning_content":
        content_blocks.append(
            {
                "type": "thinking",
                "thinking": result.thinking_content,
                "signature": generate_thinking_signature(),
            }
        )

    # Add text block if there's content
    # For include_as_text mode, prepend thinking content to regular content
    text_content = result.content
    if result.thinking_content and FAKE_REASONING_HANDLING == "include_as_text":
        text_content = result.thinking_content + text_content

    if text_content:
        content_blocks.append({"type": "text", "text": text_content})

    # Add tool use blocks
    for tc in result.tool_calls:
        tool_id = tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}"
        tool_name = tc.get("function", {}).get("name", "") or tc.get("name", "")
        tool_input = tc.get("function", {}).get("arguments", {}) or tc.get("input", {})

        if isinstance(tool_input, str):
            try:
                tool_input = json.loads(tool_input)
            except json.JSONDecodeError:
                tool_input = {}

        content_blocks.append(
            {"type": "tool_use", "id": tool_id, "name": tool_name, "input": tool_input}
        )

    stop_reason = "tool_use" if result.tool_calls else "end_turn"
    stop_sequence = None

    (
        content_blocks,
        constrained_stop_reason,
        constrained_stop_sequence,
    ) = _apply_non_streaming_output_controls(
        content_blocks,
        requested_max_tokens=requested_max_tokens,
        stop_sequences=stop_sequences,
    )
    if constrained_stop_reason:
        stop_reason = constrained_stop_reason
        stop_sequence = constrained_stop_sequence

    visible_text = _extract_text_from_blocks(content_blocks)

    # Calculate output tokens after local Anthropic-compatible post-processing.
    output_tokens = count_tokens(visible_text + result.thinking_content)

    # Calculate from context usage if available
    if result.context_usage_percentage is not None:
        prompt_tokens, _, _, _ = calculate_tokens_from_context_usage(
            result.context_usage_percentage, output_tokens, model_cache, model
        )
        input_tokens = prompt_tokens

    logger.debug(
        f"[Anthropic Non-Streaming] Completed: "
        f"input_tokens={input_tokens}, output_tokens={output_tokens}, "
        f"tool_calls={len(result.tool_calls)}, stop_reason={stop_reason}"
    )

    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "content": content_blocks,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": stop_sequence,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}
        | (prompt_cache_usage or {}),
        "_kiro_gateway_meta": {
            "content_truncation": (
                "detected" if result.content_was_truncated else "none"
            ),
            "local_stop_control": constrained_stop_reason or "none",
            "local_text_controls": ("bypass_tools" if result.tool_calls else "enabled"),
        },
    }


async def stream_with_first_token_retry_anthropic(
    make_request,
    model: str,
    model_cache: "ModelInfoCache",
    auth_manager: "KiroAuthManager",
    max_retries: int = FIRST_TOKEN_MAX_RETRIES,
    first_token_timeout: float = FIRST_TOKEN_TIMEOUT,
    request_messages: Optional[list] = None,
    request_tools: Optional[list] = None,
) -> AsyncGenerator[str, None]:
    """
    Streaming with automatic retry on first token timeout for Anthropic API.

    If model doesn't respond within first_token_timeout seconds,
    request is cancelled and a new one is made. Maximum max_retries attempts.

    This is seamless for user - they just see a delay,
    but eventually get a response (or error after all attempts).

    Args:
        make_request: Function to create new HTTP request
        model: Model name
        model_cache: Model cache
        auth_manager: Authentication manager
        max_retries: Maximum number of attempts
        first_token_timeout: First token wait timeout (seconds)
        request_messages: Original request messages (for fallback token counting)
        request_tools: Original request tools (for fallback token counting)

    Yields:
        Strings in Anthropic SSE format

    Raises:
        Exception with Anthropic error format after exhausting all attempts
    """

    def create_http_error(status_code: int, error_text: str) -> Exception:
        """Create exception for HTTP errors in Anthropic format."""
        return Exception(
            json.dumps(
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": f"Upstream API error: {error_text}",
                    },
                }
            )
        )

    def create_timeout_error(retries: int, timeout: float) -> Exception:
        """Create exception for timeout errors in Anthropic format."""
        return Exception(
            json.dumps(
                {
                    "type": "error",
                    "error": {
                        "type": "timeout_error",
                        "message": f"Model did not respond within {timeout}s after {retries} attempts. Please try again.",
                    },
                }
            )
        )

    async def stream_processor(response: httpx.Response) -> AsyncGenerator[str, None]:
        """Process response and yield Anthropic SSE chunks."""
        async for chunk in stream_kiro_to_anthropic(
            response,
            model,
            model_cache,
            auth_manager,
            first_token_timeout=first_token_timeout,
            request_messages=request_messages,
        ):
            yield chunk

    async for chunk in stream_with_first_token_retry(
        make_request=make_request,
        stream_processor=stream_processor,
        max_retries=max_retries,
        first_token_timeout=first_token_timeout,
        on_http_error=create_http_error,
        on_all_retries_failed=create_timeout_error,
    ):
        yield chunk
