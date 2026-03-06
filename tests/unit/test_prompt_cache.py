# -*- coding: utf-8 -*-

import pytest

from kiro.prompt_cache import (
    DEFAULT_PROMPT_CACHE_TTL_SECONDS,
    ONE_HOUR_TTL_SECONDS,
    RedisPromptCache,
    detect_prompt_cache_volatility,
    extract_anthropic_cache_segments,
    normalize_prompt_cache_text,
    summarize_prompt_cache_segments,
    summarize_prompt_cache_normalization,
    summarize_prompt_cache_tokens,
    summarize_prompt_cache_volatility,
)


class FakeRedis:
    def __init__(self):
        self.storage = {}
        self.set_calls = []

    async def get(self, key):
        return self.storage.get(key)

    async def set(self, key, value, ex=None):
        self.storage[key] = value
        self.set_calls.append({"key": key, "value": value, "ex": ex})
        return True


class TestExtractAnthropicCacheSegments:
    def test_extracts_tools_system_and_message_segments(self):
        payload = {
            "model": "claude-sonnet-4-6",
            "tools": [
                {
                    "name": "offline_probe",
                    "description": "Read-only probe tool.",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
            "system": [
                {
                    "type": "text",
                    "text": "system block",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "user block",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ],
        }

        segments = extract_anthropic_cache_segments(payload)

        assert [segment.namespace for segment in segments] == [
            "tools",
            "system",
            "message",
        ]
        assert all(segment.token_count > 0 for segment in segments)
        assert segments[0].ttl_seconds == ONE_HOUR_TTL_SECONDS
        assert segments[1].ttl_seconds == ONE_HOUR_TTL_SECONDS
        assert segments[2].ttl_seconds == DEFAULT_PROMPT_CACHE_TTL_SECONDS

    def test_respects_explicit_1h_ttl_on_message_block(self):
        payload = {
            "model": "claude-sonnet-4-6",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "user block",
                            "cache_control": {"type": "ephemeral", "ttl": "1h"},
                        }
                    ],
                }
            ],
        }

        segments = extract_anthropic_cache_segments(payload)

        assert len(segments) == 1
        assert segments[0].namespace == "message"
        assert segments[0].ttl_seconds == ONE_HOUR_TTL_SECONDS


class TestPromptCacheVolatilityDetection:
    def test_detects_common_volatile_markers(self):
        text = (
            "request_id=req_abc123456 "
            "uuid=123e4567-e89b-12d3-a456-426614174000 "
            "timestamp=2026-03-07T01:34:33Z "
            "temp=C:\\Users\\Administrator\\AppData\\Local\\Temp\\session-42.json"
        )

        tags = detect_prompt_cache_volatility(text)

        assert tags == [
            "volatile_uuid",
            "volatile_timestamp",
            "volatile_temp_path",
            "volatile_request_id",
        ]

    def test_normalizes_common_volatile_markers(self):
        text = (
            "request_id=req_abc123456 "
            "uuid=123e4567-e89b-12d3-a456-426614174000 "
            "timestamp=2026-03-07T01:34:33Z "
            "temp=C:\\Users\\Administrator\\AppData\\Local\\Temp\\session-42.json"
        )

        normalized_text, tags = normalize_prompt_cache_text(text)

        assert normalized_text == (
            "request_id=[REQUEST_ID] uuid=[UUID] timestamp=[TIMESTAMP] temp=[TEMP_PATH]"
        )
        assert tags == [
            "normalized_request_id",
            "normalized_uuid",
            "normalized_timestamp",
            "normalized_temp_path",
        ]

    def test_detects_and_normalizes_headroom_style_universal_patterns(self):
        text = (
            "unix=1705312847123 "
            "pref=req_abcd1234xyz9 "
            "token=sk-abcdefghijklmnopqrstuvwxyz123456 "
            "jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.signature123 "
            "hash=0123456789abcdef0123456789abcdef"
        )

        volatility_tags = detect_prompt_cache_volatility(text)
        normalized_text, normalization_tags = normalize_prompt_cache_text(text)

        assert volatility_tags == [
            "volatile_unix_timestamp",
            "volatile_prefixed_id",
            "volatile_api_token",
            "volatile_jwt",
            "volatile_hex_identifier",
        ]
        assert normalized_text == (
            "unix=[UNIX_TIMESTAMP] "
            "pref=[PREFIXED_ID] "
            "token=[API_TOKEN] "
            "jwt=[JWT] "
            "hash=[HEX_IDENTIFIER]"
        )
        assert normalization_tags == [
            "normalized_unix_timestamp",
            "normalized_jwt",
            "normalized_api_token",
            "normalized_hex_identifier",
            "normalized_prefixed_id",
        ]


class TestRedisPromptCache:
    @pytest.mark.asyncio
    async def test_metadata_is_excluded_from_cache_key(self):
        payload = {
            "model": "claude-sonnet-4-6",
            "system": [
                {
                    "type": "text",
                    "text": "system block",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "user block",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ],
            "metadata": {"user_id": "session-a"},
        }

        cache = RedisPromptCache("redis://unused", "test-prefix")
        cache.client = FakeRedis()

        usage_first, status_first = await cache.evaluate_anthropic_request(payload)
        assert status_first == "miss"
        assert usage_first["cache_creation_input_tokens"] > 0
        assert usage_first["cache_read_input_tokens"] == 0

        payload_with_other_metadata = {
            **payload,
            "metadata": {"user_id": "session-b"},
        }
        usage_second, status_second = await cache.evaluate_anthropic_request(
            payload_with_other_metadata
        )

        assert status_second == "hit"
        assert usage_second["cache_creation_input_tokens"] == 0
        assert (
            usage_second["cache_read_input_tokens"]
            == usage_first["cache_creation_input_tokens"]
        )

    @pytest.mark.asyncio
    async def test_tool_change_only_recreates_tool_segment(self):
        base_payload = {
            "model": "claude-sonnet-4-6",
            "tools": [
                {
                    "name": "offline_probe",
                    "description": "Read-only probe tool.",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
            "system": [
                {
                    "type": "text",
                    "text": "system block",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "user block",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ],
        }

        cache = RedisPromptCache("redis://unused", "test-prefix")
        cache.client = FakeRedis()

        usage_first, status_first = await cache.evaluate_anthropic_request(base_payload)
        assert status_first == "miss"
        assert usage_first["cache_creation_input_tokens"] > 0

        changed_tool_payload = {
            **base_payload,
            "tools": [
                {
                    "name": "offline_probe",
                    "description": "Read-only probe tool. Variant.",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        }

        usage_second, status_second = await cache.evaluate_anthropic_request(
            changed_tool_payload
        )

        assert status_second == "mixed"
        assert usage_second["cache_read_input_tokens"] > 0
        assert usage_second["cache_creation_input_tokens"] > 0

    @pytest.mark.asyncio
    async def test_detailed_evaluation_reports_segment_ttls_and_debug_summaries(self):
        payload = {
            "model": "claude-sonnet-4-6",
            "tools": [
                {
                    "name": "offline_probe",
                    "description": "Read-only probe tool.",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
            "system": [
                {
                    "type": "text",
                    "text": "system block",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "user block",
                            "cache_control": {"type": "ephemeral", "ttl": "1h"},
                        }
                    ],
                }
            ],
        }

        cache = RedisPromptCache("redis://unused", "test-prefix")
        fake_redis = FakeRedis()
        cache.client = fake_redis

        evaluation_first = await cache.evaluate_anthropic_request_detailed(payload)

        assert evaluation_first.status == "miss"
        assert evaluation_first.source == "explicit"
        assert [segment.status for segment in evaluation_first.segment_results] == [
            "miss",
            "miss",
            "miss",
        ]
        assert [
            segment.ttl_seconds for segment in evaluation_first.segment_results
        ] == [
            ONE_HOUR_TTL_SECONDS,
            ONE_HOUR_TTL_SECONDS,
            ONE_HOUR_TTL_SECONDS,
        ]
        assert fake_redis.set_calls[0]["ex"] == ONE_HOUR_TTL_SECONDS
        assert fake_redis.set_calls[1]["ex"] == ONE_HOUR_TTL_SECONDS
        assert fake_redis.set_calls[2]["ex"] == ONE_HOUR_TTL_SECONDS
        assert "tools:miss:1h" in summarize_prompt_cache_segments(
            evaluation_first.segment_results
        )
        assert "system[0]:miss:1h" in summarize_prompt_cache_segments(
            evaluation_first.segment_results
        )
        assert summarize_prompt_cache_tokens(evaluation_first.usage).startswith(
            "read=0;create="
        )

        evaluation_second = await cache.evaluate_anthropic_request_detailed(payload)

        assert evaluation_second.status == "hit"
        assert all(
            segment.status == "hit" for segment in evaluation_second.segment_results
        )
        assert summarize_prompt_cache_tokens(evaluation_second.usage).startswith(
            "read="
        )

    @pytest.mark.asyncio
    async def test_detailed_evaluation_reports_volatility_tags(self):
        payload = {
            "model": "claude-sonnet-4-6",
            "system": [
                {
                    "type": "text",
                    "text": "request_id=req_abc123456 at 2026-03-07T01:34:33Z",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "temp=C:\\Users\\Administrator\\AppData\\Local\\Temp\\run-1.txt "
                                "uuid=123e4567-e89b-12d3-a456-426614174000"
                            ),
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ],
        }

        cache = RedisPromptCache("redis://unused", "test-prefix")
        cache.client = FakeRedis()

        evaluation = await cache.evaluate_anthropic_request_detailed(payload)

        assert evaluation.status == "miss"
        assert evaluation.source == "explicit"
        assert evaluation.segment_results[0].volatility_tags == [
            "volatile_timestamp",
            "volatile_request_id",
        ]
        assert evaluation.segment_results[1].volatility_tags == [
            "volatile_uuid",
            "volatile_temp_path",
        ]
        assert summarize_prompt_cache_volatility(evaluation.segment_results) == (
            "system[0]:volatile_timestamp|volatile_request_id,"
            "message[0:0]:volatile_uuid|volatile_temp_path"
        )
        assert summarize_prompt_cache_normalization(evaluation.segment_results) == (
            "system[0]:normalized_request_id|normalized_timestamp,"
            "message[0:0]:normalized_uuid|normalized_temp_path"
        )

    @pytest.mark.asyncio
    async def test_normalization_allows_hit_across_volatile_value_changes(self):
        first_payload = {
            "model": "claude-sonnet-4-6",
            "system": [
                {
                    "type": "text",
                    "text": "request_id=req_alpha123 at 2026-03-07T01:34:33Z",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "temp=C:\\Users\\Administrator\\AppData\\Local\\Temp\\run-a.txt "
                                "uuid=123e4567-e89b-12d3-a456-426614174000"
                            ),
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ],
        }
        second_payload = {
            "model": "claude-sonnet-4-6",
            "system": [
                {
                    "type": "text",
                    "text": "request_id=req_beta987 at 2026-03-07T02:45:59Z",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "temp=C:\\Users\\Administrator\\AppData\\Local\\Temp\\run-b.txt "
                                "uuid=123e4567-e89b-12d3-a456-426614174999"
                            ),
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ],
        }

        cache = RedisPromptCache("redis://unused", "test-prefix")
        cache.client = FakeRedis()

        evaluation_first = await cache.evaluate_anthropic_request_detailed(
            first_payload
        )
        evaluation_second = await cache.evaluate_anthropic_request_detailed(
            second_payload
        )

        assert evaluation_first.status == "miss"
        assert evaluation_first.source == "explicit"
        assert evaluation_second.status == "hit"
        assert evaluation_second.source == "explicit"
        assert all(
            segment.status == "hit" for segment in evaluation_second.segment_results
        )

    @pytest.mark.asyncio
    async def test_shadow_evaluation_applies_to_plain_system_and_string_message(self):
        first_payload = {
            "model": "claude-sonnet-4-6",
            "system": "static system context",
            "messages": [{"role": "user", "content": "Hello from request_id=req_a1"}],
        }
        second_payload = {
            "model": "claude-sonnet-4-6",
            "system": "static system context",
            "messages": [{"role": "user", "content": "Hello from request_id=req_b2"}],
        }

        cache = RedisPromptCache("redis://unused", "test-prefix")
        cache.client = FakeRedis()

        evaluation_first = await cache.evaluate_anthropic_request_detailed(
            first_payload
        )
        evaluation_second = await cache.evaluate_anthropic_request_detailed(
            second_payload
        )

        assert evaluation_first.source == "shadow"
        assert evaluation_first.status == "miss"
        assert [segment.label for segment in evaluation_first.segment_results] == [
            "system[0]",
            "message[0:0]",
        ]
        assert evaluation_second.source == "shadow"
        assert evaluation_second.status == "hit"
