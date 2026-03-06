# -*- coding: utf-8 -*-

# Kiro Gateway
# https://github.com/jwadow/kiro-gateway
# Copyright (C) 2025 Jwadow
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Helpers for fetching Kiro account usage limits from the same internal API used
by the Kiro IDE usage dashboard.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

import httpx
from loguru import logger

from kiro.auth import AuthType, KiroAuthManager
from kiro.models_usage import KiroUsageLimitsResponse
from kiro.utils import get_kiro_headers


def _normalize_timestamp(value: Any) -> Any:
    """
    Normalize backend timestamp values to ISO-8601 strings.

    Args:
        value: Raw timestamp value from the backend.

    Returns:
        ISO-8601 string when the input is a unix timestamp, otherwise the
        original value.
    """
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    return value


def _build_usage_limits_params(auth_manager: KiroAuthManager) -> Dict[str, Any]:
    """
    Build query parameters for the Kiro GetUsageLimits endpoint.

    Args:
        auth_manager: Authentication manager with the active auth context.

    Returns:
        Dictionary of query parameters understood by the backend.
    """
    params: Dict[str, Any] = {
        "origin": "AI_EDITOR",
        "resourceType": "AGENTIC_REQUEST",
    }

    if auth_manager.auth_type == AuthType.KIRO_DESKTOP and auth_manager.profile_arn:
        params["profileArn"] = auth_manager.profile_arn

    return params


def _normalize_usage_limits_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize and enrich raw GetUsageLimits payload for API consumers.

    Args:
        payload: Raw JSON payload returned by the Kiro backend.

    Returns:
        Normalized payload with derived remaining usage and fetched timestamp.
    """
    normalized = dict(payload)
    usage_breakdown = []

    for bucket in payload.get("usageBreakdownList", []):
        bucket_copy = dict(bucket)
        current_usage = float(bucket_copy.get("currentUsage", 0) or 0)
        usage_limit = float(bucket_copy.get("usageLimit", 0) or 0)
        bucket_copy["nextDateReset"] = _normalize_timestamp(bucket_copy.get("nextDateReset"))

        free_trial_info = bucket_copy.get("freeTrialInfo")
        if isinstance(free_trial_info, dict):
            free_trial_copy = dict(free_trial_info)
            free_trial_copy["freeTrialExpiry"] = _normalize_timestamp(free_trial_copy.get("freeTrialExpiry"))
            bucket_copy["freeTrialInfo"] = free_trial_copy

        normalized_bonuses = []
        for bonus in bucket_copy.get("bonuses", []):
            if isinstance(bonus, dict):
                bonus_copy = dict(bonus)
                bonus_copy["expiry"] = _normalize_timestamp(bonus_copy.get("expiry"))
                normalized_bonuses.append(bonus_copy)
            else:
                normalized_bonuses.append(bonus)
        bucket_copy["bonuses"] = normalized_bonuses

        bucket_copy["remaining_usage"] = max(usage_limit - current_usage, 0)
        usage_breakdown.append(bucket_copy)

    normalized["usageBreakdownList"] = usage_breakdown
    normalized["nextDateReset"] = _normalize_timestamp(normalized.get("nextDateReset"))
    normalized["fetched_at"] = datetime.now(timezone.utc).isoformat()
    return normalized


async def fetch_usage_limits(
    auth_manager: KiroAuthManager,
    shared_client: httpx.AsyncClient,
    *,
    is_email_required: bool = False,
) -> KiroUsageLimitsResponse:
    """
    Fetch account usage limits from the Kiro backend.

    The request mirrors the internal IDE call to `GetUsageLimitsCommand`.

    Args:
        auth_manager: Authentication manager used to acquire access tokens.
        shared_client: Shared async HTTP client from the FastAPI app state.
        is_email_required: Whether the backend should include email information.

    Returns:
        Structured usage limits response.

    Raises:
        httpx.HTTPError: If the upstream request fails.
        ValueError: If the upstream response is malformed.
    """
    token = await auth_manager.get_access_token()
    headers = get_kiro_headers(auth_manager, token)
    params = _build_usage_limits_params(auth_manager)

    if is_email_required:
        params["isEmailRequired"] = "true"

    url = f"{auth_manager.q_host}/getUsageLimits"
    logger.info("Fetching Kiro usage limits from backend")
    logger.debug(f"Usage limits URL: {url}")

    response = await shared_client.get(url, headers=headers, params=params)
    response.raise_for_status()

    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Kiro usage limits response must be a JSON object")

    return KiroUsageLimitsResponse.model_validate(_normalize_usage_limits_payload(payload))
