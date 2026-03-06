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
FastAPI routes for account usage limits and a polished multi-account dashboard.
"""

import httpx

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger

from kiro.auth import KiroAuthManager
from kiro.models_usage import KiroUsageDashboardResponse, KiroUsageLimitsResponse
from kiro.routes_openai import verify_api_key
from kiro.usage_limits import fetch_usage_dashboard, fetch_usage_limits


USAGE_DASHBOARD_HTML = r"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Kiro Usage Dashboard</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #06101f;
      --panel: rgba(13, 19, 34, 0.82);
      --panel-strong: rgba(16, 24, 42, 0.94);
      --line: rgba(148, 163, 184, 0.16);
      --text: #e8eefc;
      --muted: #98a8c4;
      --blue: #62a8ff;
      --violet: #8b5cf6;
      --green: #34d399;
      --red: #f87171;
      --gold: #fbbf24;
      --shadow: 0 18px 56px rgba(3, 9, 23, 0.46);
      --radius: 24px;
      --radius-md: 18px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      font-family: Inter, "SF Pro Display", "Segoe UI Variable", "Segoe UI", system-ui, sans-serif;
      background:
        radial-gradient(circle at top left, rgba(98,168,255,.18), transparent 32%),
        radial-gradient(circle at top right, rgba(139,92,246,.15), transparent 28%),
        linear-gradient(180deg, #08111f 0%, #060d18 52%, #040911 100%);
      letter-spacing: -.01em;
    }
    .shell { width: min(1380px, calc(100% - 32px)); margin: 0 auto; padding: 28px 0 48px; }
    .hero, .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      backdrop-filter: blur(20px);
      -webkit-backdrop-filter: blur(20px);
    }
    .hero { position: relative; overflow: hidden; padding: 28px; }
    .hero::before {
      content: "";
      position: absolute;
      inset: -22% auto auto -8%;
      width: 340px;
      height: 340px;
      background: radial-gradient(circle, rgba(98,168,255,.22), transparent 68%);
      pointer-events: none;
    }
    .hero::after {
      content: "";
      position: absolute;
      inset: auto -12% -34% auto;
      width: 320px;
      height: 320px;
      background: radial-gradient(circle, rgba(139,92,246,.20), transparent 68%);
      pointer-events: none;
    }
    .hero-grid {
      position: relative;
      z-index: 1;
      display: grid;
      grid-template-columns: minmax(0, 1.25fr) minmax(320px, 0.8fr);
      gap: 20px;
    }
    .eyebrow {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 14px;
      border-radius: 999px;
      border: 1px solid rgba(98,168,255,.26);
      background: rgba(6, 11, 21, 0.76);
      color: #dce7ff;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: .08em;
      text-transform: uppercase;
    }
    h1 {
      margin: 18px 0 12px;
      font-size: clamp(34px, 5vw, 56px);
      line-height: 1.02;
      letter-spacing: -.05em;
    }
    h2, h3, p { margin: 0; }
    .hero-copy, .muted { color: var(--muted); line-height: 1.72; }
    .hero-actions, .card-actions { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 22px; }
    button {
      appearance: none;
      border: none;
      cursor: pointer;
      border-radius: 999px;
      padding: 12px 18px;
      color: white;
      font-size: 14px;
      font-weight: 700;
      letter-spacing: .01em;
      background: linear-gradient(135deg, #4f8cff, #6f77ff 58%, #8b5cf6);
      box-shadow: 0 10px 24px rgba(79,140,255,.26);
      transition: transform .18s ease, box-shadow .18s ease, opacity .18s ease;
    }
    button:hover { transform: translateY(-1px); box-shadow: 0 16px 28px rgba(79,140,255,.3); }
    button.secondary {
      background: rgba(11, 18, 32, 0.92);
      border: 1px solid var(--line);
      box-shadow: none;
      color: #dfe8fb;
    }
    .hero-side {
      display: grid;
      gap: 14px;
      align-content: start;
      padding: 20px;
      border-radius: 20px;
      border: 1px solid var(--line);
      background: var(--panel-strong);
    }
    .summary-grid, .accounts-grid, .mini-grid {
      display: grid;
      gap: 14px;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    }
    .summary, .mini, .account, .quota-card {
      border-radius: 18px;
      border: 1px solid rgba(148,163,184,.12);
      background: linear-gradient(180deg, rgba(10,16,29,.96), rgba(7,12,23,.92));
      padding: 18px;
    }
    .summary-label, .mini-label, .quota-label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: .08em;
      text-transform: uppercase;
    }
    .summary-value {
      margin-top: 10px;
      font-size: clamp(24px, 4vw, 36px);
      font-weight: 800;
      letter-spacing: -.05em;
    }
    .summary-note {
      margin-top: 8px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.6;
    }
    .section { margin-top: 22px; display: grid; gap: 16px; }
    .panel { padding: 20px; }
    .section-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 4px;
    }
    .section-copy { color: var(--muted); line-height: 1.6; }
    .toggle {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 10px 14px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(7, 12, 22, 0.76);
      color: #dde7fb;
      user-select: none;
    }
    .toggle input { width: auto; margin: 0; accent-color: var(--blue); }
    .status {
      padding: 14px 16px;
      border-radius: 16px;
      border: 1px solid var(--line);
      background: rgba(7, 12, 22, 0.76);
      color: var(--muted);
      line-height: 1.6;
    }
    .status.ok { color: #c4f6df; border-color: rgba(52,211,153,.26); }
    .status.error { color: #fecaca; border-color: rgba(248,113,113,.26); white-space: pre-wrap; }
    .accounts-grid { grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
    .account { display: grid; gap: 16px; min-height: 340px; }
    .account-top {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
    }
    .account-title h3 { font-size: 24px; letter-spacing: -.04em; }
    .account-title p { margin-top: 8px; color: var(--muted); font-size: 14px; line-height: 1.55; }
    .badges { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border-radius: 999px;
      padding: 7px 11px;
      font-size: 12px;
      font-weight: 700;
      background: rgba(8, 13, 23, 0.85);
      border: 1px solid var(--line);
      color: #dce7ff;
    }
    .pill.ok { color: #bdf3d9; border-color: rgba(52,211,153,.28); }
    .pill.warn { color: #fde68a; border-color: rgba(251,191,36,.28); }
    .pill.error { color: #fecaca; border-color: rgba(248,113,113,.28); }
    .quota-grid { display: grid; gap: 12px; }
    .quota-card { display: grid; gap: 10px; }
    .quota-top {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
      flex-wrap: wrap;
    }
    .quota-remaining {
      font-size: 30px;
      font-weight: 800;
      letter-spacing: -.05em;
    }
    .quota-meta { color: var(--muted); font-size: 14px; }
    .meter {
      height: 14px;
      border-radius: 999px;
      overflow: hidden;
      background: rgba(20, 31, 57, 0.94);
      border: 1px solid rgba(148,163,184,.08);
    }
    .meter > span {
      display: block;
      width: 0%;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, #60a5fa, #6f77ff 55%, #22c55e);
      box-shadow: 0 0 24px rgba(96,165,250,.34);
      transition: width .28s ease;
    }
    .mini-value {
      margin-top: 8px;
      font-size: 18px;
      font-weight: 700;
      word-break: break-word;
    }
    .details {
      border-radius: 16px;
      border: 1px solid rgba(148,163,184,.12);
      background: rgba(7, 12, 22, 0.76);
      overflow: hidden;
    }
    .details summary {
      cursor: pointer;
      list-style: none;
      padding: 15px 18px;
      font-weight: 700;
      color: #dfe8fb;
    }
    .details summary::-webkit-details-marker { display: none; }
    .details-body { padding: 0 18px 18px; }
    pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: "SF Mono", "Cascadia Code", Consolas, monospace;
      font-size: 12px;
      line-height: 1.68;
      color: #b7c8ea;
    }
    .empty {
      display: grid;
      place-items: center;
      gap: 12px;
      text-align: center;
      padding: 34px 22px;
      border-radius: 18px;
      border: 1px dashed rgba(148,163,184,.24);
      background: rgba(7, 12, 22, 0.5);
      color: var(--muted);
    }
    .empty strong { color: #f4f8ff; font-size: 18px; }
    .mono { font-family: "SF Mono", "Cascadia Code", Consolas, monospace; }
    @media (max-width: 1040px) { .hero-grid { grid-template-columns: 1fr; } }
    @media (max-width: 720px) {
      .shell { width: min(100% - 20px, 1380px); padding-top: 18px; }
      .hero, .panel { padding: 18px; }
      h1 { font-size: 34px; }
      .section-head, .account-top, .quota-top { align-items: stretch; }
      .summary-grid, .accounts-grid, .mini-grid { grid-template-columns: 1fr; }
      button { width: 100%; justify-content: center; }
    }
    @media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation: none !important; transition: none !important; } }
  </style>
</head>
<body>
  <div class='shell'>
    <section class='hero'>
      <div class='hero-grid'>
        <div>
          <div class='eyebrow'>Kiro usage control room</div>
          <h1>Open once. See every server-managed account immediately.</h1>
          <p class='hero-copy'>This dashboard is built for a private, password-protected operator workflow. All account credentials stay on the server, the roster auto-loads on open, and the layout scales cleanly as accounts are added or removed from the server config.</p>
          <div class='hero-actions'>
            <button type='button' id='refreshAllBtn'>Refresh all accounts</button>
            <button type='button' id='toggleDetailsBtn' class='secondary'>Toggle raw payloads</button>
          </div>
        </div>
        <aside class='hero-side'>
          <h2>Server-managed roster</h2>
          <p>Perfect for personal ops: add or remove account sources on the server, then open the page behind one password and get the whole picture instantly.</p>
          <div class='summary-grid'>
            <div class='summary'><div class='summary-label'>Tracked accounts</div><div class='summary-value' id='summaryAccounts'>0</div><div class='summary-note'>Every configured source renders into its own adaptive card.</div></div>
            <div class='summary'><div class='summary-label'>Remaining credits</div><div class='summary-value' id='summaryCredits'>-</div><div class='summary-note'>Combined remaining / total across all successful credit buckets.</div></div>
            <div class='summary'><div class='summary-label'>Nearest reset</div><div class='summary-value' id='summaryReset'>-</div><div class='summary-note'>The earliest next reset across the roster.</div></div>
          </div>
        </aside>
      </div>
    </section>

    <section class='section'>
      <div class='panel'>
        <div class='section-head'>
          <div>
            <h2>Live account overview</h2>
            <p class='section-copy'>No browser-stored API keys, no per-device setup. This page reads a server-side account list and refreshes all cards together.</p>
          </div>
          <label class='toggle'><input id='autoRefreshToggle' type='checkbox'> Auto refresh every 5 minutes</label>
        </div>
      </div>

      <div id='statusLine' class='status'>Loading usage…</div>
      <div id='accountsGrid' class='accounts-grid'></div>
    </section>
  </div>

  <script>
    const AUTO_REFRESH_KEY = 'kiro_usage_auto_refresh';
    const AUTO_REFRESH_MS = 5 * 60 * 1000;
    const CURRENT_PAGE_PATH = window.location.pathname.replace(/\/+$/, '');
    const PATH_BASE = CURRENT_PAGE_PATH.endsWith('/usage') ? (CURRENT_PAGE_PATH.slice(0, -'/usage'.length) || '') : '';
    const DASHBOARD_API = `${PATH_BASE}/v1/usage/all`;

    const refreshAllBtn = document.getElementById('refreshAllBtn');
    const toggleDetailsBtn = document.getElementById('toggleDetailsBtn');
    const autoRefreshToggle = document.getElementById('autoRefreshToggle');
    const statusLine = document.getElementById('statusLine');
    const accountsGrid = document.getElementById('accountsGrid');
    const summaryAccounts = document.getElementById('summaryAccounts');
    const summaryCredits = document.getElementById('summaryCredits');
    const summaryReset = document.getElementById('summaryReset');

    let dashboardResponse = { accounts: [], generated_at: null };
    let refreshTimer = null;
    let detailsOpen = false;

    function escapeHtml(value) {
      return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    function formatValue(value) {
      if (value === null || value === undefined || value === '') return '-';
      if (typeof value === 'number') return Number.isInteger(value) ? `${value}` : value.toFixed(2);
      return `${value}`;
    }

    function formatDate(value) {
      if (!value) return '-';
      const parsed = new Date(value);
      if (Number.isNaN(parsed.getTime())) return `${value}`;
      return parsed.toLocaleString();
    }

    function updateStatus(message, tone = 'default') {
      statusLine.className = 'status';
      if (tone === 'ok') statusLine.classList.add('ok');
      if (tone === 'error') statusLine.classList.add('error');
      statusLine.textContent = message;
    }

    function renderSummary() {
      const accounts = Array.isArray(dashboardResponse.accounts) ? dashboardResponse.accounts : [];
      summaryAccounts.textContent = `${accounts.length}`;
      let remaining = 0;
      let total = 0;
      let nearestReset = null;
      accounts.forEach((account) => {
        const buckets = Array.isArray(account.usage?.usage_breakdown_list) ? account.usage.usage_breakdown_list : [];
        buckets.forEach((bucket) => {
          remaining += Number(bucket.remaining_usage || 0);
          total += Number(bucket.usage_limit || 0);
        });
        const reset = account.usage?.next_date_reset ? new Date(account.usage.next_date_reset) : null;
        if (reset && !Number.isNaN(reset.getTime()) && (!nearestReset || reset < nearestReset)) {
          nearestReset = reset;
        }
      });
      summaryCredits.textContent = total > 0 ? `${formatValue(remaining)} / ${formatValue(total)}` : '-';
      summaryReset.textContent = nearestReset ? nearestReset.toLocaleString() : '-';
    }

    function bucketMarkup(bucket) {
      const used = Number(bucket.current_usage || 0);
      const total = Number(bucket.usage_limit || 0);
      const remaining = Number(bucket.remaining_usage || Math.max(total - used, 0));
      const percent = total > 0 ? Math.min((used / total) * 100, 100) : 0;
      const bonusMarkup = Array.isArray(bucket.bonuses) && bucket.bonuses.length > 0
        ? bucket.bonuses.map((bonus) => `<div class='mini'><div class='mini-label'>Bonus</div><div class='mini-value'>${formatValue(bonus.current_usage)} / ${formatValue(bonus.usage_limit)}</div><div class='summary-note'>Expires ${formatDate(bonus.expiry)}</div></div>`).join('')
        : `<div class='mini'><div class='mini-label'>Bonus</div><div class='mini-value'>-</div><div class='summary-note'>No bonus credits reported.</div></div>`;
      return `
        <section class='quota-card'>
          <div class='quota-top'>
            <div><div class='quota-label'>${escapeHtml(bucket.display_name_plural || bucket.display_name)}</div><div class='quota-remaining'>${formatValue(remaining)} left</div></div>
            <div class='quota-meta'>${formatValue(used)} used / ${formatValue(total)} total</div>
          </div>
          <div class='meter'><span style='width:${percent.toFixed(2)}%'></span></div>
          <div class='mini-grid'>
            <div class='mini'><div class='mini-label'>Reset</div><div class='mini-value'>${escapeHtml(formatDate(bucket.next_date_reset))}</div></div>
            <div class='mini'><div class='mini-label'>Free trial</div><div class='mini-value'>${bucket.free_trial_info ? `${formatValue(bucket.free_trial_info.current_usage)} / ${formatValue(bucket.free_trial_info.usage_limit)}` : '-'}</div></div>
            ${bonusMarkup}
          </div>
        </section>`;
    }

    function accountMarkup(account) {
      const stateClass = account.status === 'ok' ? 'ok' : 'error';
      const stateText = account.status === 'ok' ? 'Connected' : 'Needs attention';
      const usage = account.usage;
      const buckets = Array.isArray(usage?.usage_breakdown_list) ? usage.usage_breakdown_list : [];
      const rawPayload = usage ? JSON.stringify(usage, null, 2) : account.error || 'No payload available';
      return `
        <article class='account'>
          <div class='account-top'>
            <div class='account-title'>
              <h3>${escapeHtml(account.name)}</h3>
              <p>${escapeHtml(account.region)} · ${escapeHtml(account.auth_source)}</p>
              <div class='badges'>
                <span class='pill ${stateClass}'>${stateText}</span>
                <span class='pill'>${escapeHtml(usage?.subscription_info?.subscription_title || 'Unknown plan')}</span>
                <span class='pill'>${escapeHtml(account.account_id)}</span>
              </div>
            </div>
          </div>
          <div class='mini-grid'>
            <div class='mini'><div class='mini-label'>User</div><div class='mini-value'>${escapeHtml(usage?.user_info?.email || usage?.user_info?.user_id || '-')}</div></div>
            <div class='mini'><div class='mini-label'>Global reset</div><div class='mini-value'>${escapeHtml(formatDate(usage?.next_date_reset))}</div></div>
            <div class='mini'><div class='mini-label'>Fetched</div><div class='mini-value'>${escapeHtml(formatDate(usage?.fetched_at || dashboardResponse.generated_at))}</div></div>
          </div>
          ${account.error ? `<div class='status error'>${escapeHtml(account.error)}</div>` : ''}
          ${buckets.length ? buckets.map(bucketMarkup).join('') : `<div class='empty'><strong>No usage buckets returned</strong><span>This account did not expose any credit windows in the latest snapshot.</span></div>`}
          <details class='details' ${detailsOpen ? 'open' : ''}><summary>Raw usage payload</summary><div class='details-body'><pre>${escapeHtml(rawPayload)}</pre></div></details>
        </article>`;
    }

    function renderAccounts() {
      const accounts = Array.isArray(dashboardResponse.accounts) ? dashboardResponse.accounts : [];
      accountsGrid.innerHTML = accounts.length
        ? accounts.map(accountMarkup).join('')
        : `<div class='empty'><strong>No server-managed accounts configured</strong><span>Add sources on the server so this protected page can render them automatically.</span></div>`;
      renderSummary();
    }

    async function loadDashboard(silent = false) {
      if (!silent) updateStatus('Loading server-managed accounts…');
      try {
        const response = await fetch(DASHBOARD_API, { credentials: 'same-origin' });
        const text = await response.text();
        if (!response.ok) throw new Error(text || `HTTP ${response.status}`);
        dashboardResponse = JSON.parse(text);
        renderAccounts();
        const failures = dashboardResponse.accounts.filter((account) => account.status === 'error').length;
        if (failures) updateStatus(`Loaded ${dashboardResponse.accounts.length} accounts with ${failures} issue${failures === 1 ? '' : 's'}.`, 'error');
        else updateStatus(`Loaded ${dashboardResponse.accounts.length} account${dashboardResponse.accounts.length === 1 ? '' : 's'}.`, 'ok');
      } catch (error) {
        dashboardResponse = { accounts: [], generated_at: null };
        renderAccounts();
        updateStatus(`Failed to load dashboard: ${error.message}`, 'error');
      }
    }

    function setAutoRefresh(enabled) {
      localStorage.setItem(AUTO_REFRESH_KEY, enabled ? '1' : '0');
      autoRefreshToggle.checked = enabled;
      if (refreshTimer) clearInterval(refreshTimer);
      refreshTimer = enabled ? setInterval(() => { void loadDashboard(true); }, AUTO_REFRESH_MS) : null;
    }

    refreshAllBtn.addEventListener('click', () => { void loadDashboard(); });
    toggleDetailsBtn.addEventListener('click', () => {
      detailsOpen = !detailsOpen;
      renderAccounts();
      updateStatus(detailsOpen ? 'Expanded raw payloads for all cards.' : 'Collapsed raw payloads.', 'ok');
    });
    autoRefreshToggle.addEventListener('change', () => setAutoRefresh(autoRefreshToggle.checked));

    setAutoRefresh(localStorage.getItem(AUTO_REFRESH_KEY) === '1');
    renderAccounts();
    void loadDashboard();
  </script>
</body>
</html>
"""


router = APIRouter(tags=["Usage"])


@router.get(
    "/v1/usage",
    response_model=KiroUsageLimitsResponse,
    response_model_by_alias=False,
    dependencies=[Depends(verify_api_key)],
)
async def get_usage(request: Request, include_email: bool = False) -> KiroUsageLimitsResponse:
    """
    Fetch Kiro account usage limits for the currently authenticated gateway account.

    Args:
        request: FastAPI request used to access application state.
        include_email: Whether to request email information from the backend.

    Returns:
        Structured Kiro usage limits payload.

    Raises:
        HTTPException: If upstream authentication or usage retrieval fails.
    """
    logger.info("Request to /v1/usage")

    auth_manager: KiroAuthManager = request.app.state.auth_manager
    shared_client = request.app.state.http_client

    try:
        return await fetch_usage_limits(
            auth_manager,
            shared_client,
            is_email_required=include_email,
        )
    except httpx.HTTPStatusError as error:
        message = f"Kiro usage request failed with HTTP {error.response.status_code}"
        logger.error(f"{message}: {error.response.text}")
        raise HTTPException(status_code=502, detail=message) from error
    except httpx.HTTPError as error:
        logger.error(f"Kiro usage request failed: {error}")
        raise HTTPException(status_code=502, detail="Failed to reach Kiro usage service") from error
    except ValueError as error:
        logger.error(f"Kiro usage response was invalid: {error}")
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.get(
    "/v1/usage/all",
    response_model=KiroUsageDashboardResponse,
    response_model_by_alias=False,
    dependencies=[Depends(verify_api_key)],
)
async def get_usage_dashboard_data(request: Request) -> KiroUsageDashboardResponse:
    """
    Fetch server-managed usage data for all configured dashboard accounts.

    Args:
        request: FastAPI request used to access application state.

    Returns:
        Aggregated dashboard response for all configured accounts.

    Raises:
        HTTPException: If the dashboard aggregation fails completely.
    """
    logger.info("Request to /v1/usage/all")

    auth_manager: KiroAuthManager = request.app.state.auth_manager
    shared_client = request.app.state.http_client

    try:
        return await fetch_usage_dashboard(auth_manager, shared_client)
    except ValueError as error:
        logger.error(f"Usage dashboard config error: {error}")
        raise HTTPException(status_code=500, detail=str(error)) from error
    except Exception as error:
        logger.error(f"Usage dashboard failed: {error}")
        raise HTTPException(status_code=500, detail="Failed to build usage dashboard") from error


@router.get('/usage', response_class=HTMLResponse)
async def usage_dashboard() -> HTMLResponse:
    """
    Render a polished server-managed browser dashboard for Kiro usage limits.

    Returns:
        Static HTML page that auto-loads `/v1/usage/all` and adapts the layout
        to however many accounts are configured on the server.
    """
    return HTMLResponse(content=USAGE_DASHBOARD_HTML)
