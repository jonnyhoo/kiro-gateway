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
FastAPI routes for Anthropic Messages API.

Contains the /v1/messages endpoint compatible with Anthropic's Messages API.

Reference: https://docs.anthropic.com/en/api/messages
"""

import copy
import inspect
import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Security, Header
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from loguru import logger

from kiro.config import PROXY_API_KEY
from kiro.models_anthropic import (
    AnthropicMessagesRequest,
)
from kiro.auth import KiroAuthManager, AuthType
from kiro.cache import ModelInfoCache
from kiro.converters_anthropic import (
    anthropic_to_kiro,
    extract_system_prompt,
    apply_anthropic_tool_choice_compat,
)
from kiro.prompt_cache import (
    build_prompt_cache_usage,
    empty_prompt_cache_evaluation,
    summarize_prompt_cache_normalization,
    summarize_prompt_cache_segments,
    summarize_prompt_cache_tokens,
    summarize_prompt_cache_volatility,
)
from kiro.response_cache import get_anthropic_cache_eligibility
from kiro.streaming_anthropic import (
    stream_kiro_to_anthropic,
    collect_anthropic_response,
)
from kiro.http_client import KiroHttpClient
from kiro.utils import generate_conversation_id
from kiro.tokenizer import count_message_tokens, count_tokens
from kiro.streaming_core import ToolCallTruncationError

# Import debug_logger
try:
    from kiro.debug_logger import debug_logger
except ImportError:
    debug_logger = None


# --- Security scheme ---
# Anthropic uses x-api-key header instead of Authorization: Bearer
anthropic_api_key_header = APIKeyHeader(name="x-api-key", auto_error=False)
# Also support Authorization: Bearer for compatibility
auth_header = APIKeyHeader(name="Authorization", auto_error=False)


async def verify_anthropic_api_key(
    x_api_key: Optional[str] = Security(anthropic_api_key_header),
    authorization: Optional[str] = Security(auth_header),
) -> bool:
    """
    Verify API key for Anthropic API.

    Supports two authentication methods:
    1. x-api-key header (Anthropic native)
    2. Authorization: Bearer header (for compatibility)

    Args:
        x_api_key: Value from x-api-key header
        authorization: Value from Authorization header

    Returns:
        True if key is valid

    Raises:
        HTTPException: 401 if key is invalid or missing
    """
    # Check x-api-key first (Anthropic native)
    if x_api_key and x_api_key == PROXY_API_KEY:
        return True

    # Fall back to Authorization: Bearer
    if authorization and authorization == f"Bearer {PROXY_API_KEY}":
        return True

    logger.warning("Access attempt with invalid API key (Anthropic endpoint)")
    raise HTTPException(
        status_code=401,
        detail={
            "type": "error",
            "error": {
                "type": "authentication_error",
                "message": "Invalid or missing API key. Use x-api-key header or Authorization: Bearer.",
            },
        },
    )


def _count_anthropic_tool_tokens(tools: Any) -> int:
    """Approximate token count for Anthropic tool definitions."""
    if not tools or not isinstance(tools, list):
        return 0

    total_tokens = 0

    for tool in tools:
        if not isinstance(tool, dict):
            continue

        total_tokens += 4
        total_tokens += count_tokens(
            tool.get("name", ""), apply_claude_correction=False
        )
        total_tokens += count_tokens(
            tool.get("description", ""), apply_claude_correction=False
        )

        input_schema = tool.get("input_schema")
        if input_schema:
            total_tokens += count_tokens(
                json.dumps(input_schema, ensure_ascii=False),
                apply_claude_correction=False,
            )

    return total_tokens


def _merge_prompt_cache_usage(response_payload: dict, prompt_cache_usage: dict) -> dict:
    """Merge prompt-cache usage fields into an Anthropic response payload."""
    usage = response_payload.get("usage")
    if not isinstance(usage, dict):
        usage = {}
        response_payload["usage"] = usage

    usage.pop("cache_creation_input_tokens", None)
    usage.pop("cache_read_input_tokens", None)
    usage.pop("cache_creation", None)

    if not prompt_cache_usage:
        return response_payload

    usage.update(prompt_cache_usage)
    return response_payload


async def _evaluate_prompt_cache(
    prompt_cache: Any,
    cache_payload: dict,
) -> tuple[dict, str, Optional[list], str]:
    """Evaluate prompt-cache compatibility and return usage, status, and segment details."""
    if prompt_cache is None:
        evaluation = empty_prompt_cache_evaluation()
        return (
            evaluation.usage,
            evaluation.status,
            evaluation.segment_results,
            evaluation.source,
        )

    detailed_method = getattr(prompt_cache, "evaluate_anthropic_request_detailed", None)
    if callable(detailed_method):
        detailed_result = detailed_method(cache_payload)
        if inspect.isawaitable(detailed_result):
            evaluation = await detailed_result
            return (
                evaluation.usage,
                evaluation.status,
                evaluation.segment_results,
                evaluation.source,
            )

    basic_method = getattr(prompt_cache, "evaluate_anthropic_request", None)
    if callable(basic_method):
        basic_result = basic_method(cache_payload)
        if inspect.isawaitable(basic_result):
            prompt_cache_usage, prompt_cache_status = await basic_result
            return prompt_cache_usage, prompt_cache_status, None, "legacy"

    evaluation = empty_prompt_cache_evaluation()
    return (
        evaluation.usage,
        evaluation.status,
        evaluation.segment_results,
        evaluation.source,
    )


def _build_prompt_cache_headers(
    prompt_cache_status: str,
    prompt_cache_usage: dict,
    prompt_cache_segments: Optional[list],
    prompt_cache_source: str,
) -> dict[str, str]:
    return {
        "x-kiro-gateway-prompt-cache": prompt_cache_status,
        "x-kiro-gateway-prompt-cache-source": prompt_cache_source,
        "x-kiro-gateway-prompt-cache-segments": summarize_prompt_cache_segments(
            prompt_cache_segments
        ),
        "x-kiro-gateway-prompt-cache-tokens": summarize_prompt_cache_tokens(
            prompt_cache_usage
        ),
        "x-kiro-gateway-prompt-cache-volatility": summarize_prompt_cache_volatility(
            prompt_cache_segments
        ),
        "x-kiro-gateway-prompt-cache-normalized": summarize_prompt_cache_normalization(
            prompt_cache_segments
        ),
    }


def _pop_gateway_response_meta(response_payload: Any) -> dict[str, str]:
    """Remove internal gateway metadata before returning an Anthropic response."""
    if not isinstance(response_payload, dict):
        return {}

    meta = response_payload.pop("_kiro_gateway_meta", None)
    return meta if isinstance(meta, dict) else {}


def _build_local_output_headers(
    *,
    text_controls_enabled: bool,
    response_meta: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Build lightweight observability headers for local output controls."""
    meta = response_meta or {}
    default_mode = "enabled" if text_controls_enabled else "bypass_tools"
    return {
        "x-kiro-gateway-local-text-controls": meta.get(
            "local_text_controls", default_mode
        ),
        "x-kiro-gateway-local-stop-control": meta.get("local_stop_control", "none"),
        "x-kiro-gateway-content-truncation": meta.get("content_truncation", "none"),
    }


def _build_tool_cache_scope(
    request_payload: dict, auth_manager: KiroAuthManager
) -> Optional[dict[str, str]]:
    """Build a conservative tool-result cache scope for safe reuse."""
    metadata = request_payload.get("metadata")
    scope: dict[str, str] = {}

    if auth_manager.profile_arn:
        scope["profile_arn"] = auth_manager.profile_arn
    elif auth_manager.api_host:
        scope["api_host"] = auth_manager.api_host

    if isinstance(metadata, dict):
        for key in (
            "user_id",
            "session_id",
            "workspace_id",
            "cwd",
            "working_directory",
            "repo",
            "repository",
            "project",
            "project_root",
        ):
            value = metadata.get(key)
            if value is not None and str(value).strip():
                scope[key] = str(value).strip()

    for key in (
        "session_id",
        "workspace_id",
        "cwd",
        "working_directory",
        "repo",
        "repository",
        "project",
        "project_root",
        "user",
    ):
        value = request_payload.get(key)
        if value is not None and str(value).strip():
            scope[key] = str(value).strip()

    meaningful_scope = {
        key: value
        for key, value in scope.items()
        if key not in ("profile_arn", "api_host")
    }
    return scope if meaningful_scope else None


def _validate_required_anthropic_tool_choice(
    request_data: AnthropicMessagesRequest, response_payload: dict
) -> None:
    """Fail explicitly when required Anthropic tool_choice is not satisfied."""
    choice = request_data.tool_choice
    if not choice:
        return

    if isinstance(choice, dict):
        choice_type = choice.get("type")
        choice_name = choice.get("name")
    else:
        choice_type = getattr(choice, "type", None)
        choice_name = getattr(choice, "name", None)

    if choice_type not in {"any", "tool"}:
        return

    content = response_payload.get("content")
    tool_blocks = [
        block
        for block in content or []
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    if not tool_blocks:
        raise HTTPException(
            status_code=502,
            detail={
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": f"Upstream model ignored required tool_choice ({choice_type}).",
                },
            },
        )

    if choice_type == "tool" and not any(
        block.get("name") == choice_name for block in tool_blocks
    ):
        raise HTTPException(
            status_code=502,
            detail={
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": (
                        "Upstream model did not call the required tool_choice "
                        f"tool '{choice_name}'."
                    ),
                },
            },
        )


# --- Router ---
router = APIRouter(tags=["Anthropic API"])


@router.post(
    "/v1/messages/count_tokens", dependencies=[Depends(verify_anthropic_api_key)]
)
async def messages_count_tokens(
    payload: dict,
    anthropic_version: Optional[str] = Header(None, alias="anthropic-version"),
):
    """
    Anthropic Count Tokens API endpoint.

    Returns an approximate Claude-compatible input token count for the request.
    """
    messages = payload.get("messages")
    model = payload.get("model", "")

    if not isinstance(messages, list) or not messages:
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "messages must be a non-empty list",
                },
            },
        )

    if anthropic_version:
        logger.debug(f"Anthropic-Version header (count_tokens): {anthropic_version}")

    input_tokens = count_message_tokens(messages, apply_claude_correction=False)

    system_prompt = extract_system_prompt(payload.get("system"))
    if system_prompt:
        input_tokens += count_tokens(system_prompt, apply_claude_correction=False)

    input_tokens += _count_anthropic_tool_tokens(payload.get("tools"))

    logger.info(
        f"Request to /v1/messages/count_tokens (model={model}, input_tokens={input_tokens})"
    )

    return JSONResponse(status_code=200, content={"input_tokens": input_tokens})


@router.post("/v1/messages", dependencies=[Depends(verify_anthropic_api_key)])
async def messages(
    request: Request,
    request_data: AnthropicMessagesRequest,
    anthropic_version: Optional[str] = Header(None, alias="anthropic-version"),
):
    """
    Anthropic Messages API endpoint.

    Compatible with Anthropic's /v1/messages endpoint.
    Accepts requests in Anthropic format and translates them to Kiro API.

    Required headers:
    - x-api-key: Your API key (or Authorization: Bearer)
    - anthropic-version: API version (optional, for compatibility)
    - Content-Type: application/json

    Args:
        request: FastAPI Request for accessing app.state
        request_data: Request in Anthropic MessagesRequest format
        anthropic_version: Anthropic API version header (optional)

    Returns:
        StreamingResponse for streaming mode (SSE)
        JSONResponse for non-streaming mode

    Raises:
        HTTPException: On validation or API errors
    """
    logger.info(
        f"Request to /v1/messages (model={request_data.model}, stream={request_data.stream})"
    )

    if anthropic_version:
        logger.debug(f"Anthropic-Version header: {anthropic_version}")

    auth_manager: KiroAuthManager = request.app.state.auth_manager
    model_cache: ModelInfoCache = request.app.state.model_cache

    # Note: prepare_new_request() and log_request_body() are now called by DebugLoggerMiddleware
    # This ensures debug logging works even for requests that fail Pydantic validation (422 errors)

    # Check for truncation recovery opportunities
    from kiro.truncation_state import get_tool_truncation, get_content_truncation
    from kiro.truncation_recovery import (
        generate_truncation_tool_result,
        generate_truncation_user_message,
    )
    from kiro.models_anthropic import AnthropicMessage

    modified_messages = []
    tool_results_modified = 0
    content_notices_added = 0

    for msg in request_data.messages:
        # Check if this is a user message with tool_result blocks
        if msg.role == "user" and msg.content and isinstance(msg.content, list):
            modified_content_blocks = []
            has_modifications = False

            for block in msg.content:
                # Handle both dict and Pydantic objects (ToolResultContentBlock)
                if isinstance(block, dict):
                    block_type = block.get("type")
                    tool_use_id = block.get("tool_use_id")
                    original_content = block.get("content", "")
                elif hasattr(block, "type"):
                    block_type = block.type
                    tool_use_id = getattr(block, "tool_use_id", None)
                    original_content = getattr(block, "content", "")
                else:
                    modified_content_blocks.append(block)
                    continue

                if block_type == "tool_result" and tool_use_id:
                    truncation_info = get_tool_truncation(tool_use_id)
                    if truncation_info:
                        # Modify tool_result content to include truncation notice
                        synthetic = generate_truncation_tool_result(
                            tool_name=truncation_info.tool_name,
                            tool_use_id=tool_use_id,
                            truncation_info=truncation_info.truncation_info,
                        )
                        # Prepend truncation notice to original content
                        modified_content = f"{synthetic['content']}\n\n---\n\nOriginal tool result:\n{original_content}"

                        # Create modified block (handle both dict and Pydantic)
                        if isinstance(block, dict):
                            modified_block = block.copy()
                            modified_block["content"] = modified_content
                        else:
                            # Pydantic object - use model_copy
                            modified_block = block.model_copy(
                                update={"content": modified_content}
                            )

                        modified_content_blocks.append(modified_block)
                        tool_results_modified += 1
                        has_modifications = True
                        logger.debug(
                            f"Modified tool_result for {tool_use_id} to include truncation notice"
                        )
                        continue

                modified_content_blocks.append(block)

            # Create NEW AnthropicMessage object if modifications were made (Pydantic immutability)
            if has_modifications:
                modified_msg = msg.model_copy(
                    update={"content": modified_content_blocks}
                )
                modified_messages.append(modified_msg)
                continue  # Skip normal append since we already added modified version

        # Check if this is an assistant message with truncated content
        if msg.role == "assistant" and msg.content:
            # Extract text content for hash check
            text_content = ""
            if isinstance(msg.content, str):
                text_content = msg.content
            elif isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_content += block.get("text", "")

            if text_content:
                truncation_info = get_content_truncation(text_content)
                if truncation_info:
                    # Add this message first
                    modified_messages.append(msg)
                    # Then add synthetic user message about truncation
                    synthetic_user_msg = AnthropicMessage(
                        role="user",
                        content=[
                            {"type": "text", "text": generate_truncation_user_message()}
                        ],
                    )
                    modified_messages.append(synthetic_user_msg)
                    content_notices_added += 1
                    logger.debug(
                        f"Added truncation notice after assistant message (hash: {truncation_info.message_hash})"
                    )
                    continue  # Skip normal append since we already added it

        modified_messages.append(msg)

    if tool_results_modified > 0 or content_notices_added > 0:
        request_data.messages = modified_messages
        logger.info(
            f"Truncation recovery: modified {tool_results_modified} tool_result(s), added {content_notices_added} content notice(s)"
        )

    try:
        request_data, tool_choice_mode = apply_anthropic_tool_choice_compat(
            request_data
        )
        if tool_choice_mode in {"any", "tool"}:
            logger.debug(
                f"Applied Anthropic tool_choice compatibility mode: {tool_choice_mode}"
            )
    except ValueError as e:
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": str(e)},
            },
        )

    response_cache = request.app.state.response_cache
    prompt_cache = getattr(request.app.state, "prompt_cache", None)
    tool_result_cache = request.app.state.tool_result_cache
    local_text_controls_enabled = not bool(request_data.tools)
    cache_status = "bypass"
    request_payload = request_data.model_dump(exclude_none=True)
    tool_cache_scope = _build_tool_cache_scope(request_payload, auth_manager)
    (
        request_data.messages,
        tool_cache_status,
    ) = await tool_result_cache.hydrate_anthropic_messages(
        request_data.messages, scope=tool_cache_scope
    )
    cache_payload = request_data.model_dump(exclude_none=True)
    prompt_cache_usage = build_prompt_cache_usage(0, 0, 0, 0)
    prompt_cache_status = "bypass"
    prompt_cache_segments = []
    prompt_cache_source = "bypass"
    prompt_cache_headers = _build_prompt_cache_headers(
        prompt_cache_status,
        prompt_cache_usage,
        prompt_cache_segments,
        prompt_cache_source,
    )
    (
        cacheable,
        cache_reason,
    ) = get_anthropic_cache_eligibility(cache_payload)

    if not request_data.stream:
        if cacheable and response_cache.is_available():
            cached_response = await response_cache.get_json(
                "anthropic-messages", cache_payload
            )
            if cached_response is not None:
                logger.info("HTTP 200 - POST /v1/messages (non-streaming) - cache hit")
                if debug_logger:
                    debug_logger.discard_buffers()
                cached_response = _merge_prompt_cache_usage(
                    copy.deepcopy(cached_response),
                    prompt_cache_usage,
                )
                response_meta = _pop_gateway_response_meta(cached_response)
                return JSONResponse(
                    content=cached_response,
                    headers=(
                        {
                            "x-kiro-gateway-cache": "hit",
                            "x-kiro-gateway-tool-cache": tool_cache_status,
                        }
                        | _build_local_output_headers(
                            text_controls_enabled=local_text_controls_enabled,
                            response_meta=response_meta,
                        )
                        | prompt_cache_headers
                    ),
                )
            cache_status = "miss"
        else:
            logger.debug(f"Response cache bypass for /v1/messages: {cache_reason}")

    (
        prompt_cache_usage,
        prompt_cache_status,
        prompt_cache_segments,
        prompt_cache_source,
    ) = await _evaluate_prompt_cache(
        prompt_cache,
        cache_payload,
    )
    prompt_cache_headers = _build_prompt_cache_headers(
        prompt_cache_status,
        prompt_cache_usage,
        prompt_cache_segments,
        prompt_cache_source,
    )

    # Generate conversation ID for Kiro API (random UUID, not used for tracking)
    conversation_id = generate_conversation_id()

    # Build payload for Kiro
    # profileArn is only needed for Kiro Desktop auth
    profile_arn_for_payload = ""
    if auth_manager.auth_type == AuthType.KIRO_DESKTOP and auth_manager.profile_arn:
        profile_arn_for_payload = auth_manager.profile_arn

    try:
        kiro_payload = anthropic_to_kiro(
            request_data, conversation_id, profile_arn_for_payload
        )
    except ValueError as e:
        logger.error(f"Conversion error: {e}")
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": str(e)},
            },
        )

    # Log Kiro payload
    try:
        kiro_request_body = json.dumps(
            kiro_payload, ensure_ascii=False, indent=2
        ).encode("utf-8")
        if debug_logger:
            debug_logger.log_kiro_request_body(kiro_request_body)
    except Exception as e:
        logger.warning(f"Failed to log Kiro request: {e}")

    # Create HTTP client with retry logic
    # For streaming: use per-request client to avoid CLOSE_WAIT leak on VPN disconnect (issue #54)
    # For non-streaming: use shared client for connection pooling
    url = f"{auth_manager.api_host}/generateAssistantResponse"
    logger.debug(f"Kiro API URL: {url}")

    if request_data.stream:
        # Streaming mode: per-request client prevents orphaned connections
        # when network interface changes (VPN disconnect/reconnect)
        http_client = KiroHttpClient(auth_manager, shared_client=None)
    else:
        # Non-streaming mode: shared client for efficient connection reuse
        shared_client = request.app.state.http_client
        http_client = KiroHttpClient(auth_manager, shared_client=shared_client)

    # Prepare data for token counting
    # Convert Pydantic models to dicts for tokenizer
    messages_for_tokenizer = [msg.model_dump() for msg in request_data.messages]
    ([tool.model_dump() for tool in request_data.tools] if request_data.tools else None)

    try:
        # Make request to Kiro API (for both streaming and non-streaming modes)
        # Important: we wait for Kiro response BEFORE returning StreamingResponse,
        # so that we can return proper HTTP error codes if Kiro fails
        response = await http_client.request_with_retry(
            "POST", url, kiro_payload, stream=True
        )

        if response.status_code != 200:
            try:
                error_content = await response.aread()
            except Exception:
                error_content = b"Unknown error"

            await http_client.close()
            error_text = error_content.decode("utf-8", errors="replace")

            # Try to parse JSON response from Kiro to extract error message
            error_message = error_text
            try:
                error_json = json.loads(error_text)
                # Enhance Kiro API errors with user-friendly messages
                from kiro.kiro_errors import enhance_kiro_error

                error_info = enhance_kiro_error(error_json)
                error_message = error_info.user_message
                # Log original error for debugging
                logger.debug(
                    f"Original Kiro error: {error_info.original_message} (reason: {error_info.reason})"
                )
            except (json.JSONDecodeError, KeyError):
                pass

            # Log access log for error (before flush, so it gets into app_logs)
            logger.warning(
                f"HTTP {response.status_code} - POST /v1/messages - {error_message[:100]}"
            )

            # Flush debug logs on error
            if debug_logger:
                debug_logger.flush_on_error(response.status_code, error_message)

            # Return error in Anthropic format
            return JSONResponse(
                status_code=response.status_code,
                content={
                    "type": "error",
                    "error": {"type": "api_error", "message": error_message},
                },
            )

        if request_data.stream:
            # Streaming mode - Kiro already returned 200, now stream the response
            async def stream_wrapper():
                streaming_error = None
                client_disconnected = False
                try:
                    async for chunk in stream_kiro_to_anthropic(
                        response,
                        request_data.model,
                        model_cache,
                        auth_manager,
                        request_messages=messages_for_tokenizer,
                        prompt_cache_usage=prompt_cache_usage,
                        requested_max_tokens=request_data.max_tokens,
                        stop_sequences=request_data.stop_sequences,
                        enable_local_text_controls=local_text_controls_enabled,
                    ):
                        yield chunk
                except GeneratorExit:
                    client_disconnected = True
                    logger.debug(
                        "Client disconnected during streaming (GeneratorExit in routes)"
                    )
                except Exception as e:
                    streaming_error = e
                    # Send error event to client, then gracefully end the stream
                    try:
                        error_event = f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': str(e)}})}\n\n"
                        yield error_event
                    except Exception:
                        pass
                finally:
                    await http_client.close()
                    if streaming_error:
                        error_type = type(streaming_error).__name__
                        error_msg = (
                            str(streaming_error)
                            if str(streaming_error)
                            else "(empty message)"
                        )
                        logger.error(
                            f"HTTP 500 - POST /v1/messages (streaming) - [{error_type}] {error_msg[:100]}"
                        )
                    elif client_disconnected:
                        logger.info(
                            "HTTP 200 - POST /v1/messages (streaming) - client disconnected"
                        )
                    else:
                        logger.info(
                            "HTTP 200 - POST /v1/messages (streaming) - completed"
                        )

                    if debug_logger:
                        if streaming_error:
                            debug_logger.flush_on_error(500, str(streaming_error))
                        else:
                            debug_logger.discard_buffers()

            return StreamingResponse(
                stream_wrapper(),
                media_type="text/event-stream",
                headers=(
                    {
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "x-kiro-gateway-tool-cache": tool_cache_status,
                    }
                    | _build_local_output_headers(
                        text_controls_enabled=local_text_controls_enabled
                    )
                    | prompt_cache_headers
                ),
            )

        else:
            # Non-streaming mode - collect entire response
            anthropic_response = await collect_anthropic_response(
                response,
                request_data.model,
                model_cache,
                auth_manager,
                request_messages=messages_for_tokenizer,
                prompt_cache_usage=prompt_cache_usage,
                requested_max_tokens=request_data.max_tokens,
                stop_sequences=request_data.stop_sequences,
            )
            anthropic_response = _merge_prompt_cache_usage(
                anthropic_response, prompt_cache_usage
            )
            _validate_required_anthropic_tool_choice(request_data, anthropic_response)

            await http_client.close()

            logger.info("HTTP 200 - POST /v1/messages (non-streaming) - completed")

            if debug_logger:
                debug_logger.discard_buffers()

            if cache_status == "miss":
                await response_cache.set_json(
                    "anthropic-messages",
                    cache_payload,
                    copy.deepcopy(anthropic_response),
                )

            response_meta = _pop_gateway_response_meta(anthropic_response)

            return JSONResponse(
                content=anthropic_response,
                headers=(
                    {
                        "x-kiro-gateway-cache": cache_status,
                        "x-kiro-gateway-tool-cache": tool_cache_status,
                    }
                    | _build_local_output_headers(
                        text_controls_enabled=local_text_controls_enabled,
                        response_meta=response_meta,
                    )
                    | prompt_cache_headers
                ),
            )

    except HTTPException as e:
        await http_client.close()
        logger.error(f"HTTP {e.status_code} - POST /v1/messages - {e.detail}")
        if debug_logger:
            debug_logger.flush_on_error(e.status_code, str(e.detail))
        raise
    except ToolCallTruncationError as e:
        await http_client.close()
        logger.error(f"HTTP 502 - POST /v1/messages - {str(e)}")
        if debug_logger:
            debug_logger.flush_on_error(502, str(e))

        return JSONResponse(
            status_code=502,
            content={
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": str(e),
                },
            },
        )
    except Exception as e:
        await http_client.close()
        logger.error(f"Internal error: {e}", exc_info=True)
        logger.error(f"HTTP 500 - POST /v1/messages - {str(e)[:100]}")
        if debug_logger:
            debug_logger.flush_on_error(500, str(e))

        return JSONResponse(
            status_code=500,
            content={
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": f"Internal Server Error: {str(e)}",
                },
            },
        )
