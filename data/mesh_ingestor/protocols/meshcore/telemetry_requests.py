# Copyright © 2025-26 l5yth & contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""On-demand telemetry-request claim client for the MeshCore protocol.

SPEC decision **TI-A3** extends the existing periodic telemetry sweep with an
on-demand path: an operator clicks "Request telemetry" for a node in the web
dashboard, which enqueues a claim on the instance's
``POST /api/telemetry-requests/claim`` endpoint.  This module is the ingestor
side of that hand-off — it polls the claim endpoint across every configured
instance and resolves a claimed request's canonical node ID back to a
MeshCore roster contact so the caller (``telemetry.py``) can issue a targeted
``req_telemetry`` at the actual radio.

This module performs no transmission itself; it only reads (HTTP GET-like
POST claim, and the in-memory contact roster).  The **MA7** transmit gate
that governs whether ``req_telemetry`` may actually go out on the mesh is
enforced at the call site in ``telemetry.py``, immediately before the send,
per the "Adding a New Ingestor Protocol" contract in the repository's
``CLAUDE.md``.

Claim-poll error logging is split by actionability: network failures and
malformed responses (an instance being down, unreachable, or timing out) are
expected background noise for an ingestor that polls every instance every
:data:`_CLAIM_POLL_SECONDS`, so they log at ``severity="debug"`` (silent
unless ``DEBUG=1``).  A non-404 HTTP error (e.g. ``401``/``500``) means the
instance *is* reachable but rejected the request — typically a
misconfigured or expired ``API_TOKEN`` — which an operator can act on, so it
logs at ``severity="warning"`` (printed unconditionally).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from ... import config

_CLAIM_POLL_SECONDS = 30
"""Seconds between claim polls while the feature is answering (non-404)."""

_CLAIM_BACKOFF_SECONDS = 300
"""Claim-poll interval after every instance 404s (feature off server-side)."""

_CLAIM_TIMEOUT_SECS = 10
"""Socket timeout for one claim POST."""


def _claim_telemetry_request() -> tuple[dict | None, bool]:
    """Claim one pending telemetry request from the configured instances.

    Tries each ``config.INSTANCES`` pair in order; the first 200 wins.  A 204
    means the queue is empty; a 404 means the feature is disabled on that
    instance.  Network/decode failures log at debug severity, a non-404 HTTP
    error logs at warning severity (see the module docstring for why), and
    neither ever raises — a dead or misbehaving instance must not kill the
    poll loop.

    Returns:
        ``(payload, feature_seen)`` — the claimed request mapping (or
        ``None``) and whether the run should be treated as "the feature is
        confirmed off".  ``feature_seen`` is ``False`` *only* when every
        instance explicitly answered 404; an unreachable instance, a
        timeout, or any other error is "unknown", not "off", and still
        counts as ``True`` so callers poll again at
        :data:`_CLAIM_POLL_SECONDS` instead of backing off to
        :data:`_CLAIM_BACKOFF_SECONDS`.
    """
    saw_feature = False
    for instance, token in config.INSTANCES:
        url = f"{instance}/api/telemetry-requests/claim"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=_CLAIM_TIMEOUT_SECS) as resp:
                saw_feature = True
                if resp.status != 200:
                    continue
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                saw_feature = True
                config._debug_log(
                    "telemetry request claim failed",
                    context="meshcore.telemetry.request",
                    severity="warning",
                    url=url,
                    error=str(exc),
                )
            continue
        except Exception as exc:
            # Unreachable instance, timeout, or malformed response: the
            # instance's feature state is *unknown*, not confirmed off, so
            # this must not be mistaken for a 404 — see the Returns note
            # above and the module docstring's severity-split rationale.
            saw_feature = True
            config._debug_log(
                "telemetry request claim errored",
                context="meshcore.telemetry.request",
                severity="debug",
                url=url,
                error=str(exc),
            )
            continue
        if isinstance(payload, dict) and payload.get("nodeId"):
            return payload, True
    return None, saw_feature


def _find_roster_contact(iface, node_id: str) -> dict | None:
    """Return the roster contact whose canonical node id matches *node_id*.

    Parameters:
        iface: Active ``_MeshcoreInterface`` holding the contact snapshot.
        node_id: Canonical ``!xxxxxxxx`` id from a claimed request.

    Returns:
        The contact dict, or ``None`` when the node is not in the roster
        (the request is then dropped — ``req_telemetry`` needs a contact).
    """
    with iface._contacts_lock:
        contacts = list(iface._contacts.values())
    for contact in contacts:
        prefix = (contact.get("public_key") or "")[:12]
        if prefix and iface.lookup_node_id(prefix) == node_id:
            return contact
    return None
