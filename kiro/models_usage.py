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
Pydantic models for Kiro account usage and credits information.

These models represent the GetUsageLimits response used by the Kiro IDE to
show subscription state, monthly credits, and any bonus/free-trial windows.
"""

from typing import List, Optional, Literal

from pydantic import BaseModel, Field


class KiroFreeTrialInfo(BaseModel):
    """
    Free trial usage information for a usage bucket.

    Attributes:
        current_usage: Usage consumed inside the free-trial bucket.
        current_usage_with_precision: Usage consumed with full backend precision.
        free_trial_expiry: Expiration timestamp for the free trial.
        free_trial_status: Current free trial status.
        usage_limit: Free trial usage cap.
        usage_limit_with_precision: Free trial cap with backend precision.
    """

    current_usage: float = Field(alias="currentUsage")
    current_usage_with_precision: float = Field(alias="currentUsageWithPrecision")
    free_trial_expiry: Optional[str] = Field(default=None, alias="freeTrialExpiry")
    free_trial_status: Optional[str] = Field(default=None, alias="freeTrialStatus")
    usage_limit: float = Field(alias="usageLimit")
    usage_limit_with_precision: float = Field(alias="usageLimitWithPrecision")


class KiroUsageBonus(BaseModel):
    """
    Bonus usage grant attached to a primary usage bucket.

    Attributes:
        current_usage: Usage consumed from the bonus grant.
        current_usage_with_precision: Usage consumed with backend precision.
        expiry: Expiration timestamp for the bonus grant.
        usage_limit: Total bonus allowance.
        usage_limit_with_precision: Bonus allowance with backend precision.
    """

    current_usage: float = Field(alias="currentUsage")
    current_usage_with_precision: float = Field(alias="currentUsageWithPrecision")
    expiry: Optional[str] = None
    usage_limit: float = Field(alias="usageLimit")
    usage_limit_with_precision: float = Field(alias="usageLimitWithPrecision")


class KiroUsageBucket(BaseModel):
    """
    One usage bucket returned by GetUsageLimits.

    Attributes:
        resource_type: Logical resource type, for example CREDIT.
        display_name: Human-friendly singular display name.
        display_name_plural: Human-friendly plural display name.
        currency: Currency for overage fields.
        unit: Unit for the quota bucket.
        current_usage: Current consumed amount.
        current_usage_with_precision: Consumed amount with backend precision.
        usage_limit: Total bucket allowance.
        usage_limit_with_precision: Total bucket allowance with backend precision.
        current_overages: Overage amount currently billed.
        current_overages_with_precision: Overage amount with backend precision.
        overage_cap: Maximum allowed overage.
        overage_cap_with_precision: Maximum overage with backend precision.
        overage_rate: Per-unit overage rate.
        overage_charges: Current overage charges.
        next_date_reset: Next reset timestamp for this usage bucket.
        free_trial_info: Optional free-trial information.
        bonuses: Optional bonus grants attached to this bucket.
        remaining_usage: Derived remaining allowance.
    """

    resource_type: str = Field(alias="resourceType")
    display_name: str = Field(alias="displayName")
    display_name_plural: str = Field(alias="displayNamePlural")
    currency: Optional[str] = None
    unit: Optional[str] = None
    current_usage: float = Field(alias="currentUsage")
    current_usage_with_precision: float = Field(alias="currentUsageWithPrecision")
    usage_limit: float = Field(alias="usageLimit")
    usage_limit_with_precision: float = Field(alias="usageLimitWithPrecision")
    current_overages: float = Field(alias="currentOverages")
    current_overages_with_precision: float = Field(alias="currentOveragesWithPrecision")
    overage_cap: float = Field(alias="overageCap")
    overage_cap_with_precision: float = Field(alias="overageCapWithPrecision")
    overage_rate: float = Field(alias="overageRate")
    overage_charges: float = Field(alias="overageCharges")
    next_date_reset: Optional[str] = Field(default=None, alias="nextDateReset")
    free_trial_info: Optional[KiroFreeTrialInfo] = Field(default=None, alias="freeTrialInfo")
    bonuses: List[KiroUsageBonus] = Field(default_factory=list)
    remaining_usage: float = 0


class KiroSubscriptionInfo(BaseModel):
    """
    Subscription information attached to the current account.

    Attributes:
        subscription_title: Human-readable subscription title.
        subscription_type: Backend subscription type.
        overage_capability: Overage capability for the account.
        upgrade_capability: Upgrade capability for the account.
        subscription_management_target: Whether the account can self-manage.
    """

    subscription_title: str = Field(alias="subscriptionTitle")
    subscription_type: str = Field(alias="type")
    overage_capability: Optional[str] = Field(default=None, alias="overageCapability")
    upgrade_capability: Optional[str] = Field(default=None, alias="upgradeCapability")
    subscription_management_target: Optional[str] = Field(default=None, alias="subscriptionManagementTarget")


class KiroOverageConfiguration(BaseModel):
    """
    Overage settings for the current account.

    Attributes:
        overage_status: Whether overage is enabled or disabled.
    """

    overage_status: Optional[str] = Field(default=None, alias="overageStatus")


class KiroUserInfo(BaseModel):
    """
    Account identity metadata returned by GetUsageLimits.

    Attributes:
        user_id: Stable internal user identifier.
        email: Optional email if explicitly requested.
    """

    user_id: Optional[str] = Field(default=None, alias="userId")
    email: Optional[str] = None


class KiroUsageLimitsResponse(BaseModel):
    """
    Structured Kiro account usage response for API clients and the UI.

    Attributes:
        subscription_info: High-level subscription metadata.
        overage_configuration: Overage configuration state.
        usage_breakdown_list: One or more usage buckets.
        next_date_reset: Global reset timestamp.
        days_until_reset: Whole days until the global reset.
        user_info: Account identity metadata.
        fetched_at: ISO-8601 timestamp when the gateway fetched the data.
    """

    subscription_info: KiroSubscriptionInfo = Field(alias="subscriptionInfo")
    overage_configuration: Optional[KiroOverageConfiguration] = Field(default=None, alias="overageConfiguration")
    usage_breakdown_list: List[KiroUsageBucket] = Field(alias="usageBreakdownList")
    next_date_reset: Optional[str] = Field(default=None, alias="nextDateReset")
    days_until_reset: Optional[int] = Field(default=None, alias="daysUntilReset")
    user_info: Optional[KiroUserInfo] = Field(default=None, alias="userInfo")
    fetched_at: str


class KiroUsageDashboardAccount(BaseModel):
    """
    One account card in the server-managed usage dashboard.

    Attributes:
        account_id: Stable identifier for the configured account.
        name: Display label shown in the dashboard.
        auth_source: Credential source type.
        region: AWS region used for the account.
        status: Load status for this account.
        usage: Usage payload when loading succeeded.
        error: User-facing error message when loading failed.
    """

    account_id: str
    name: str
    auth_source: str
    region: str
    status: Literal["ok", "error"]
    usage: Optional[KiroUsageLimitsResponse] = None
    error: Optional[str] = None


class KiroUsageDashboardResponse(BaseModel):
    """
    Aggregated multi-account usage response for the dashboard page.

    Attributes:
        accounts: Usage cards for all configured accounts.
        generated_at: ISO-8601 timestamp when the snapshot was built.
    """

    accounts: List[KiroUsageDashboardAccount]
    generated_at: str
