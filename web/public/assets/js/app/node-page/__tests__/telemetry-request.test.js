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

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  renderTelemetryRequestButton,
  bindTelemetryRequestButtons,
} from '../telemetry-request.js';
import { renderNodeDetailHtml } from '../detail-html.js';

const MESHCORE_NODE = { nodeId: '!abcd0001', protocol: 'meshcore' };

function fakeButton(nodeId) {
  const listeners = {};
  return {
    disabled: false,
    textContent: 'Request telemetry',
    getAttribute: name => (name === 'data-telemetry-request' ? nodeId : null),
    addEventListener: (type, fn) => { listeners[type] = fn; },
    dataset: {},
    async click() { await listeners.click?.({ preventDefault: () => {} }); },
  };
}

function fakeContainer(buttons) {
  return { querySelectorAll: sel => (sel === '[data-telemetry-request]' ? buttons : []) };
}

function fakeResponse(status, retryAfter = null) {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: name => (name === 'Retry-After' ? retryAfter : null) },
  };
}

test('renders only for enabled meshcore nodes with an id', () => {
  const html = renderTelemetryRequestButton(MESHCORE_NODE, { enabled: true });
  assert.ok(html.includes('data-telemetry-request="!abcd0001"'));
  assert.ok(html.includes('Request telemetry'));
  assert.equal(renderTelemetryRequestButton(MESHCORE_NODE, { enabled: false }), '');
  assert.equal(renderTelemetryRequestButton({ nodeId: '!a', protocol: 'meshtastic' }, { enabled: true }), '');
  assert.equal(renderTelemetryRequestButton({ protocol: 'meshcore' }, { enabled: true }), '');
});

test('escapes the node id attribute', () => {
  const html = renderTelemetryRequestButton({ nodeId: '!a"b', protocol: 'meshcore' }, { enabled: true });
  assert.ok(html.includes('data-telemetry-request="!a&quot;b"'));
});

test('click POSTs the node id and locks in the requested state on 202', async () => {
  const calls = [];
  const button = fakeButton('!abcd0001');
  const bound = bindTelemetryRequestButtons(fakeContainer([button]), {
    fetchImpl: async (url, options) => { calls.push([url, options]); return fakeResponse(202); },
  });
  assert.equal(bound, 1);
  await button.click();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], '/api/telemetry-requests');
  assert.equal(calls[0][1].method, 'POST');
  assert.deepEqual(JSON.parse(calls[0][1].body), { nodeId: '!abcd0001' });
  assert.equal(button.disabled, true);
  assert.ok(button.textContent.includes('requested'));
});

test('429 renders the retry hint from Retry-After and stays disabled', async () => {
  const button = fakeButton('!abcd0001');
  bindTelemetryRequestButtons(fakeContainer([button]), {
    fetchImpl: async () => fakeResponse(429, '540'),
  });
  await button.click();
  assert.equal(button.disabled, true);
  assert.ok(button.textContent.includes('9 min'));
});

test('failures re-enable the button for a retry', async () => {
  const button = fakeButton('!abcd0001');
  bindTelemetryRequestButtons(fakeContainer([button]), {
    fetchImpl: async () => { throw new Error('offline'); },
  });
  await button.click();
  assert.equal(button.disabled, false);
  assert.ok(button.textContent.includes('failed'));
});

test('non-202/429 statuses show failure and re-enable the button', async () => {
  const button = fakeButton('!abcd0001');
  bindTelemetryRequestButtons(fakeContainer([button]), {
    fetchImpl: async () => fakeResponse(500),
  });
  await button.click();
  assert.equal(button.disabled, false);
  assert.ok(button.textContent.includes('failed'));
});

test('bind tolerates a container without matches and a missing fetch', () => {
  assert.equal(bindTelemetryRequestButtons(null, {}), 0);
  assert.equal(bindTelemetryRequestButtons(fakeContainer([]), {}), 0);
});

const RENDER_OPTS = {
  renderShortHtml: value => String(value ?? ''),
  telemetryRequestsEnabled: true,
};

test('renderNodeDetailHtml embeds the button for meshcore nodes when enabled', () => {
  const html = renderNodeDetailHtml(MESHCORE_NODE, RENDER_OPTS);
  assert.ok(html.includes('data-telemetry-request="!abcd0001"'));
});

test('renderNodeDetailHtml omits the button when disabled or non-meshcore', () => {
  const disabled = renderNodeDetailHtml(MESHCORE_NODE, { ...RENDER_OPTS, telemetryRequestsEnabled: false });
  assert.ok(!disabled.includes('data-telemetry-request'));
  const meshtastic = renderNodeDetailHtml({ nodeId: '!ff00ff00', protocol: 'meshtastic' }, RENDER_OPTS);
  assert.ok(!meshtastic.includes('data-telemetry-request'));
});
