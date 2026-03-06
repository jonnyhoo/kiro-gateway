# -*- coding: utf-8 -*-

"""
Redis-backed Anthropic prompt-cache compatibility layer.

This module does not change the upstream Kiro request. It tracks cacheable
Anthropic request segments marked with `cache_control` and reports
Anthropic-compatible cache creation/read usage back to clients.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from redis import asyncio as redis_asyncio

from kiro.tokenizer import count_tokens


DEFAULT_PROMPT_CACHE_TTL_SECONDS = 300
ONE_HOUR_TTL_SECONDS = 3600
FIVE_MINUTE_TTL_LABEL = "5m"
ONE_HOUR_TTL_LABEL = "1h"
UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
)
ISO_TIMESTAMP_PATTERN = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ][0-2]\d:[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-][0-2]\d:\d{2})?\b"
)
TEMP_PATH_PATTERN = re.compile(
    r"(?:[A-Za-z]:\\Users\\[^\\]+\\AppData\\Local\\Temp\\[^\s\"']+|(?:/private)?/tmp/[^\s\"']+|/var/folders/[^\s\"']+)"
)
REQUEST_ID_PATTERN = re.compile(
    r"(?i)(?:request|trace|req|correlation)[-_ ]?id[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9._:-]{6,}"
)
REQUEST_ID_NORMALIZE_PATTERN = re.compile(
    r"(?i)((?:request|trace|req|correlation)[-_ ]?id[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9._:-]{6,}"
)
UNIX_TIMESTAMP_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])\d{10,13}(?![A-Za-z0-9_-])")
PREFIXED_ID_PATTERN = re.compile(r"\b[a-z]{2,6}_[a-zA-Z0-9]{8,}\b")
API_TOKEN_PATTERN = re.compile(
    r"\b(?:sk|pk|api|key|token|bearer)[-_][a-zA-Z0-9]{16,}\b"
)
JWT_PATTERN = re.compile(r"\beyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\b")
HEX_IDENTIFIER_PATTERN = re.compile(
    r"\b(?:[a-fA-F0-9]{32}|[a-fA-F0-9]{40}|[a-fA-F0-9]{64})\b"
)


def _json_dumps_canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _count_anthropic_tool_tokens(tools: Any) -> int:
    if not isinstance(tools, list):
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
                _json_dumps_canonical(input_schema),
                apply_claude_correction=False,
            )
    return total_tokens


def _ttl_label(ttl_seconds: int) -> str:
    if ttl_seconds >= ONE_HOUR_TTL_SECONDS:
        return ONE_HOUR_TTL_LABEL
    return FIVE_MINUTE_TTL_LABEL


def _parse_cache_control(cache_control: Any, default_ttl_seconds: int) -> Optional[int]:
    if not isinstance(cache_control, dict):
        return None
    if cache_control.get("type") != "ephemeral":
        return None
    if cache_control.get("ttl") == ONE_HOUR_TTL_LABEL:
        return ONE_HOUR_TTL_SECONDS
    if cache_control.get("ttl") == FIVE_MINUTE_TTL_LABEL:
        return DEFAULT_PROMPT_CACHE_TTL_SECONDS
    return default_ttl_seconds


@dataclass
class CacheSegment:
    namespace: str
    label: str
    key_payload: Dict[str, Any]
    analysis_text: str
    token_count: int
    ttl_seconds: int
    normalization_tags: List[str] = field(default_factory=list)


@dataclass
class CacheSegmentResult:
    namespace: str
    label: str
    status: str
    token_count: int
    ttl_seconds: int
    volatility_tags: List[str] = field(default_factory=list)
    normalization_tags: List[str] = field(default_factory=list)


@dataclass
class PromptCacheEvaluation:
    usage: Dict[str, Any]
    status: str
    segment_results: List[CacheSegmentResult]
    source: str = "bypass"


def _has_cache_control_markers(payload: Dict[str, Any]) -> bool:
    system = payload.get("system")
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and _parse_cache_control(
                block.get("cache_control"),
                ONE_HOUR_TTL_SECONDS,
            ):
                return True

    for msg in payload.get("messages", []) or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and _parse_cache_control(
                block.get("cache_control"),
                DEFAULT_PROMPT_CACHE_TTL_SECONDS,
            ):
                return True

    return False


def detect_prompt_cache_volatility(text: Any) -> List[str]:
    if isinstance(text, str):
        analysis_text = text
    else:
        analysis_text = _json_dumps_canonical(text)

    tags: List[str] = []
    if UUID_PATTERN.search(analysis_text):
        tags.append("volatile_uuid")
    if ISO_TIMESTAMP_PATTERN.search(analysis_text):
        tags.append("volatile_timestamp")
    if UNIX_TIMESTAMP_PATTERN.search(analysis_text):
        tags.append("volatile_unix_timestamp")
    if TEMP_PATH_PATTERN.search(analysis_text):
        tags.append("volatile_temp_path")
    if REQUEST_ID_PATTERN.search(analysis_text):
        tags.append("volatile_request_id")
    prefixed_id_candidate_text = REQUEST_ID_NORMALIZE_PATTERN.sub("", analysis_text)
    if PREFIXED_ID_PATTERN.search(prefixed_id_candidate_text):
        tags.append("volatile_prefixed_id")
    if API_TOKEN_PATTERN.search(analysis_text):
        tags.append("volatile_api_token")
    if JWT_PATTERN.search(analysis_text):
        tags.append("volatile_jwt")
    if HEX_IDENTIFIER_PATTERN.search(analysis_text):
        tags.append("volatile_hex_identifier")
    return tags


def _dedupe_tags(tags: List[str]) -> List[str]:
    seen: set[str] = set()
    ordered: List[str] = []
    for tag in tags:
        if tag in seen:
            continue
        seen.add(tag)
        ordered.append(tag)
    return ordered


def normalize_prompt_cache_text(text: str) -> Tuple[str, List[str]]:
    normalized_text = text
    normalization_tags: List[str] = []

    replaced_request_id = REQUEST_ID_NORMALIZE_PATTERN.sub(
        r"\1[REQUEST_ID]", normalized_text
    )
    if replaced_request_id != normalized_text:
        normalization_tags.append("normalized_request_id")
        normalized_text = replaced_request_id

    replaced_uuid = UUID_PATTERN.sub("[UUID]", normalized_text)
    if replaced_uuid != normalized_text:
        normalization_tags.append("normalized_uuid")
        normalized_text = replaced_uuid

    replaced_timestamp = ISO_TIMESTAMP_PATTERN.sub("[TIMESTAMP]", normalized_text)
    if replaced_timestamp != normalized_text:
        normalization_tags.append("normalized_timestamp")
        normalized_text = replaced_timestamp

    replaced_unix_timestamp = UNIX_TIMESTAMP_PATTERN.sub(
        "[UNIX_TIMESTAMP]", normalized_text
    )
    if replaced_unix_timestamp != normalized_text:
        normalization_tags.append("normalized_unix_timestamp")
        normalized_text = replaced_unix_timestamp

    replaced_temp_path = TEMP_PATH_PATTERN.sub("[TEMP_PATH]", normalized_text)
    if replaced_temp_path != normalized_text:
        normalization_tags.append("normalized_temp_path")
        normalized_text = replaced_temp_path

    replaced_jwt = JWT_PATTERN.sub("[JWT]", normalized_text)
    if replaced_jwt != normalized_text:
        normalization_tags.append("normalized_jwt")
        normalized_text = replaced_jwt

    replaced_api_token = API_TOKEN_PATTERN.sub("[API_TOKEN]", normalized_text)
    if replaced_api_token != normalized_text:
        normalization_tags.append("normalized_api_token")
        normalized_text = replaced_api_token

    replaced_hex_identifier = HEX_IDENTIFIER_PATTERN.sub(
        "[HEX_IDENTIFIER]", normalized_text
    )
    if replaced_hex_identifier != normalized_text:
        normalization_tags.append("normalized_hex_identifier")
        normalized_text = replaced_hex_identifier

    replaced_prefixed_id = PREFIXED_ID_PATTERN.sub("[PREFIXED_ID]", normalized_text)
    if replaced_prefixed_id != normalized_text:
        normalization_tags.append("normalized_prefixed_id")
        normalized_text = replaced_prefixed_id

    return normalized_text, normalization_tags


def normalize_prompt_cache_value(value: Any) -> Tuple[Any, List[str]]:
    if isinstance(value, str):
        return normalize_prompt_cache_text(value)

    if isinstance(value, list):
        normalized_items = []
        normalization_tags: List[str] = []
        for item in value:
            normalized_item, item_tags = normalize_prompt_cache_value(item)
            normalized_items.append(normalized_item)
            normalization_tags.extend(item_tags)
        return normalized_items, _dedupe_tags(normalization_tags)

    if isinstance(value, dict):
        normalized_dict: Dict[str, Any] = {}
        normalization_tags: List[str] = []
        for key, item in value.items():
            normalized_item, item_tags = normalize_prompt_cache_value(item)
            normalized_dict[key] = normalized_item
            normalization_tags.extend(item_tags)
        return normalized_dict, _dedupe_tags(normalization_tags)

    return value, []


def extract_anthropic_cache_segments(payload: Dict[str, Any]) -> List[CacheSegment]:
    if not isinstance(payload, dict) or not _has_cache_control_markers(payload):
        return []

    model = payload.get("model", "")
    segments: List[CacheSegment] = []
    tools = payload.get("tools")

    if isinstance(tools, list) and tools:
        tool_tokens = _count_anthropic_tool_tokens(tools)
        if tool_tokens > 0:
            normalized_tools, normalization_tags = normalize_prompt_cache_value(tools)
            segments.append(
                CacheSegment(
                    namespace="tools",
                    label="tools",
                    key_payload={"model": model, "tools": normalized_tools},
                    analysis_text=_json_dumps_canonical(tools),
                    token_count=tool_tokens,
                    ttl_seconds=ONE_HOUR_TTL_SECONDS,
                    normalization_tags=normalization_tags,
                )
            )

    system = payload.get("system")
    if isinstance(system, list):
        for index, block in enumerate(system):
            if not isinstance(block, dict):
                continue
            ttl_seconds = _parse_cache_control(
                block.get("cache_control"),
                ONE_HOUR_TTL_SECONDS,
            )
            if ttl_seconds is None or block.get("type") != "text":
                continue
            text = block.get("text", "")
            token_count = count_tokens(text, apply_claude_correction=False)
            if token_count <= 0:
                continue
            normalized_text, normalization_tags = normalize_prompt_cache_text(text)
            segments.append(
                CacheSegment(
                    namespace="system",
                    label=f"system[{index}]",
                    key_payload={
                        "model": model,
                        "index": index,
                        "text": normalized_text,
                    },
                    analysis_text=text,
                    token_count=token_count,
                    ttl_seconds=ttl_seconds,
                    normalization_tags=normalization_tags,
                )
            )

    for message_index, msg in enumerate(payload.get("messages", []) or []):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            ttl_seconds = _parse_cache_control(
                block.get("cache_control"),
                DEFAULT_PROMPT_CACHE_TTL_SECONDS,
            )
            if ttl_seconds is None or block.get("type") != "text":
                continue
            text = block.get("text", "")
            token_count = count_tokens(text, apply_claude_correction=False)
            if token_count <= 0:
                continue
            normalized_text, normalization_tags = normalize_prompt_cache_text(text)
            segments.append(
                CacheSegment(
                    namespace="message",
                    label=f"message[{message_index}:{block_index}]",
                    key_payload={
                        "model": model,
                        "message_index": message_index,
                        "block_index": block_index,
                        "role": role,
                        "text": normalized_text,
                    },
                    analysis_text=text,
                    token_count=token_count,
                    ttl_seconds=ttl_seconds,
                    normalization_tags=normalization_tags,
                )
            )

    return segments


def summarize_prompt_cache_status(statuses: List[str]) -> str:
    if not statuses:
        return "bypass"
    unique = set(statuses)
    if unique == {"hit"}:
        return "hit"
    if unique == {"miss"}:
        return "miss"
    return "mixed"


def build_prompt_cache_usage(
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
    ephemeral_5m_input_tokens: int,
    ephemeral_1h_input_tokens: int,
) -> Dict[str, Any]:
    usage = {
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
    }
    if cache_creation_input_tokens > 0 or cache_read_input_tokens > 0:
        usage["cache_creation"] = {
            "ephemeral_5m_input_tokens": ephemeral_5m_input_tokens,
            "ephemeral_1h_input_tokens": ephemeral_1h_input_tokens,
        }
    return usage


def summarize_prompt_cache_segments(
    segment_results: Optional[List[CacheSegmentResult]],
    max_items: int = 8,
) -> str:
    if segment_results is None:
        return "unavailable"
    if not segment_results:
        return "none"

    visible_results = segment_results[:max_items]
    parts = [
        (
            f"{segment_result.label}:"
            f"{segment_result.status}:"
            f"{_ttl_label(segment_result.ttl_seconds)}:"
            f"{segment_result.token_count}"
        )
        for segment_result in visible_results
    ]
    remaining = len(segment_results) - len(visible_results)
    if remaining > 0:
        parts.append(f"+{remaining}more")
    return ",".join(parts)


def summarize_prompt_cache_tokens(usage: Dict[str, Any]) -> str:
    cache_creation = usage.get("cache_creation", {}) if isinstance(usage, dict) else {}
    return (
        f"read={usage.get('cache_read_input_tokens', 0)};"
        f"create={usage.get('cache_creation_input_tokens', 0)};"
        f"5m={cache_creation.get('ephemeral_5m_input_tokens', 0)};"
        f"1h={cache_creation.get('ephemeral_1h_input_tokens', 0)}"
    )


def summarize_prompt_cache_volatility(
    segment_results: Optional[List[CacheSegmentResult]],
    max_items: int = 8,
) -> str:
    if segment_results is None:
        return "unavailable"
    if not segment_results:
        return "none"

    volatile_segments = [
        segment_result
        for segment_result in segment_results
        if segment_result.volatility_tags
    ]
    if not volatile_segments:
        return "none"

    visible_results = volatile_segments[:max_items]
    parts = [
        f"{segment_result.label}:{'|'.join(segment_result.volatility_tags)}"
        for segment_result in visible_results
    ]
    remaining = len(volatile_segments) - len(visible_results)
    if remaining > 0:
        parts.append(f"+{remaining}more")
    return ",".join(parts)


def summarize_prompt_cache_normalization(
    segment_results: Optional[List[CacheSegmentResult]],
    max_items: int = 8,
) -> str:
    if segment_results is None:
        return "unavailable"
    if not segment_results:
        return "none"

    normalized_segments = [
        segment_result
        for segment_result in segment_results
        if segment_result.normalization_tags
    ]
    if not normalized_segments:
        return "none"

    visible_results = normalized_segments[:max_items]
    parts = [
        f"{segment_result.label}:{'|'.join(segment_result.normalization_tags)}"
        for segment_result in visible_results
    ]
    remaining = len(normalized_segments) - len(visible_results)
    if remaining > 0:
        parts.append(f"+{remaining}more")
    return ",".join(parts)


def empty_prompt_cache_evaluation(source: str = "bypass") -> PromptCacheEvaluation:
    return PromptCacheEvaluation(
        usage=build_prompt_cache_usage(0, 0, 0, 0),
        status="bypass",
        segment_results=[],
        source=source,
    )


def _build_shadow_prompt_cache_payload(
    payload: Dict[str, Any],
) -> Tuple[Dict[str, Any], bool]:
    shadow_payload = copy.deepcopy(payload)
    changed = False

    system = shadow_payload.get("system")
    if isinstance(system, str) and system.strip():
        shadow_payload["system"] = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral", "ttl": ONE_HOUR_TTL_LABEL},
            }
        ]
        changed = True
    elif isinstance(system, list):
        for index in range(len(system) - 1, -1, -1):
            block = system[index]
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            if isinstance(block.get("cache_control"), dict):
                break
            updated_block = dict(block)
            updated_block["cache_control"] = {
                "type": "ephemeral",
                "ttl": ONE_HOUR_TTL_LABEL,
            }
            system[index] = updated_block
            changed = True
            break

    messages = shadow_payload.get("messages")
    if isinstance(messages, list):
        message_cache_control_applied = False
        for message_index in range(len(messages) - 1, -1, -1):
            message = messages[message_index]
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                updated_message = dict(message)
                updated_message["content"] = [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": {
                            "type": "ephemeral",
                            "ttl": FIVE_MINUTE_TTL_LABEL,
                        },
                    }
                ]
                messages[message_index] = updated_message
                changed = True
                message_cache_control_applied = True
                break

            if not isinstance(content, list):
                continue

            for block_index in range(len(content) - 1, -1, -1):
                block = content[block_index]
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "text":
                    continue
                if isinstance(block.get("cache_control"), dict):
                    message_cache_control_applied = True
                    break
                updated_block = dict(block)
                updated_block["cache_control"] = {
                    "type": "ephemeral",
                    "ttl": FIVE_MINUTE_TTL_LABEL,
                }
                content[block_index] = updated_block
                changed = True
                message_cache_control_applied = True
                break

            if message_cache_control_applied:
                break

    return shadow_payload, changed


class RedisPromptCache:
    """Redis-backed Anthropic prompt-cache compatibility tracker."""

    def __init__(self, redis_url: str, key_prefix: str):
        self.redis_url = redis_url.strip()
        self.key_prefix = key_prefix.strip() or "kiro-gateway:prompt-cache"
        self.client: Optional[redis_asyncio.Redis] = None

    @property
    def enabled(self) -> bool:
        return bool(self.redis_url)

    def is_available(self) -> bool:
        return self.client is not None

    async def connect(self) -> None:
        if not self.enabled:
            logger.info("Prompt cache disabled")
            return

        try:
            self.client = redis_asyncio.from_url(self.redis_url, decode_responses=True)
            await self.client.ping()
            logger.info(f"Prompt cache connected: {self.redis_url}")
        except Exception as e:
            logger.warning(f"Prompt cache unavailable, continuing without Redis: {e}")
            self.client = None

    async def close(self) -> None:
        if self.client is None:
            return
        try:
            await self.client.aclose()
        except Exception as e:
            logger.warning(f"Error closing prompt cache client: {e}")
        finally:
            self.client = None

    def _make_key(self, segment: CacheSegment) -> str:
        digest = hashlib.sha256(
            _json_dumps_canonical(segment.key_payload).encode("utf-8")
        ).hexdigest()
        return f"{self.key_prefix}:{segment.namespace}:{digest}"

    async def evaluate_anthropic_request_detailed(
        self,
        payload: Dict[str, Any],
    ) -> PromptCacheEvaluation:
        if self.client is None:
            return empty_prompt_cache_evaluation()

        evaluation_payload = payload
        evaluation_source = "explicit"
        if not _has_cache_control_markers(payload):
            evaluation_payload, shadow_applied = _build_shadow_prompt_cache_payload(
                payload
            )
            if shadow_applied:
                evaluation_source = "shadow"
            else:
                return empty_prompt_cache_evaluation()

        segments = extract_anthropic_cache_segments(evaluation_payload)
        if not segments:
            return empty_prompt_cache_evaluation(source=evaluation_source)

        cache_creation_input_tokens = 0
        cache_read_input_tokens = 0
        ephemeral_5m_input_tokens = 0
        ephemeral_1h_input_tokens = 0
        statuses: List[str] = []
        segment_results: List[CacheSegmentResult] = []

        for segment in segments:
            key = self._make_key(segment)
            try:
                cached = await self.client.get(key)
                if cached:
                    segment_status = "hit"
                    statuses.append(segment_status)
                    cache_read_input_tokens += segment.token_count
                else:
                    segment_status = "miss"
                    statuses.append(segment_status)
                    cache_creation_input_tokens += segment.token_count
                    if segment.ttl_seconds >= ONE_HOUR_TTL_SECONDS:
                        ephemeral_1h_input_tokens += segment.token_count
                    else:
                        ephemeral_5m_input_tokens += segment.token_count
                    await self.client.set(key, "1", ex=segment.ttl_seconds)
                segment_results.append(
                    CacheSegmentResult(
                        namespace=segment.namespace,
                        label=segment.label,
                        status=segment_status,
                        token_count=segment.token_count,
                        ttl_seconds=segment.ttl_seconds,
                        volatility_tags=detect_prompt_cache_volatility(
                            segment.analysis_text
                        ),
                        normalization_tags=segment.normalization_tags,
                    )
                )
            except Exception as e:
                logger.warning(
                    f"Prompt cache read/write failed for {segment.namespace}: {e}"
                )
                return empty_prompt_cache_evaluation()

        volatile_segment_summary = summarize_prompt_cache_volatility(segment_results)
        if volatile_segment_summary != "none":
            logger.debug(
                f"Prompt cache volatility detected: {volatile_segment_summary}"
            )
        normalized_segment_summary = summarize_prompt_cache_normalization(
            segment_results
        )
        if normalized_segment_summary != "none":
            logger.debug(
                f"Prompt cache normalization applied: {normalized_segment_summary}"
            )

        return PromptCacheEvaluation(
            usage=build_prompt_cache_usage(
                cache_creation_input_tokens=cache_creation_input_tokens,
                cache_read_input_tokens=cache_read_input_tokens,
                ephemeral_5m_input_tokens=ephemeral_5m_input_tokens,
                ephemeral_1h_input_tokens=ephemeral_1h_input_tokens,
            ),
            status=summarize_prompt_cache_status(statuses),
            segment_results=segment_results,
            source=evaluation_source,
        )

    async def evaluate_anthropic_request(
        self, payload: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], str]:
        evaluation = await self.evaluate_anthropic_request_detailed(payload)
        return evaluation.usage, evaluation.status
