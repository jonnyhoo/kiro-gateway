# -*- coding: utf-8 -*-

"""
Redis-backed cache for observed read-only tool results.

This cache does not execute tools on the server. It records completed tool
results from client requests so repeated read-only tool outputs can be detected
and reused by future gateway features.
"""

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from redis import asyncio as redis_asyncio


READ_ONLY_HINTS = (
    "read",
    "list",
    "get",
    "search",
    "find",
    "grep",
    "glob",
    "stat",
    "query",
    "fetch",
    "ls",
    "cat",
    "inspect",
)

MUTATING_HINTS = (
    "write",
    "edit",
    "update",
    "delete",
    "create",
    "remove",
    "move",
    "rename",
    "mv",
    "cp",
    "bash",
    "run",
    "exec",
    "install",
)


def _json_dumps_canonical(value: Any) -> str:
    """Serialize a value deterministically for hashing and storage."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize_arguments(arguments: Any) -> str:
    """Normalize tool arguments into a deterministic string."""
    if isinstance(arguments, str):
        return arguments
    return _json_dumps_canonical(arguments)


def _normalize_result_content(content: Any) -> str:
    """Normalize tool result content into a deterministic string."""
    if isinstance(content, str):
        return content
    return _json_dumps_canonical(content)


def _normalize_scope(scope: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """Normalize a tool cache scope into a deterministic, non-empty mapping."""
    if not isinstance(scope, dict):
        return None

    normalized_scope: Dict[str, str] = {}
    for key, value in scope.items():
        if value is None:
            continue

        key_text = str(key).strip()
        value_text = str(value).strip()
        if not key_text or not value_text:
            continue

        normalized_scope[key_text] = value_text

    return normalized_scope or None


def is_probably_read_only_tool(tool_name: str) -> bool:
    """Best-effort heuristic for identifying read-only tool names."""
    normalized = tool_name.lower()

    if any(token in normalized for token in MUTATING_HINTS):
        return False

    return any(token in normalized for token in READ_ONLY_HINTS)


def summarize_tool_cache_status(statuses: List[str]) -> str:
    """Summarize per-tool statuses into a single response header value."""
    if not statuses:
        return "bypass"

    unique = set(statuses)
    if unique == {"reused"}:
        return "reused"
    if unique == {"hit"}:
        return "hit"
    if unique == {"miss"}:
        return "miss"
    if unique == {"bypass"}:
        return "bypass"
    if unique <= {"hit", "reused"}:
        return "reused"
    return "mixed"


class RedisToolResultCache:
    """Redis-backed cache for observed read-only tool results."""

    def __init__(self, redis_url: str, ttl_seconds: int, key_prefix: str):
        self.redis_url = redis_url.strip()
        self.ttl_seconds = ttl_seconds
        self.key_prefix = key_prefix.strip() or "kiro-gateway:tool-result-cache"
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
            logger.info("Tool result cache disabled")
            return

        try:
            self.client = redis_asyncio.from_url(self.redis_url, decode_responses=True)
            await self.client.ping()
            logger.info(
                f"Tool result cache connected: {self.redis_url} (ttl={self.ttl_seconds}s)"
            )
        except Exception as e:
            logger.warning(
                f"Tool result cache unavailable, continuing without Redis: {e}"
            )
            self.client = None

    async def close(self) -> None:
        """Close the Redis connection if open."""
        if self.client is None:
            return

        try:
            await self.client.aclose()
        except Exception as e:
            logger.warning(f"Error closing tool result cache client: {e}")
        finally:
            self.client = None

    def _make_key(
        self, namespace: str, scope: Dict[str, str], tool_name: str, arguments: Any
    ) -> str:
        digest = hashlib.sha256(
            _json_dumps_canonical(
                {
                    "scope": scope,
                    "tool_name": tool_name,
                    "arguments": _normalize_arguments(arguments),
                }
            ).encode("utf-8")
        ).hexdigest()
        return f"{self.key_prefix}:{namespace}:{digest}"

    async def _get_cached_payload(
        self,
        namespace: str,
        scope: Optional[Dict[str, Any]],
        tool_name: str,
        arguments: Any,
    ) -> Optional[Dict[str, Any]]:
        normalized_scope = _normalize_scope(scope)
        if (
            self.client is None
            or normalized_scope is None
            or not tool_name
            or not is_probably_read_only_tool(tool_name)
        ):
            return None

        key = self._make_key(namespace, normalized_scope, tool_name, arguments)
        try:
            cached = await self.client.get(key)
            return json.loads(cached) if cached else None
        except Exception as e:
            logger.warning(f"Tool result cache read failed for {tool_name}: {e}")
            return None

    async def _set_payload(
        self,
        namespace: str,
        scope: Optional[Dict[str, Any]],
        tool_name: str,
        arguments: Any,
        content: Any,
    ) -> bool:
        normalized_scope = _normalize_scope(scope)
        if (
            self.client is None
            or normalized_scope is None
            or not tool_name
            or not is_probably_read_only_tool(tool_name)
        ):
            return False

        key = self._make_key(namespace, normalized_scope, tool_name, arguments)
        payload = {
            "scope": normalized_scope,
            "tool_name": tool_name,
            "arguments": arguments,
            "content": content,
        }

        try:
            await self.client.set(
                key, _json_dumps_canonical(payload), ex=self.ttl_seconds
            )
            return True
        except Exception as e:
            logger.warning(f"Tool result cache write failed for {tool_name}: {e}")
            return False

    async def _observe(
        self,
        namespace: str,
        scope: Optional[Dict[str, Any]],
        tool_name: str,
        arguments: Any,
        content: Any,
    ) -> str:
        if self.client is None:
            return "bypass"

        if _normalize_scope(scope) is None:
            return "bypass"

        if not tool_name or not is_probably_read_only_tool(tool_name):
            return "bypass"

        if self._is_empty_tool_content(content):
            return "bypass"

        normalized_content = _normalize_result_content(content)
        cached_payload = await self._get_cached_payload(
            namespace, scope, tool_name, arguments
        )
        await self._set_payload(namespace, scope, tool_name, arguments, content)

        if (
            cached_payload
            and _normalize_result_content(cached_payload.get("content"))
            == normalized_content
        ):
            return "hit"

        return "miss"

    @staticmethod
    def _is_empty_tool_content(content: Any) -> bool:
        """Return True when tool result content is effectively empty."""
        if content is None:
            return True
        if isinstance(content, str):
            return content.strip() == ""
        if isinstance(content, list):
            return len(content) == 0
        return False

    async def hydrate_openai_messages(
        self, messages: List[Any], scope: Optional[Dict[str, Any]] = None
    ) -> Tuple[List[Any], str]:
        """Hydrate empty read-only OpenAI tool results from Redis when possible."""
        normalized_scope = _normalize_scope(scope)
        if not messages or normalized_scope is None:
            return messages, "bypass"

        tool_calls: Dict[str, Dict[str, Any]] = {}
        statuses: List[str] = []
        modified_messages: List[Any] = []

        for msg in messages:
            if getattr(msg, "role", None) == "assistant":
                for tool_call in getattr(msg, "tool_calls", None) or []:
                    if not isinstance(tool_call, dict):
                        continue
                    tool_id = tool_call.get("id")
                    function = tool_call.get("function", {})
                    if tool_id:
                        tool_calls[tool_id] = {
                            "name": function.get("name", ""),
                            "arguments": function.get("arguments", {}),
                        }
                modified_messages.append(msg)
                continue

            if getattr(msg, "role", None) == "tool":
                metadata = tool_calls.get(getattr(msg, "tool_call_id", None))
                if not metadata or not is_probably_read_only_tool(metadata["name"]):
                    statuses.append("bypass")
                    modified_messages.append(msg)
                    continue

                current_content = getattr(msg, "content", None)
                cached_payload = await self._get_cached_payload(
                    "openai", normalized_scope, metadata["name"], metadata["arguments"]
                )

                if (
                    self._is_empty_tool_content(current_content)
                    and cached_payload is not None
                ):
                    statuses.append("reused")
                    modified_messages.append(
                        msg.model_copy(
                            update={"content": cached_payload.get("content", "")}
                        )
                    )
                    continue

                statuses.append(
                    await self._observe(
                        "openai",
                        normalized_scope,
                        metadata["name"],
                        metadata["arguments"],
                        current_content,
                    )
                )
                modified_messages.append(msg)
                continue

            modified_messages.append(msg)

        return modified_messages, summarize_tool_cache_status(statuses)

    async def hydrate_anthropic_messages(
        self, messages: List[Any], scope: Optional[Dict[str, Any]] = None
    ) -> Tuple[List[Any], str]:
        """Hydrate empty read-only Anthropic tool results from Redis when possible."""
        normalized_scope = _normalize_scope(scope)
        if not messages or normalized_scope is None:
            return messages, "bypass"

        tool_uses: Dict[str, Dict[str, Any]] = {}
        statuses: List[str] = []
        modified_messages: List[Any] = []

        for msg in messages:
            role = getattr(msg, "role", None)
            content = getattr(msg, "content", None)

            if role != "assistant" and role != "user":
                modified_messages.append(msg)
                continue

            if not isinstance(content, list):
                modified_messages.append(msg)
                continue

            if role == "assistant":
                for block in content:
                    if not hasattr(block, "type") and not isinstance(block, dict):
                        continue
                    block_type = (
                        block.get("type") if isinstance(block, dict) else block.type
                    )
                    if block_type != "tool_use":
                        continue
                    tool_id = (
                        block.get("id")
                        if isinstance(block, dict)
                        else getattr(block, "id", "")
                    )
                    tool_name = (
                        block.get("name")
                        if isinstance(block, dict)
                        else getattr(block, "name", "")
                    )
                    tool_input = (
                        block.get("input")
                        if isinstance(block, dict)
                        else getattr(block, "input", {})
                    )
                    if tool_id:
                        tool_uses[tool_id] = {
                            "name": tool_name,
                            "arguments": tool_input,
                        }
                modified_messages.append(msg)
                continue

            modified_blocks = []
            has_modifications = False

            for block in content:
                if not hasattr(block, "type") and not isinstance(block, dict):
                    modified_blocks.append(block)
                    continue

                block_type = (
                    block.get("type") if isinstance(block, dict) else block.type
                )
                if block_type != "tool_result":
                    modified_blocks.append(block)
                    continue

                tool_use_id = (
                    block.get("tool_use_id")
                    if isinstance(block, dict)
                    else getattr(block, "tool_use_id", "")
                )
                metadata = tool_uses.get(tool_use_id)
                if not metadata or not is_probably_read_only_tool(metadata["name"]):
                    statuses.append("bypass")
                    modified_blocks.append(block)
                    continue

                current_content = (
                    block.get("content")
                    if isinstance(block, dict)
                    else getattr(block, "content", None)
                )
                cached_payload = await self._get_cached_payload(
                    "anthropic",
                    normalized_scope,
                    metadata["name"],
                    metadata["arguments"],
                )

                if (
                    self._is_empty_tool_content(current_content)
                    and cached_payload is not None
                ):
                    statuses.append("reused")
                    has_modifications = True
                    if isinstance(block, dict):
                        updated = block.copy()
                        updated["content"] = cached_payload.get("content", "")
                    else:
                        updated = block.model_copy(
                            update={"content": cached_payload.get("content", "")}
                        )
                    modified_blocks.append(updated)
                    continue

                statuses.append(
                    await self._observe(
                        "anthropic",
                        normalized_scope,
                        metadata["name"],
                        metadata["arguments"],
                        current_content,
                    )
                )
                modified_blocks.append(block)

            if has_modifications:
                modified_messages.append(
                    msg.model_copy(update={"content": modified_blocks})
                )
            else:
                modified_messages.append(msg)

        return modified_messages, summarize_tool_cache_status(statuses)

    async def observe_openai_messages(
        self, messages: List[Dict[str, Any]], scope: Optional[Dict[str, Any]] = None
    ) -> str:
        """Observe OpenAI tool exchanges and cache read-only tool results."""
        normalized_scope = _normalize_scope(scope)
        if normalized_scope is None:
            return "bypass"

        tool_calls: Dict[str, Dict[str, Any]] = {}
        statuses: List[str] = []

        for msg in messages:
            if not isinstance(msg, dict):
                continue

            role = msg.get("role")
            if role == "assistant":
                for tool_call in msg.get("tool_calls") or []:
                    if not isinstance(tool_call, dict):
                        continue
                    tool_id = tool_call.get("id")
                    function = tool_call.get("function", {})
                    if tool_id:
                        tool_calls[tool_id] = {
                            "name": function.get("name", ""),
                            "arguments": function.get("arguments", {}),
                        }
            elif role == "tool":
                tool_call_id = msg.get("tool_call_id")
                metadata = tool_calls.get(tool_call_id)
                if not metadata:
                    statuses.append("bypass")
                    continue
                statuses.append(
                    await self._observe(
                        namespace="openai",
                        scope=normalized_scope,
                        tool_name=metadata["name"],
                        arguments=metadata["arguments"],
                        content=msg.get("content", ""),
                    )
                )

        return summarize_tool_cache_status(statuses)

    async def observe_anthropic_messages(
        self, messages: List[Dict[str, Any]], scope: Optional[Dict[str, Any]] = None
    ) -> str:
        """Observe Anthropic tool exchanges and cache read-only tool results."""
        normalized_scope = _normalize_scope(scope)
        if normalized_scope is None:
            return "bypass"

        tool_uses: Dict[str, Dict[str, Any]] = {}
        statuses: List[str] = []

        for msg in messages:
            if not isinstance(msg, dict):
                continue

            role = msg.get("role")
            content = msg.get("content")
            if not isinstance(content, list):
                continue

            if role == "assistant":
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_uses[block.get("id", "")] = {
                            "name": block.get("name", ""),
                            "arguments": block.get("input", {}),
                        }
            elif role == "user":
                for block in content:
                    if (
                        not isinstance(block, dict)
                        or block.get("type") != "tool_result"
                    ):
                        continue
                    tool_use_id = block.get("tool_use_id", "")
                    metadata = tool_uses.get(tool_use_id)
                    if not metadata:
                        statuses.append("bypass")
                        continue
                    statuses.append(
                        await self._observe(
                            namespace="anthropic",
                            scope=normalized_scope,
                            tool_name=metadata["name"],
                            arguments=metadata["arguments"],
                            content=block.get("content", ""),
                        )
                    )

        return summarize_tool_cache_status(statuses)
