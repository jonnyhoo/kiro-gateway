# -*- coding: utf-8 -*-

"""
Unit tests for Kiro usage limits fetching helpers.
"""

from datetime import datetime, timezone

from kiro.auth import AuthType
from kiro.usage_limits import _build_usage_limits_params, _normalize_usage_limits_payload


class TestUsageLimitHelpers:
    """Tests for usage limit helper functions."""

    def test_build_usage_params_for_kiro_desktop_includes_profile_arn(self, mock_auth_manager):
        """
        What it does: Builds usage query params for Kiro Desktop auth.
        Purpose: Ensure profileArn is forwarded only when required.
        """
        print("Setup: Desktop auth manager with profile ARN...")
        mock_auth_manager._auth_type = AuthType.KIRO_DESKTOP

        print("Action: Building usage params...")
        params = _build_usage_limits_params(mock_auth_manager)

        print(f"Result params: {params}")
        assert params["origin"] == "AI_EDITOR"
        assert params["resourceType"] == "AGENTIC_REQUEST"
        assert params["profileArn"] == mock_auth_manager.profile_arn

    def test_build_usage_params_for_sso_omits_profile_arn(self, mock_auth_manager):
        """
        What it does: Omits profileArn for AWS SSO auth.
        Purpose: Match the auth rules used by the rest of the gateway.
        """
        print("Setup: AWS SSO auth manager...")
        mock_auth_manager._auth_type = AuthType.AWS_SSO_OIDC

        print("Action: Building usage params...")
        params = _build_usage_limits_params(mock_auth_manager)

        print(f"Result params: {params}")
        assert "profileArn" not in params

    def test_normalize_usage_payload_adds_remaining_usage_and_timestamp(self):
        """
        What it does: Adds derived fields to raw usage response.
        Purpose: Ensure the frontend receives ready-to-render values.
        """
        print("Setup: Raw usage payload...")
        payload = {
            "subscriptionInfo": {"subscriptionTitle": "KIRO PRO", "type": "Q_DEVELOPER_STANDALONE_PRO"},
            "usageBreakdownList": [
                {
                    "resourceType": "CREDIT",
                    "displayName": "Credit",
                    "displayNamePlural": "Credits",
                    "currency": "USD",
                    "unit": "INVOCATIONS",
                    "currentUsage": 125,
                    "currentUsageWithPrecision": 125,
                    "usageLimit": 1000,
                    "usageLimitWithPrecision": 1000,
                    "currentOverages": 0,
                    "currentOveragesWithPrecision": 0,
                    "overageCap": 10000,
                    "overageCapWithPrecision": 10000,
                    "overageRate": 0.04,
                    "overageCharges": 0,
                    "bonuses": [],
                }
            ],
        }

        print("Action: Normalizing payload...")
        normalized = _normalize_usage_limits_payload(payload)

        print(f"Normalized payload: {normalized}")
        assert normalized["usageBreakdownList"][0]["remaining_usage"] == 875
        assert datetime.fromisoformat(normalized["fetched_at"]).tzinfo == timezone.utc

