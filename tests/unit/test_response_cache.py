# -*- coding: utf-8 -*-

"""
Unit tests for Redis exact response cache.
"""

import pytest
from unittest.mock import AsyncMock, patch

from kiro.response_cache import (
    RedisResponseCache,
    get_openai_cache_eligibility,
    get_anthropic_cache_eligibility,
)
from kiro.tool_result_cache import (
    RedisToolResultCache,
    is_probably_read_only_tool,
)


class FakeToolCacheRedis:
    def __init__(self):
        self.storage = {}

    async def get(self, key):
        return self.storage.get(key)

    async def set(self, key, value, ex=None):
        self.storage[key] = value
        return True


class TestOpenAICacheEligibility:
    """Tests for OpenAI exact cache eligibility checks."""

    def test_allows_simple_non_streaming_request(self):
        payload = {
            "model": "claude-sonnet-4.5",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        }

        eligible, reason = get_openai_cache_eligibility(payload)
        assert eligible is True
        assert reason == "eligible"

    def test_rejects_tools(self):
        payload = {
            "model": "claude-sonnet-4.5",
            "messages": [{"role": "user", "content": "Hello"}],
            "tools": [{"type": "function"}],
        }

        eligible, reason = get_openai_cache_eligibility(payload)
        assert eligible is False
        assert reason == "tools"

    def test_rejects_images(self):
        payload = {
            "model": "claude-sonnet-4.5",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Hello"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,abc"},
                        },
                    ],
                }
            ],
        }

        eligible, reason = get_openai_cache_eligibility(payload)
        assert eligible is False
        assert reason == "images"

    def test_allows_completed_read_only_tool_history(self):
        payload = {
            "model": "claude-sonnet-4.5",
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": {"path": "README.md"},
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": "file content",
                },
                {
                    "role": "user",
                    "content": "Summarize it",
                },
            ],
        }

        eligible, reason = get_openai_cache_eligibility(payload)
        assert eligible is True
        assert reason == "eligible"

    def test_rejects_mutating_tool_history(self):
        payload = {
            "model": "claude-sonnet-4.5",
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": {"path": "README.md"},
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": "done",
                },
            ],
        }

        eligible, reason = get_openai_cache_eligibility(payload)
        assert eligible is False
        assert reason == "mutating_tool_history"


class TestAnthropicCacheEligibility:
    """Tests for Anthropic exact cache eligibility checks."""

    def test_allows_simple_non_streaming_request(self):
        payload = {
            "model": "claude-sonnet-4.5",
            "max_tokens": 128,
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        }

        eligible, reason = get_anthropic_cache_eligibility(payload)
        assert eligible is True
        assert reason == "eligible"

    def test_rejects_non_text_content_blocks(self):
        payload = {
            "model": "claude-sonnet-4.5",
            "max_tokens": 128,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Hello"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "abc",
                            },
                        },
                    ],
                }
            ],
        }

        eligible, reason = get_anthropic_cache_eligibility(payload)
        assert eligible is False
        assert reason == "images"

    def test_allows_completed_read_only_tool_history(self):
        payload = {
            "model": "claude-sonnet-4.5",
            "max_tokens": 128,
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "read_file",
                            "input": {"path": "README.md"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "file content",
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": "Summarize it",
                },
            ],
        }

        eligible, reason = get_anthropic_cache_eligibility(payload)
        assert eligible is True
        assert reason == "eligible"

    def test_rejects_mutating_tool_history(self):
        payload = {
            "model": "claude-sonnet-4.5",
            "max_tokens": 128,
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "write_file",
                            "input": {"path": "README.md"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "done",
                        },
                    ],
                },
            ],
        }

        eligible, reason = get_anthropic_cache_eligibility(payload)
        assert eligible is False
        assert reason == "mutating_tool_history"


class TestRedisResponseCache:
    """Tests for RedisResponseCache behavior."""

    @patch("kiro.response_cache.redis_asyncio.from_url")
    @pytest.mark.asyncio
    async def test_connects_and_roundtrips_json(self, mock_from_url):
        mock_client = AsyncMock()
        mock_client.get.return_value = '{"id":"cached"}'
        mock_from_url.return_value = mock_client

        cache = RedisResponseCache(
            redis_url="redis://localhost:6379/0",
            ttl_seconds=300,
            key_prefix="test-cache",
        )

        await cache.connect()
        result = await cache.get_json(
            "openai", {"messages": [{"role": "user", "content": "Hello"}]}
        )

        assert cache.is_available() is True
        assert result == {"id": "cached"}

        ok = await cache.set_json(
            "openai",
            {"messages": [{"role": "user", "content": "Hello"}]},
            {"id": "fresh"},
        )
        assert ok is True
        mock_client.set.assert_awaited_once()

    @patch("kiro.response_cache.redis_asyncio.from_url")
    @pytest.mark.asyncio
    async def test_fails_open_when_redis_is_unavailable(self, mock_from_url):
        mock_client = AsyncMock()
        mock_client.ping.side_effect = RuntimeError("redis down")
        mock_from_url.return_value = mock_client

        cache = RedisResponseCache(
            redis_url="redis://localhost:6379/0",
            ttl_seconds=300,
            key_prefix="test-cache",
        )

        await cache.connect()
        result = await cache.get_json(
            "openai", {"messages": [{"role": "user", "content": "Hello"}]}
        )

        assert cache.is_available() is False
        assert result is None


class TestRedisToolResultCache:
    """Tests for read-only tool result cache behavior."""

    def test_read_only_heuristic(self):
        assert is_probably_read_only_tool("read_file") is True
        assert is_probably_read_only_tool("grep_search") is True
        assert is_probably_read_only_tool("write_file") is False
        assert is_probably_read_only_tool("bash") is False

    @patch("kiro.tool_result_cache.redis_asyncio.from_url")
    @pytest.mark.asyncio
    async def test_observe_openai_messages_requires_scope(self, mock_from_url):
        mock_client = AsyncMock()
        mock_client.get.side_effect = [None, '{"content":"file content"}']
        mock_from_url.return_value = mock_client

        cache = RedisToolResultCache(
            redis_url="redis://localhost:6379/0",
            ttl_seconds=300,
            key_prefix="test-tool-cache",
        )
        await cache.connect()

        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": {"path": "README.md"},
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "file content",
            },
        ]

        first = await cache.observe_openai_messages(messages)

        assert first == "bypass"

    @patch("kiro.tool_result_cache.redis_asyncio.from_url")
    @pytest.mark.asyncio
    async def test_observe_openai_messages_miss_then_hit_with_scope(
        self, mock_from_url
    ):
        mock_client = AsyncMock()
        mock_client.get.side_effect = [None, '{"content":"file content"}']
        mock_from_url.return_value = mock_client

        cache = RedisToolResultCache(
            redis_url="redis://localhost:6379/0",
            ttl_seconds=300,
            key_prefix="test-tool-cache",
        )
        await cache.connect()

        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": {"path": "README.md"},
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "file content",
            },
        ]

        first = await cache.observe_openai_messages(
            messages, scope={"user": "session-a"}
        )
        second = await cache.observe_openai_messages(
            messages, scope={"user": "session-a"}
        )

        assert first == "miss"
        assert second == "hit"

    @pytest.mark.asyncio
    async def test_tool_result_cache_isolated_by_scope(self):
        cache = RedisToolResultCache(
            redis_url="redis://localhost:6379/0",
            ttl_seconds=300,
            key_prefix="test-tool-cache",
        )
        cache.client = FakeToolCacheRedis()

        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": {"path": "README.md"},
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "file content",
            },
        ]

        first = await cache.observe_openai_messages(
            messages, scope={"user": "session-a", "workspace_id": "repo-a"}
        )
        second = await cache.observe_openai_messages(
            messages, scope={"user": "session-a", "workspace_id": "repo-a"}
        )
        third = await cache.observe_openai_messages(
            messages, scope={"user": "session-a", "workspace_id": "repo-b"}
        )

        assert first == "miss"
        assert second == "hit"
        assert third == "miss"

    @patch("kiro.tool_result_cache.redis_asyncio.from_url")
    @pytest.mark.asyncio
    async def test_hydrate_openai_messages_reuses_empty_tool_result(
        self, mock_from_url
    ):
        mock_client = AsyncMock()
        mock_client.get.return_value = '{"tool_name":"read_file","arguments":{"path":"README.md"},"content":"file content"}'
        mock_from_url.return_value = mock_client

        cache = RedisToolResultCache(
            redis_url="redis://localhost:6379/0",
            ttl_seconds=300,
            key_prefix="test-tool-cache",
        )
        await cache.connect()

        from kiro.models_openai import ChatMessage

        messages = [
            ChatMessage(
                role="assistant",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": {"path": "README.md"},
                        },
                    }
                ],
            ),
            ChatMessage(role="tool", tool_call_id="call_1", content=""),
        ]

        hydrated, status = await cache.hydrate_openai_messages(
            messages, scope={"user": "session-a"}
        )

        assert status == "reused"
        assert hydrated[1].content == "file content"

    @patch("kiro.tool_result_cache.redis_asyncio.from_url")
    @pytest.mark.asyncio
    async def test_hydrate_openai_messages_bypasses_without_scope(self, mock_from_url):
        mock_client = AsyncMock()
        mock_client.get.return_value = '{"tool_name":"read_file","arguments":{"path":"README.md"},"content":"file content"}'
        mock_from_url.return_value = mock_client

        cache = RedisToolResultCache(
            redis_url="redis://localhost:6379/0",
            ttl_seconds=300,
            key_prefix="test-tool-cache",
        )
        await cache.connect()

        from kiro.models_openai import ChatMessage

        messages = [
            ChatMessage(
                role="assistant",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": {"path": "README.md"},
                        },
                    }
                ],
            ),
            ChatMessage(role="tool", tool_call_id="call_1", content=""),
        ]

        hydrated, status = await cache.hydrate_openai_messages(messages)

        assert status == "bypass"
        assert hydrated[1].content == ""

    @patch("kiro.tool_result_cache.redis_asyncio.from_url")
    @pytest.mark.asyncio
    async def test_observe_anthropic_messages_bypasses_mutating_tools(
        self, mock_from_url
    ):
        mock_client = AsyncMock()
        mock_from_url.return_value = mock_client

        cache = RedisToolResultCache(
            redis_url="redis://localhost:6379/0",
            ttl_seconds=300,
            key_prefix="test-tool-cache",
        )
        await cache.connect()

        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "write_file",
                        "input": {"path": "a.txt"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "done",
                    },
                ],
            },
        ]

        status = await cache.observe_anthropic_messages(
            messages, scope={"user_id": "session-a"}
        )
        assert status == "bypass"

    @patch("kiro.tool_result_cache.redis_asyncio.from_url")
    @pytest.mark.asyncio
    async def test_hydrate_anthropic_messages_reuses_empty_tool_result(
        self, mock_from_url
    ):
        mock_client = AsyncMock()
        mock_client.get.return_value = '{"tool_name":"read_file","arguments":{"path":"README.md"},"content":"file content"}'
        mock_from_url.return_value = mock_client

        cache = RedisToolResultCache(
            redis_url="redis://localhost:6379/0",
            ttl_seconds=300,
            key_prefix="test-tool-cache",
        )
        await cache.connect()

        from kiro.models_anthropic import AnthropicMessage

        messages = [
            AnthropicMessage(
                role="assistant",
                content=[
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read_file",
                        "input": {"path": "README.md"},
                    }
                ],
            ),
            AnthropicMessage(
                role="user",
                content=[
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": ""}
                ],
            ),
        ]

        hydrated, status = await cache.hydrate_anthropic_messages(
            messages, scope={"user_id": "session-a"}
        )

        assert status == "reused"
        tool_result_block = hydrated[1].content[0]
        assert tool_result_block.content == "file content"
