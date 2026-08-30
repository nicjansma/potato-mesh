/*
 * Copyright © 2025-26 l5yth & contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/**
 * Fire-and-forget "Request telemetry" button for MeshCore nodes.
 *
 * Rendering and click wiring are separated so the pure renderer can be
 * embedded in the node-detail HTML string while binding happens after the
 * fragment lands in the DOM (both the node page and the overlay path).
 *
 * @module node-page/telemetry-request
 */

import { escapeHtml } from '../utils.js';
import { stringOrNull } from '../value-helpers.js';

/**
 * Render the request button for one node, or nothing when inapplicable.
 *
 * @param {Object} node Normalised node payload.
 * @param {{ enabled?: boolean }} [options] Feature flag from the app config.
 * @returns {string} HTML fragment, or `''` when the button must not appear.
 */
export function renderTelemetryRequestButton(node, { enabled = false } = {}) {
  if (!enabled) return '';
  if (stringOrNull(node?.protocol) !== 'meshcore') return '';
  const nodeId = stringOrNull(node?.nodeId ?? node?.node_id);
  if (!nodeId) return '';
  return `<button type="button" class="node-detail__telemetry-request" data-telemetry-request="${escapeHtml(nodeId)}">Request telemetry</button>`;
}

/**
 * Handle one button click: POST the request, reflect the outcome inline.
 *
 * The UX is fire-and-forget: 202 locks the button for this page view (the
 * server's cooldown is authoritative across reloads); 429 shows the
 * Retry-After hint; transport errors re-enable the button for a retry.
 *
 * @param {Object} button Button element carrying `data-telemetry-request`.
 * @param {Function} fetchFn Fetch implementation.
 * @returns {Promise<void>} Resolves when the button state is settled.
 */
async function handleTelemetryRequestClick(button, fetchFn) {
  const nodeId = button.getAttribute('data-telemetry-request');
  button.disabled = true;
  button.textContent = 'Requesting…';
  try {
    const response = await fetchFn('/api/telemetry-requests', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ nodeId }),
    });
    if (response.status === 202) {
      button.textContent = 'Telemetry requested ✓';
      return;
    }
    if (response.status === 429) {
      const retry = Number(response.headers?.get?.('Retry-After'));
      const minutes = Number.isFinite(retry) && retry > 0 ? Math.max(1, Math.ceil(retry / 60)) : null;
      button.textContent = minutes
        ? `Requested recently — try again in ~${minutes} min`
        : 'Requested recently';
      return;
    }
    button.textContent = 'Request failed';
    button.disabled = false;
  } catch (err) {
    console.error('Telemetry request failed', err);
    button.textContent = 'Request failed';
    button.disabled = false;
  }
}

/**
 * Attach click handlers to every request button inside `container`.
 *
 * @param {?ParentNode} container Rendered node-detail root.
 * @param {{ fetchImpl?: Function }} [options] Fetch override for tests.
 * @returns {number} Count of buttons bound.
 */
export function bindTelemetryRequestButtons(container, { fetchImpl } = {}) {
  if (!container || typeof container.querySelectorAll !== 'function') return 0;
  const fetchFn = typeof fetchImpl === 'function' ? fetchImpl : globalThis.fetch;
  if (typeof fetchFn !== 'function') return 0;
  const buttons = Array.from(container.querySelectorAll('[data-telemetry-request]'));
  for (const button of buttons) {
    button.addEventListener('click', event => {
      event?.preventDefault?.();
      return handleTelemetryRequestClick(button, fetchFn);
    });
  }
  return buttons.length;
}
