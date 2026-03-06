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
from kiro.models_usage import KiroUsageLimitsResponse
from kiro.routes_openai import verify_api_key
from kiro.usage_limits import fetch_usage_limits


USAGE_DASHBOARD_HTML = r"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Kiro Usage Dashboard</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #07111f;
      --panel: rgba(12,18,32,.82);
      --panel-2: rgba(16,24,42,.9);
      --line: rgba(148,163,184,.15);
      --text: #e8eefc;
      --muted: #96a4c0;
      --blue: #62a8ff;
      --violet: #8b5cf6;
      --green: #34d399;
      --red: #f87171;
      --shadow: 0 16px 48px rgba(2,8,23,.45);
      --radius: 22px;
      --radius-sm: 16px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      font-family: Inter, "SF Pro Display", "Segoe UI Variable", "Segoe UI", system-ui, sans-serif;
      background:
        radial-gradient(circle at top left, rgba(98,168,255,.18), transparent 34%),
        radial-gradient(circle at top right, rgba(139,92,246,.16), transparent 28%),
        linear-gradient(180deg, #08111f, #060c18 55%, #040912);
      letter-spacing: -.01em;
    }
    .shell { width: min(1360px, calc(100% - 32px)); margin: 0 auto; padding: 28px 0 44px; }
    .hero, .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      backdrop-filter: blur(22px);
      -webkit-backdrop-filter: blur(22px);
    }
    .hero { padding: 28px; overflow: hidden; position: relative; }
    .hero::before {
      content: ""; position: absolute; inset: -20% auto auto -8%; width: 320px; height: 320px;
      background: radial-gradient(circle, rgba(98,168,255,.22), transparent 68%);
    }
    .hero::after {
      content: ""; position: absolute; inset: auto -10% -35% auto; width: 320px; height: 320px;
      background: radial-gradient(circle, rgba(139,92,246,.2), transparent 68%);
    }
    .hero-grid {
      position: relative; z-index: 1; display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(320px, .8fr); gap: 20px;
    }
    .eyebrow {
      display: inline-flex; align-items: center; gap: 8px;
      padding: 8px 14px; border-radius: 999px;
      background: rgba(8,13,25,.72); border: 1px solid rgba(98,168,255,.24);
      color: #d9e5ff; font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase;
    }
    h1 { margin: 18px 0 12px; font-size: clamp(34px, 5vw, 54px); line-height: 1.02; letter-spacing: -.05em; }
    h2, h3, p { margin: 0; }
    .hero p, .muted { color: var(--muted); line-height: 1.7; }
    .hero-actions, .stack-actions { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 22px; }
    button, .button {
      appearance: none; border: none; cursor: pointer; border-radius: 999px; padding: 12px 18px;
      color: white; font-size: 14px; font-weight: 700; letter-spacing: .01em;
      background: linear-gradient(135deg, #4f8cff, #6f77ff 58%, #8b5cf6);
      box-shadow: 0 10px 24px rgba(79,140,255,.28); transition: transform .18s ease, box-shadow .18s ease;
    }
    button:hover, .button:hover { transform: translateY(-1px); box-shadow: 0 16px 28px rgba(79,140,255,.3); }
    .secondary { background: rgba(12,18,32,.88); border: 1px solid var(--line); box-shadow: none; }
    .ghost { background: transparent; border: 1px solid var(--line); box-shadow: none; color: #dfe8fb; }
    .hero-side { display: grid; gap: 14px; align-content: start; padding: 20px; border-radius: calc(var(--radius) - 4px); background: var(--panel-2); border: 1px solid var(--line); }
    .summary-grid, .accounts-grid, .input-grid, .bucket-grid, .mini-grid {
      display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    }
    .summary, .mini, .bucket, .account {
      border-radius: 18px; border: 1px solid rgba(148,163,184,.12);
      background: linear-gradient(180deg, rgba(10,16,29,.95), rgba(8,13,24,.9));
      padding: 18px;
    }
    .summary-label, .mini-label { color: var(--muted); font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
    .summary-value { margin-top: 10px; font-size: clamp(24px, 4vw, 36px); font-weight: 800; letter-spacing: -.05em; }
    .summary-note { margin-top: 8px; color: var(--muted); font-size: 13px; line-height: 1.6; }
    .section { margin-top: 20px; display: grid; gap: 16px; }
    .panel { padding: 20px; }
    .section-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 4px; }
    .section-copy { color: var(--muted); line-height: 1.6; }
    .field { display: grid; gap: 8px; }
    .field label { color: #dfe8fb; font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
    input {
      width: 100%; background: rgba(6,11,21,.94); color: #f9fbff; border: 1px solid rgba(148,163,184,.16);
      border-radius: 14px; padding: 13px 14px; font: inherit; outline: none; transition: border-color .16s ease, box-shadow .16s ease;
    }
    input:focus { border-color: rgba(98,168,255,.8); box-shadow: 0 0 0 4px rgba(98,168,255,.16); }
    .toolbar { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 18px; }
    .toggle {
      display: inline-flex; align-items: center; gap: 10px; padding: 10px 14px; border-radius: 999px;
      background: rgba(7,12,22,.8); border: 1px solid var(--line); color: #dde7fb;
    }
    .toggle input { width: auto; margin: 0; accent-color: var(--blue); }
    .status { padding: 14px 16px; border-radius: 16px; border: 1px solid var(--line); background: rgba(7,12,22,.76); color: var(--muted); line-height: 1.6; }
    .status.ok { color: #c4f6df; border-color: rgba(52,211,153,.26); }
    .status.error { color: #fecaca; border-color: rgba(248,113,113,.26); white-space: pre-wrap; }
    .accounts-grid { grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
    .account { display: grid; gap: 16px; min-height: 320px; }
    .account-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
    .account-title h3 { font-size: 24px; letter-spacing: -.04em; }
    .account-title p { margin-top: 8px; color: var(--muted); font-size: 14px; line-height: 1.5; word-break: break-all; }
    .badges { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
    .pill {
      display: inline-flex; align-items: center; gap: 8px; border-radius: 999px; padding: 7px 11px;
      font-size: 12px; font-weight: 700; background: rgba(7,12,22,.85); border: 1px solid var(--line); color: #dce7ff;
    }
    .pill.ok { color: #bdf3d9; border-color: rgba(52,211,153,.28); }
    .pill.warn { color: #fde68a; border-color: rgba(251,191,36,.28); }
    .pill.error { color: #fecaca; border-color: rgba(248,113,113,.28); }
    .icon-button {
      width: 40px; height: 40px; border-radius: 50%; padding: 0; box-shadow: none;
      background: rgba(7,12,22,.82); border: 1px solid var(--line); color: #dce7ff;
    }
    .bucket { display: grid; gap: 10px; }
    .bucket-top { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; }
    .bucket-title { color: var(--muted); font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
    .bucket-value { font-size: 30px; font-weight: 800; letter-spacing: -.05em; }
    .bucket-meta { color: var(--muted); font-size: 14px; }
    .meter { height: 14px; border-radius: 999px; overflow: hidden; background: rgba(22,32,58,.95); border: 1px solid rgba(148,163,184,.08); }
    .meter > span { display: block; height: 100%; width: 0%; border-radius: inherit; background: linear-gradient(90deg, #60a5fa, #6f77ff 55%, #22c55e); box-shadow: 0 0 24px rgba(96,165,250,.36); transition: width .28s ease; }
    .mini-value { margin-top: 8px; font-size: 18px; font-weight: 700; word-break: break-word; }
    .footer { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; color: var(--muted); font-size: 13px; }
    .details { border-radius: 16px; border: 1px solid rgba(148,163,184,.12); background: rgba(7,12,22,.76); overflow: hidden; }
    .details summary { cursor: pointer; list-style: none; padding: 15px 18px; font-weight: 700; color: #dfe8fb; }
    .details summary::-webkit-details-marker { display: none; }
    .details-body { padding: 0 18px 18px; }
    pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-family: "SF Mono", "Cascadia Code", Consolas, monospace; font-size: 12px; line-height: 1.68; color: #b7c8ea; }
    .empty { display: grid; place-items: center; gap: 12px; text-align: center; padding: 32px 20px; border-radius: 18px; border: 1px dashed rgba(148,163,184,.24); background: rgba(7,12,22,.48); color: var(--muted); }
    .empty strong { color: #f4f8ff; font-size: 18px; }
    .mono { font-family: "SF Mono", "Cascadia Code", Consolas, monospace; }
    @media (max-width: 1040px) { .hero-grid { grid-template-columns: 1fr; } }
    @media (max-width: 720px) {
      .shell { width: min(100% - 20px, 1360px); padding-top: 18px; }
      .hero, .panel { padding: 18px; }
      h1 { font-size: 34px; }
      .hero-actions, .stack-actions, .toolbar, .section-head, .account-top, .bucket-top, .footer { align-items: stretch; }
      .accounts-grid, .summary-grid, .input-grid, .bucket-grid, .mini-grid { grid-template-columns: 1fr; }
      button, .button, .icon-button { width: 100%; justify-content: center; }
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
          <h1>Monitor every account in one adaptive, high-signal view.</h1>
          <p>Store multiple gateway accounts, let the layout rebalance automatically, and keep monthly credits, resets, free-trial windows, and bonus grants readable at a glance.</p>
          <div class='hero-actions'>
            <button type='button' id='heroAddBtn'>Add account</button>
            <button type='button' id='refreshAllBtn' class='secondary'>Refresh all</button>
            <button type='button' id='seedLocalBtn' class='ghost'>Use current gateway</button>
          </div>
        </div>
        <aside class='hero-side'>
          <h2>Kiro Usage Dashboard</h2>
          <p>Modernized for multi-account monitoring. Cards scale with account count, raw payloads collapse cleanly, and the page keeps each account isolated and easy to compare.</p>
          <div class='summary-grid'>
            <div class='summary'><div class='summary-label'>Tracked accounts</div><div class='summary-value' id='summaryAccounts'>0</div><div class='summary-note'>Saved locally in this browser profile.</div></div>
            <div class='summary'><div class='summary-label'>Remaining credits</div><div class='summary-value' id='summaryCredits'>-</div><div class='summary-note'>Combined remaining / total across all loaded accounts.</div></div>
            <div class='summary'><div class='summary-label'>Nearest reset</div><div class='summary-value' id='summaryReset'>-</div><div class='summary-note'>Earliest visible reset among loaded accounts.</div></div>
          </div>
        </aside>
      </div>
    </section>

    <section class='section'>
      <div class='panel'>
        <div class='section-head'>
          <div>
            <h2>Account roster</h2>
            <p class='section-copy'>Each card can point at this gateway or another deployment. Great for personal, staging, team, or region-specific accounts.</p>
          </div>
          <label class='toggle'><input id='autoRefreshToggle' type='checkbox'> Auto refresh every 5 minutes</label>
        </div>
        <div class='input-grid'>
          <div class='field'>
            <label for='accountName'>Account label</label>
            <input id='accountName' type='text' placeholder='Personal · KIRO PRO'>
          </div>
          <div class='field'>
            <label for='accountBaseUrl'>Gateway base URL</label>
            <input id='accountBaseUrl' type='url' placeholder='https://gateway.example.com'>
          </div>
          <div class='field'>
            <label for='accountApiKey'>Gateway API key</label>
            <input id='accountApiKey' type='password' placeholder='Paste PROXY_API_KEY here'>
          </div>
        </div>
        <div class='toolbar'>
          <div class='muted'>Keys are stored in <span class='mono'>localStorage</span> for this browser only.</div>
          <div class='stack-actions'>
            <button type='button' id='saveAccountBtn'>Save account</button>
            <button type='button' id='clearFormBtn' class='secondary'>Clear form</button>
          </div>
        </div>
      </div>

      <div id='statusLine' class='status'>Ready. Add an account, or use the current gateway as a quick starting point.</div>
      <div id='accountsGrid' class='accounts-grid'></div>
    </section>
  </div>
  <script>
    const STORAGE_KEY = 'kiro_usage_accounts_v2';
    const LEGACY_KEY = 'kiro_gateway_api_key';
    const AUTO_REFRESH_KEY = 'kiro_usage_auto_refresh';
    const AUTO_REFRESH_MS = 5 * 60 * 1000;
    const CURRENT_PAGE_PATH = window.location.pathname.replace(/\/+$/, '');
    const PATH_BASE = CURRENT_PAGE_PATH.endsWith('/usage') ? (CURRENT_PAGE_PATH.slice(0, -'/usage'.length) || '') : '';
    const CURRENT_GATEWAY_BASE = `${window.location.origin}${PATH_BASE}`;

    const accountNameInput = document.getElementById('accountName');
    const accountBaseUrlInput = document.getElementById('accountBaseUrl');
    const accountApiKeyInput = document.getElementById('accountApiKey');
    const saveAccountBtn = document.getElementById('saveAccountBtn');
    const clearFormBtn = document.getElementById('clearFormBtn');
    const heroAddBtn = document.getElementById('heroAddBtn');
    const seedLocalBtn = document.getElementById('seedLocalBtn');
    const refreshAllBtn = document.getElementById('refreshAllBtn');
    const autoRefreshToggle = document.getElementById('autoRefreshToggle');
    const statusLine = document.getElementById('statusLine');
    const accountsGrid = document.getElementById('accountsGrid');
    const summaryAccounts = document.getElementById('summaryAccounts');
    const summaryCredits = document.getElementById('summaryCredits');
    const summaryReset = document.getElementById('summaryReset');

    let accounts = [];
    let refreshTimer = null;

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

    function normalizeBaseUrl(rawUrl) {
      const input = (rawUrl || '').trim();
      if (!input) return CURRENT_GATEWAY_BASE;
      const url = new URL(input, CURRENT_GATEWAY_BASE);
      const pathname = url.pathname.replace(/\/+$/, '');
      return `${url.origin}${pathname}`;
    }

    function maskKey(key) {
      if (!key) return 'No key';
      if (key.length <= 8) return '••••';
      return `${key.slice(0, 4)}••••${key.slice(-4)}`;
    }

    function loadAccounts() {
      try {
        const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
        accounts = Array.isArray(parsed) ? parsed : [];
      } catch (error) {
        accounts = [];
      }
    }

    function saveAccounts() {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(accounts));
    }

    function updateStatus(message, tone = 'default') {
      statusLine.className = 'status';
      if (tone === 'ok') statusLine.classList.add('ok');
      if (tone === 'error') statusLine.classList.add('error');
      statusLine.textContent = message;
    }

    function renderSummary() {
      summaryAccounts.textContent = `${accounts.length}`;
      let remaining = 0;
      let total = 0;
      let nearestReset = null;
      accounts.forEach((account) => {
        const buckets = Array.isArray(account.data?.usage_breakdown_list) ? account.data.usage_breakdown_list : [];
        buckets.forEach((bucket) => {
          remaining += Number(bucket.remaining_usage || 0);
          total += Number(bucket.usage_limit || 0);
        });
        const reset = account.data?.next_date_reset ? new Date(account.data.next_date_reset) : null;
        if (reset && !Number.isNaN(reset.getTime()) && (!nearestReset || reset < nearestReset)) {
          nearestReset = reset;
        }
      });
      summaryCredits.textContent = total > 0 ? `${formatValue(remaining)} / ${formatValue(total)}` : '-';
      summaryReset.textContent = nearestReset ? nearestReset.toLocaleString() : '-';
    }

    function emptyMarkup() {
      return `<div class='empty'><strong>No accounts yet</strong><span>Add your first account above. The grid will automatically rebalance as you add more.</span></div>`;
    }

    function bucketMarkup(bucket) {
      const used = Number(bucket.current_usage || 0);
      const total = Number(bucket.usage_limit || 0);
      const remaining = Number(bucket.remaining_usage || Math.max(total - used, 0));
      const percent = total > 0 ? Math.min((used / total) * 100, 100) : 0;
      const bonus = Array.isArray(bucket.bonuses) && bucket.bonuses.length > 0
        ? bucket.bonuses.map((entry) => `<div class='mini'><div class='mini-label'>Bonus</div><div class='mini-value'>${formatValue(entry.current_usage)} / ${formatValue(entry.usage_limit)}</div><div class='summary-note'>Expires ${formatDate(entry.expiry)}</div></div>`).join('')
        : `<div class='mini'><div class='mini-label'>Bonus</div><div class='mini-value'>-</div><div class='summary-note'>No bonus credits reported.</div></div>`;
      return `
        <section class='bucket'>
          <div class='bucket-top'>
            <div><div class='bucket-title'>${escapeHtml(bucket.display_name_plural || bucket.display_name)}</div><div class='bucket-value'>${formatValue(remaining)} left</div></div>
            <div class='bucket-meta'>${formatValue(used)} used / ${formatValue(total)} total</div>
          </div>
          <div class='meter'><span style='width:${percent.toFixed(2)}%'></span></div>
          <div class='mini-grid'>
            <div class='mini'><div class='mini-label'>Reset</div><div class='mini-value'>${escapeHtml(formatDate(bucket.next_date_reset))}</div></div>
            <div class='mini'><div class='mini-label'>Free trial</div><div class='mini-value'>${bucket.free_trial_info ? `${formatValue(bucket.free_trial_info.current_usage)} / ${formatValue(bucket.free_trial_info.usage_limit)}` : '-'}</div></div>
            ${bonus}
          </div>
        </section>`;
    }

    function accountMarkup(account) {
      const data = account.data;
      const title = data?.subscription_info?.subscription_title || 'Not loaded yet';
      const user = data?.user_info?.email || data?.user_info?.user_id || 'Unknown user';
      const stateClass = account.error ? 'error' : data ? 'ok' : 'warn';
      const stateText = account.error ? 'Load failed' : data ? 'Connected' : 'Pending';
      const buckets = Array.isArray(data?.usage_breakdown_list) ? data.usage_breakdown_list : [];
      const details = data ? `<details class='details'><summary>Raw usage payload</summary><div class='details-body'><pre>${escapeHtml(JSON.stringify(data, null, 2))}</pre></div></details>` : '';
      return `
        <article class='account' data-account-id='${account.id}'>
          <div class='account-top'>
            <div class='account-title'>
              <h3>${escapeHtml(account.name)}</h3>
              <p>${escapeHtml(account.baseUrl)}</p>
              <div class='badges'>
                <span class='pill ${stateClass}'>${stateText}</span>
                <span class='pill'>${escapeHtml(title)}</span>
                <span class='pill'>${escapeHtml(maskKey(account.apiKey))}</span>
              </div>
            </div>
            <button type='button' class='icon-button remove-account' data-account-id='${account.id}' aria-label='Remove account'>✕</button>
          </div>
          <div class='mini-grid'>
            <div class='mini'><div class='mini-label'>User</div><div class='mini-value'>${escapeHtml(user)}</div></div>
            <div class='mini'><div class='mini-label'>Global reset</div><div class='mini-value'>${escapeHtml(formatDate(data?.next_date_reset))}</div></div>
            <div class='mini'><div class='mini-label'>Fetched</div><div class='mini-value'>${escapeHtml(formatDate(account.lastUpdatedAt || data?.fetched_at))}</div></div>
          </div>
          ${account.error ? `<div class='status error'>${escapeHtml(account.error)}</div>` : ''}
          ${buckets.length ? buckets.map(bucketMarkup).join('') : `<div class='empty'><strong>No usage buckets yet</strong><span>Save the account and refresh it to pull live usage data.</span></div>`}
          ${details}
          <div class='footer'>
            <span>${escapeHtml(account.note || 'Stored locally in this browser')}</span>
            <div class='stack-actions' style='margin-top:0'>
              <button type='button' class='secondary refresh-account' data-account-id='${account.id}'>Refresh</button>
              <button type='button' class='ghost edit-account' data-account-id='${account.id}'>Edit</button>
            </div>
          </div>
        </article>`;
    }

    function renderAccounts() {
      accountsGrid.innerHTML = accounts.length ? accounts.map(accountMarkup).join('') : emptyMarkup();
      renderSummary();
      bindAccountActions();
    }

    async function fetchAccountUsage(account) {
      const response = await fetch(`${account.baseUrl.replace(/\/$/, '')}/v1/usage?include_email=true`, {
        headers: { Authorization: `Bearer ${account.apiKey}` }
      });
      const text = await response.text();
      if (!response.ok) throw new Error(text || `HTTP ${response.status}`);
      return JSON.parse(text);
    }

    async function refreshAccount(accountId, silent = false) {
      const account = accounts.find((entry) => entry.id === accountId);
      if (!account) return;
      if (!silent) updateStatus(`Refreshing ${account.name}…`);
      try {
        account.data = await fetchAccountUsage(account);
        account.error = null;
        account.lastUpdatedAt = new Date().toISOString();
        saveAccounts();
        renderAccounts();
        if (!silent) updateStatus(`Loaded ${account.name}.`, 'ok');
      } catch (error) {
        account.data = null;
        account.error = error.message;
        account.lastUpdatedAt = new Date().toISOString();
        saveAccounts();
        renderAccounts();
        if (!silent) updateStatus(`Failed to load ${account.name}: ${error.message}`, 'error');
      }
    }

    async function refreshAll(silent = false) {
      if (!accounts.length) {
        updateStatus('Add at least one account before refreshing.', 'error');
        return;
      }
      if (!silent) updateStatus(`Refreshing ${accounts.length} account${accounts.length === 1 ? '' : 's'}…`);
      for (const account of accounts) {
        await refreshAccount(account.id, true);
      }
      const failed = accounts.filter((account) => account.error).length;
      if (failed) updateStatus(`Loaded with ${failed} account error${failed === 1 ? '' : 's'}.`, 'error');
      else if (!silent) updateStatus(`All ${accounts.length} account${accounts.length === 1 ? '' : 's'} refreshed.`, 'ok');
    }

    function resetForm() {
      accountNameInput.value = '';
      accountBaseUrlInput.value = CURRENT_GATEWAY_BASE;
      accountApiKeyInput.value = '';
      saveAccountBtn.dataset.editId = '';
      saveAccountBtn.textContent = 'Save account';
    }

    function upsertAccount(next) {
      const index = accounts.findIndex((account) => account.id === next.id);
      if (index >= 0) {
        const current = accounts[index];
        accounts[index] = { ...current, ...next, data: current.data, error: current.error, lastUpdatedAt: current.lastUpdatedAt };
      } else {
        accounts.unshift(next);
      }
      saveAccounts();
      renderAccounts();
    }

    function buildAccountFromForm() {
      const name = accountNameInput.value.trim();
      const apiKey = accountApiKeyInput.value.trim();
      const baseUrl = normalizeBaseUrl(accountBaseUrlInput.value);
      if (!name) throw new Error('Please add an account label.');
      if (!apiKey) throw new Error('Please provide the gateway API key.');
      return {
        id: saveAccountBtn.dataset.editId || `acct-${Date.now()}-${Math.random().toString(16).slice(2)}`,
        name,
        baseUrl,
        apiKey,
        note: baseUrl === CURRENT_GATEWAY_BASE ? 'Current gateway' : 'Remote gateway',
        data: null,
        error: null,
        lastUpdatedAt: null,
      };
    }

    function populateForm(account) {
      accountNameInput.value = account.name;
      accountBaseUrlInput.value = account.baseUrl;
      accountApiKeyInput.value = account.apiKey;
      saveAccountBtn.dataset.editId = account.id;
      saveAccountBtn.textContent = 'Update account';
      accountNameInput.focus();
    }

    function removeAccount(accountId) {
      accounts = accounts.filter((account) => account.id !== accountId);
      saveAccounts();
      renderAccounts();
      updateStatus('Account removed.', 'ok');
    }

    function bindAccountActions() {
      document.querySelectorAll('.remove-account').forEach((button) => button.addEventListener('click', () => removeAccount(button.dataset.accountId)));
      document.querySelectorAll('.refresh-account').forEach((button) => button.addEventListener('click', () => { void refreshAccount(button.dataset.accountId); }));
      document.querySelectorAll('.edit-account').forEach((button) => button.addEventListener('click', () => {
        const account = accounts.find((entry) => entry.id === button.dataset.accountId);
        if (account) populateForm(account);
      }));
    }

    function setAutoRefresh(enabled) {
      localStorage.setItem(AUTO_REFRESH_KEY, enabled ? '1' : '0');
      autoRefreshToggle.checked = enabled;
      if (refreshTimer) clearInterval(refreshTimer);
      refreshTimer = enabled ? setInterval(() => { void refreshAll(true); }, AUTO_REFRESH_MS) : null;
    }

    function seedCurrentGateway() {
      const key = localStorage.getItem(LEGACY_KEY) || accountApiKeyInput.value.trim();
      accountNameInput.value = accountNameInput.value || 'Current gateway';
      accountBaseUrlInput.value = CURRENT_GATEWAY_BASE;
      if (key) accountApiKeyInput.value = key;
      accountNameInput.focus();
      updateStatus('Current gateway values prefilled. Add or confirm the API key, then save.', 'ok');
    }

    saveAccountBtn.addEventListener('click', async () => {
      try {
        const account = buildAccountFromForm();
        upsertAccount(account);
        resetForm();
        updateStatus(`Saved ${account.name}. Fetching usage…`);
        await refreshAccount(account.id, true);
        updateStatus(`Saved and refreshed ${account.name}.`, 'ok');
      } catch (error) {
        updateStatus(error.message, 'error');
      }
    });
    clearFormBtn.addEventListener('click', () => { resetForm(); updateStatus('Form cleared.'); });
    heroAddBtn.addEventListener('click', () => { accountNameInput.focus(); updateStatus('Add another account below. The grid will resize automatically.'); });
    seedLocalBtn.addEventListener('click', seedCurrentGateway);
    refreshAllBtn.addEventListener('click', () => { void refreshAll(); });
    autoRefreshToggle.addEventListener('change', () => setAutoRefresh(autoRefreshToggle.checked));

    loadAccounts();
    setAutoRefresh(localStorage.getItem(AUTO_REFRESH_KEY) === '1');
    resetForm();
    renderAccounts();
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


@router.get('/usage', response_class=HTMLResponse)
async def usage_dashboard() -> HTMLResponse:
    """
    Render a polished multi-account browser dashboard for Kiro usage limits.

    Returns:
        Static HTML page that fetches usage data from one or more `/v1/usage`
        endpoints and adapts the layout to the number of accounts.
    """
    return HTMLResponse(content=USAGE_DASHBOARD_HTML)
