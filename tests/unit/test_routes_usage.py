# -*- coding: utf-8 -*-

"""
Unit tests for usage endpoints and dashboard UI.
"""

from unittest.mock import AsyncMock, patch

from kiro.models_usage import KiroUsageDashboardResponse, KiroUsageLimitsResponse


class TestUsageRoutes:
    """Tests for usage API and dashboard routes."""

    def test_usage_dashboard_returns_html(self, test_client):
        """
        What it does: Verifies the usage dashboard page renders HTML.
        Purpose: Ensure users have a browser entry point for credits inspection.
        """
        print("Action: GET /usage...")
        response = test_client.get("/usage")

        print(f"Status: {response.status_code}")
        assert response.status_code == 200
        assert "Kiro Usage Dashboard" in response.text
        assert "/v1/usage/all" in response.text
        assert "Tracked accounts" in response.text
        assert "Server-managed roster" in response.text
        assert "Refresh all" in response.text

    def test_usage_api_requires_authentication(self, test_client):
        """
        What it does: Verifies the usage API is protected by the gateway API key.
        Purpose: Prevent exposing account credits to unauthenticated callers.
        """
        print("Action: GET /v1/usage without auth...")
        response = test_client.get("/v1/usage")

        print(f"Status: {response.status_code}")
        assert response.status_code == 401

    def test_usage_dashboard_api_requires_authentication(self, test_client):
        """
        What it does: Verifies aggregated dashboard data requires authentication.
        Purpose: Prevent exposing the server-managed roster without auth.
        """
        print("Action: GET /v1/usage/all without auth...")
        response = test_client.get("/v1/usage/all")

        print(f"Status: {response.status_code}")
        assert response.status_code == 401

    def test_usage_api_returns_normalized_payload(self, test_client, auth_headers):
        """
        What it does: Returns normalized usage data from the Kiro backend.
        Purpose: Ensure the frontend can render account quota information.
        """
        print("Setup: Mock usage limits fetcher...")
        response_model = KiroUsageLimitsResponse.model_validate({
            "subscriptionInfo": {
                "subscriptionTitle": "KIRO PRO",
                "type": "Q_DEVELOPER_STANDALONE_PRO",
                "overageCapability": "OVERAGE_CAPABLE",
                "upgradeCapability": "UPGRADE_CAPABLE",
                "subscriptionManagementTarget": "MANAGE",
            },
            "overageConfiguration": {"overageStatus": "DISABLED"},
            "usageBreakdownList": [
                {
                    "resourceType": "CREDIT",
                    "displayName": "Credit",
                    "displayNamePlural": "Credits",
                    "currency": "USD",
                    "unit": "INVOCATIONS",
                    "currentUsage": 250,
                    "currentUsageWithPrecision": 250,
                    "usageLimit": 1000,
                    "usageLimitWithPrecision": 1000,
                    "currentOverages": 0,
                    "currentOveragesWithPrecision": 0,
                    "overageCap": 10000,
                    "overageCapWithPrecision": 10000,
                    "overageRate": 0.04,
                    "overageCharges": 0,
                    "nextDateReset": "2026-04-01T00:00:00.000Z",
                    "bonuses": [],
                    "remaining_usage": 750,
                }
            ],
            "nextDateReset": "2026-04-01T00:00:00.000Z",
            "daysUntilReset": 25,
            "userInfo": {"userId": "user-123"},
            "fetched_at": "2026-03-06T00:00:00+00:00",
        })

        with patch("kiro.routes_usage.fetch_usage_limits", new=AsyncMock(return_value=response_model)):
            print("Action: GET /v1/usage...")
            response = test_client.get("/v1/usage", headers=auth_headers())

        print(f"Status: {response.status_code}, JSON: {response.json()}")
        assert response.status_code == 200
        assert response.json()["subscription_info"]["subscription_title"] == "KIRO PRO"
        assert response.json()["usage_breakdown_list"][0]["remaining_usage"] == 750

    def test_usage_dashboard_api_returns_server_managed_accounts(self, test_client, auth_headers):
        """
        What it does: Returns aggregated server-managed dashboard accounts.
        Purpose: Ensure the dashboard can auto-load accounts without browser-stored keys.
        """
        print("Setup: Mock usage dashboard fetcher...")
        dashboard_model = KiroUsageDashboardResponse.model_validate({
            "accounts": [
                {
                    "account_id": "current-gateway",
                    "name": "Current gateway",
                    "auth_source": "kiro_desktop",
                    "region": "us-east-1",
                    "status": "ok",
                    "usage": {
                        "subscriptionInfo": {
                            "subscriptionTitle": "KIRO PRO",
                            "type": "Q_DEVELOPER_STANDALONE_PRO",
                            "overageCapability": "OVERAGE_CAPABLE",
                            "upgradeCapability": "UPGRADE_CAPABLE",
                            "subscriptionManagementTarget": "MANAGE",
                        },
                        "overageConfiguration": {"overageStatus": "DISABLED"},
                        "usageBreakdownList": [],
                        "nextDateReset": "2026-04-01T00:00:00.000Z",
                        "daysUntilReset": 25,
                        "userInfo": {"userId": "user-123"},
                        "fetched_at": "2026-03-06T00:00:00+00:00",
                    },
                    "error": None,
                }
            ],
            "generated_at": "2026-03-06T00:00:00+00:00",
        })

        with patch("kiro.routes_usage.fetch_usage_dashboard", new=AsyncMock(return_value=dashboard_model)):
            print("Action: GET /v1/usage/all...")
            response = test_client.get("/v1/usage/all", headers=auth_headers())

        print(f"Status: {response.status_code}, JSON: {response.json()}")
        assert response.status_code == 200
        assert response.json()["accounts"][0]["name"] == "Current gateway"
        assert response.json()["accounts"][0]["usage"]["subscription_info"]["subscription_title"] == "KIRO PRO"
