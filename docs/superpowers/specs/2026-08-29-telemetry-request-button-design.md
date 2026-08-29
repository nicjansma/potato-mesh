<!--
  Copyright © 2025-26 l5yth & contributors
  Licensed under the Apache License, Version 2.0 — see LICENSE for details.
-->

# On-Demand MeshCore Telemetry Requests from the UI — Design

**Date:** 2026-08-29
**Status:** Approved (design), pending implementation plan

## Summary

Add a button to the web UI that lets a viewer request fresh telemetry
(`req_telemetry`) from a MeshCore node. The click is queued by the web app and
picked up by the MeshCore ingestor, which transmits the on-air pull under the
existing transmit policy. Results flow back through the unchanged telemetry
ingest pipeline. The feature is off by default and gated behind an operator
env flag.

## Requirements (settled in brainstorming)

1. **Hidden unless enabled** — the entire feature (routes + button) exists only
   when the operator sets `TELEMETRY_REQUESTS=1` on the web app. No viewer auth.
2. **Fire-and-forget UX** — the click acknowledges immediately; fresh data
   arrives via the existing SSE/live-update flow. No request-lifecycle tracking
   in the UI.
3. **Short per-node cooldown** — configurable via
   `TELEMETRY_REQUEST_COOLDOWN_SECONDS` (default 900), hard floor 300 s;
   values below the floor or unparseable are clamped up, fail-safe. Plus a
   global hourly accept cap (`TELEMETRY_REQUEST_HOURLY_CAP`, default 12,
   `<= 0` disables accepts). On-demand polls also stamp the background poll
   loop's 24 h per-node cooldown.
4. **All MeshCore nodes show the button** — the ingestor validates roster
   membership at claim time and drops non-contacts with a debug log.

## Invariant conformance

- **Apex (no MQTT/cloud):** web ⇄ ingestor stays pull-only HTTP between the
  existing components; no broker, no new connection types.
- **MA7 (default-off transmission):** every on-demand pull runs through
  `tx_policy.transmit_permitted()` at the transmit site beside
  `activity.record_tx()`. `TELEMETRY_REQUESTS=1` with `TX_ENABLED=0` accepts
  requests that expire unclaimed — no new transmit path is opened by default.
- **Privacy:** requests reference node ids already public on the instance;
  no new personal data is stored.

## Architecture

### New table: `telemetry_requests`

Base schema file `data/telemetry_requests.sql` plus a dated migration
(follow the `destinations` pattern). Columns:

| column         | type    | notes                          |
|----------------|---------|--------------------------------|
| `id`           | INTEGER | PK autoincrement               |
| `node_id`      | TEXT    | canonical node id              |
| `requested_at` | INTEGER | unix seconds                   |
| `claimed_at`   | INTEGER | NULL until claimed             |

Rows double as the rate-limit ledger; cooldown checks query recent rows per
node. Unclaimed rows older than 10 minutes are expired (filtered by the claim
query; stale rows removed opportunistically in the claim transaction).

### Web routes

- **`POST /api/telemetry-requests`** (viewer-facing, no token).
  Gates in order: feature flag (404 when off — route invisible), body shape
  (400), node exists (404), node protocol is `meshcore` (422), per-node
  cooldown and hourly cap (429 + `Retry-After`). Success: insert row, 202.
- **`POST /api/telemetry-requests/claim`** (ingestor-facing,
  `require_token!`). Atomic `UPDATE … WHERE claimed_at IS NULL … RETURNING`
  of the oldest unexpired pending row; 200 with the request, or 204 when none;
  404 when the feature is off. Atomic claim makes co-operating ingestors safe
  (first claimer wins).

### Ingestor (MeshCore provider)

The existing `_telemetry_poll_loop` gains a claim-poll: every ~30 s while
`tx_policy.transmit_permitted()`, call the claim endpoint (HTTP client per the
`announce.py` dogfeed pattern, run via thread offload from the async loop).
On a claimed request:

1. Resolve the roster contact by node-id prefix; on miss, drop + debug log.
2. Check `tx_policy.transmit_permitted()` at the transmit site, count with
   `activity.record_tx()`, send `req_telemetry_sync`; fall back to
   `req_status_sync` (its own gate + count), exactly as the background loop.
3. Stamp the background loop's 24 h per-node cooldown (`last_polled`).
4. Results flow through `_queue_meshcore_telemetry` → `POST /api/telemetry`
   → pubsub/SSE — pipeline unchanged.

Claim-poll errors are logged at debug and retried next cycle; a 404 (feature
off) backs the poll off to ~5 min.

### Frontend

- Feature flag delivered as one boolean in the existing `frontend_app_config`
  / `app_config_json` injection.
- Button rendered in the node detail overlay and node page only when the flag
  is on **and** `protocol === "meshcore"`.
- Click → POST → "Telemetry requested ✓" (disabled) for the cooldown; 429 →
  "requested recently — try again in ~N min" from `Retry-After`. No client
  persistence; no polling.

## Configuration reference (operator-facing README additions)

| env | default | effect |
|-----|---------|--------|
| `TELEMETRY_REQUESTS` | `0` | `1` shows the button and opens both routes |
| `TELEMETRY_REQUEST_COOLDOWN_SECONDS` | `900` | per-node cooldown; floor 300 |
| `TELEMETRY_REQUEST_HOURLY_CAP` | `12` | global accepts/hour; `<= 0` disables |

One README sentence: the button only transmits if the ingestor sets
`TX_ENABLED=1`.

## Testing

100 % unit coverage on every new unit (repo bar):

- **Ruby:** both routes — flag off/on, body validation, protocol gate,
  cooldown incl. floor clamping, hourly cap, `Retry-After`, atomic claim under
  contention, expiry filtering, private-mode interaction.
- **Python:** claim client (success/204/404 back-off/error tolerance),
  execution path (tx_policy gate honoured per site, roster miss, status
  fallback, 24 h stamp), loop integration.
- **JS:** button render gates (flag × protocol), click states, 429 handling.

## Documentation impact

- `CONTRACTS.md`: new section for both routes (shapes, gates, claim
  semantics).
- `SPEC.md`: one new numbered decision row recording this design; MA7
  unchanged (no default-on transmit path added).
- README: table above, task-oriented, three lines.
