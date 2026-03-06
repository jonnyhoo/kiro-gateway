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

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import httpx
from loguru import logger

from kiro.auth import AuthType, KiroAuthManager
from kiro.config import KIRO_USAGE_ACCOUNTS_FILE
from kiro.models_usage import (
    KiroUsageDashboardAccount,
    KiroUsageDashboardResponse,
    KiroUsageLimitsResponse,
)
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


def _load_server_managed_usage_accounts() -> List[Dict[str, Any]]:
    """
    Load additional server-managed dashboard accounts from JSON config.

    Returns:
        List of configured account dictionaries. Returns an empty list when the
        config file is not configured or does not exist.

    Raises:
        ValueError: If the config file is malformed.
    """
    if not KIRO_USAGE_ACCOUNTS_FILE:
        return []

    config_path = Path(KIRO_USAGE_ACCOUNTS_FILE).expanduser()
    if not config_path.exists():
        logger.warning(f"KIRO_USAGE_ACCOUNTS_FILE not found: {config_path}")
        return []

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Failed to parse usage accounts config: {error}") from error

    if isinstance(payload, dict):
        payload = payload.get("accounts", [])

    if not isinstance(payload, list):
        raise ValueError("Usage accounts config must be a JSON array or an object with an 'accounts' array")

    normalized_accounts: List[Dict[str, Any]] = []
    for index, account in enumerate(payload, start=1):
        if not isinstance(account, dict):
            raise ValueError(f"Usage account entry #{index} must be a JSON object")

        name = str(account.get("name") or f"Account {index}").strip()
        region = str(account.get("region") or "us-east-1").strip()
        normalized_accounts.append({
            "account_id": str(account.get("account_id") or f"server-{index}"),
            "name": name,
            "region": region,
            "auth_source": "creds_file" if account.get("creds_file") else "refresh_token" if account.get("refresh_token") else "sqlite_db" if account.get("sqlite_db") else "unknown",
            "include_email": bool(account.get("include_email", True)),
            "manager": KiroAuthManager(
                refresh_token=account.get("refresh_token"),
                profile_arn=account.get("profile_arn"),
                region=region,
                creds_file=account.get("creds_file"),
                sqlite_db=account.get("sqlite_db"),
            ),
        })

    return normalized_accounts


async def fetch_usage_dashboard(
    auth_manager: KiroAuthManager,
    shared_client: httpx.AsyncClient,
) -> KiroUsageDashboardResponse:
    """
    Fetch usage data for the current account plus optional server-managed extras.

    Args:
        auth_manager: Primary auth manager from the running gateway.
        shared_client: Shared async HTTP client from the FastAPI app state.

    Returns:
        Aggregated dashboard response with one card per account.
    """
    account_specs: List[Dict[str, Any]] = [
        {
            "account_id": "current-gateway",
            "name": "Current gateway",
            "region": auth_manager.region,
            "auth_source": auth_manager.auth_type.value,
            "include_email": True,
            "manager": auth_manager,
        },
        *_load_server_managed_usage_accounts(),
    ]

    accounts: List[KiroUsageDashboardAccount] = []
    for spec in account_specs:
        try:
            usage = await fetch_usage_limits(
                spec["manager"],
                shared_client,
                is_email_required=bool(spec.get("include_email", True)),
            )
            accounts.append(KiroUsageDashboardAccount(
                account_id=spec["account_id"],
                name=spec["name"],
                auth_source=spec["auth_source"],
                region=spec["region"],
                status="ok",
                usage=usage,
            ))
        except Exception as error:
            logger.error(f"Failed to fetch usage for {spec['name']}: {error}")
            accounts.append(KiroUsageDashboardAccount(
                account_id=spec["account_id"],
                name=spec["name"],
                auth_source=spec["auth_source"],
                region=spec["region"],
                status="error",
                error=str(error),
            ))

    return KiroUsageDashboardResponse(
        accounts=accounts,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


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
