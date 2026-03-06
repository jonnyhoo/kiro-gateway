# -*- coding: utf-8 -*-

"""
Redis-backed exact response cache for safe non-streaming requests.

This cache is intentionally conservative:
- exact request match only
- non-streaming only
- no tools or images
- low-randomness requests only

The goal is to reduce repeated upstream calls without changing tool-heavy
agent behavior or pretending to be provider-native prompt caching.
"""

import hashlib
import json
from typing import Any, Dict, Optional, Tuple

from loguru import logger
from redis import asyncio as redis_asyncio

from kiro.tool_result_cache import is_probably_read_only_tool


def _json_dumps_canonical(value: Any) -> str:
    """Serialize a value deterministically for hashing and storage."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _has_openai_images(messages: list[Any]) -> bool:
    """Detect OpenAI image content blocks in a message list."""
    for msg in messages:
        if not isinstance(msg, dict):
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            continue

        for block in content:
            if isinstance(block, dict) and block.get("type") in ("image_url", "image"):
                return True

    return False


def _openai_tool_history_status(messages: list[Any]) -> Tuple[bool, str]:
    """Validate whether OpenAI tool history is complete and read-only."""
    tool_calls: dict[str, str] = {}
    resolved_tool_calls: set[str] = set()

    for msg in messages:
        if not isinstance(msg, dict):
            continue

        if msg.get("role") == "assistant":
            for tool_call in msg.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                tool_id = tool_call.get("id")
                function = tool_call.get("function", {})
                tool_name = function.get("name", "")
                if tool_id:
                    if not is_probably_read_only_tool(tool_name):
                        return False, "mutating_tool_history"
                    tool_calls[tool_id] = tool_name
        elif msg.get("role") == "tool":
            tool_call_id = msg.get("tool_call_id")
            if tool_call_id not in tool_calls:
                return False, "orphan_tool_result"
            resolved_tool_calls.add(tool_call_id)

    if tool_calls and resolved_tool_calls != set(tool_calls.keys()):
        return False, "pending_tool_history"

    return True, "eligible"


def _anthropic_tool_history_status(messages: list[Any]) -> Tuple[bool, str]:
    """Validate whether Anthropic tool history is complete and read-only."""
    tool_uses: dict[str, str] = {}
    resolved_tool_uses: set[str] = set()

    for msg in messages:
        if not isinstance(msg, dict):
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            continue

        if msg.get("role") == "assistant":
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use":
                    continue
                tool_id = block.get("id", "")
                tool_name = block.get("name", "")
                if tool_id:
                    if not is_probably_read_only_tool(tool_name):
                        return False, "mutating_tool_history"
                    tool_uses[tool_id] = tool_name
        elif msg.get("role") == "user":
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "image":
                    return False, "images"
                if block_type != "tool_result":
                    continue
                tool_use_id = block.get("tool_use_id", "")
                if tool_use_id not in tool_uses:
                    return False, "orphan_tool_result"
                tool_result_content = block.get("content")
                if isinstance(tool_result_content, list):
                    for nested_block in tool_result_content:
                        if (
                            isinstance(nested_block, dict)
                            and nested_block.get("type") == "image"
                        ):
                            return False, "images"
                resolved_tool_uses.add(tool_use_id)

    if tool_uses and resolved_tool_uses != set(tool_uses.keys()):
        return False, "pending_tool_history"

    return True, "eligible"


def get_openai_cache_eligibility(payload: Dict[str, Any]) -> Tuple[bool, str]:
    """Return whether an OpenAI request is safe for exact response caching."""
    if payload.get("stream") is True:
        return False, "streaming"

    if payload.get("tools") or payload.get("tool_choice"):
        return False, "tools"

    if payload.get("n") not in (None, 1):
        return False, "multiple_choices"

    if payload.get("temperature") not in (None, 0, 0.0):
        return False, "temperature"

    if payload.get("top_p") not in (None, 1, 1.0):
        return False, "top_p"

    if payload.get("presence_penalty") not in (None, 0, 0.0):
        return False, "presence_penalty"

    if payload.get("frequency_penalty") not in (None, 0, 0.0):
        return False, "frequency_penalty"

    messages = payload.get("messages", [])
    if _has_openai_images(messages):
        return False, "images"

    return _openai_tool_history_status(messages)


def get_anthropic_cache_eligibility(payload: Dict[str, Any]) -> Tuple[bool, str]:
    """Return whether an Anthropic request is safe for exact response caching."""
    if payload.get("stream") is True:
        return False, "streaming"

    if payload.get("tools") or payload.get("tool_choice"):
        return False, "tools"

    if payload.get("temperature") not in (None, 0, 0.0):
        return False, "temperature"

    if payload.get("top_p") not in (None, 1, 1.0):
        return False, "top_p"

    if payload.get("top_k") is not None:
        return False, "top_k"

    messages = payload.get("messages", [])
    return _anthropic_tool_history_status(messages)


class RedisResponseCache:
    """Redis-backed exact response cache with fail-open behavior."""

    def __init__(self, redis_url: str, ttl_seconds: int, key_prefix: str):
        self.redis_url = redis_url.strip()
        self.ttl_seconds = ttl_seconds
        self.key_prefix = key_prefix.strip() or "kiro-gateway:response-cache"
        self.client: Optional[redis_asyncio.Redis] = None

    @property
    def enabled(self) -> bool:
        """Whether the cache is configured to run."""
        return bool(self.redis_url) and self.ttl_seconds > 0

    def is_available(self) -> bool:
        """Whether the cache is ready to serve requests."""
        return self.client is not None

    async def connect(self) -> None:
        """Connect to Redis and fail open if unavailable."""
        if not self.enabled:
            logger.info("Response cache disabled")
            return

        try:
            self.client = redis_asyncio.from_url(self.redis_url, decode_responses=True)
            await self.client.ping()
            logger.info(
                f"Response cache connected: {self.redis_url} (ttl={self.ttl_seconds}s)"
            )
        except Exception as e:
            logger.warning(f"Response cache unavailable, continuing without Redis: {e}")
            self.client = None

    async def close(self) -> None:
        """Close the Redis connection if open."""
        if self.client is None:
            return

        try:
            await self.client.aclose()
        except Exception as e:
            logger.warning(f"Error closing response cache client: {e}")
        finally:
            self.client = None

    def _make_key(self, namespace: str, request_payload: Dict[str, Any]) -> str:
        """Build a stable Redis key for a request payload."""
        digest = hashlib.sha256(
            _json_dumps_canonical(request_payload).encode("utf-8")
        ).hexdigest()
        return f"{self.key_prefix}:{namespace}:{digest}"

    async def get_json(
        self, namespace: str, request_payload: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Fetch a cached JSON payload, or None if absent/unavailable."""
        if self.client is None:
            return None

        key = self._make_key(namespace, request_payload)
        try:
            cached = await self.client.get(key)
            if not cached:
                return None
            return json.loads(cached)
        except Exception as e:
            logger.warning(f"Response cache read failed for {namespace}: {e}")
            return None

    async def set_json(
        self,
        namespace: str,
        request_payload: Dict[str, Any],
        response_payload: Dict[str, Any],
    ) -> bool:
        """Store a JSON payload in Redis with TTL, returning success."""
        if self.client is None:
            return False

        key = self._make_key(namespace, request_payload)
        try:
            await self.client.set(
                key,
                _json_dumps_canonical(response_payload),
                ex=self.ttl_seconds,
            )
            return True
        except Exception as e:
            logger.warning(f"Response cache write failed for {namespace}: {e}")
            return False
