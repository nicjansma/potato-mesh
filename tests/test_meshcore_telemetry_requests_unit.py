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

"""Unit tests for the MeshCore on-demand telemetry claim client."""

import asyncio
import io
import json
import time
import urllib.error

import data.mesh_ingestor.protocols.meshcore.telemetry_requests as mc_req
from data.mesh_ingestor.protocols.meshcore.interface import _MeshcoreInterface
from test_provider_unit import _telemetry_env, _TEST_CONTACT_KEY

_KEY = "aabbccddeeff" + "00" * 26


def _fake_urlopen_factory(responses, *, captured=None):
    """Return a urlopen stub yielding per-URL (status, body) or raising.

    Parameters:
        responses: Mapping of full request URL to either an
            ``(status, body)`` tuple or an ``Exception`` instance to raise.
        captured: Optional list; when given, every ``Request`` object passed
            to the stub is appended to it so callers can assert on method,
            body, and headers after the call.
    """

    class _Resp:
        """Minimal context-manager stand-in for ``http.client.HTTPResponse``."""

        def __init__(self, status, body):
            self.status = status
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _fake(req, timeout=None):
        if captured is not None:
            captured.append(req)
        outcome = responses[req.full_url]
        if isinstance(outcome, Exception):
            raise outcome
        return _Resp(*outcome)

    return _fake


def test_claim_returns_payload_and_feature_seen(monkeypatch):
    """A 200 response is decoded and reported as a successful, seen claim."""
    monkeypatch.setattr(mc_req.config, "_debug_log", lambda *a, **k: None)
    monkeypatch.setattr(mc_req.config, "INSTANCES", (("http://x", "tok"),))
    body = json.dumps({"id": 5, "nodeId": "!aabbccdd", "requestedAt": 1}).encode()
    monkeypatch.setattr(
        mc_req.urllib.request,
        "urlopen",
        _fake_urlopen_factory({"http://x/api/telemetry-requests/claim": (200, body)}),
    )
    claimed, seen = mc_req._claim_telemetry_request()
    assert claimed == {"id": 5, "nodeId": "!aabbccdd", "requestedAt": 1}
    assert seen is True


def test_claim_request_contract_method_body_and_auth_header(monkeypatch):
    """The claim POST uses the documented method, body, and headers.

    Verifies the outgoing request is a ``POST`` to the claim path with an
    empty JSON body, JSON ``Content-Type``/``Accept`` headers, and a
    ``Bearer`` ``Authorization`` header carrying the configured token.
    """
    monkeypatch.setattr(mc_req.config, "_debug_log", lambda *a, **k: None)
    monkeypatch.setattr(mc_req.config, "INSTANCES", (("http://x", "tok"),))
    body = json.dumps({"id": 5, "nodeId": "!aabbccdd", "requestedAt": 1}).encode()
    captured = []
    monkeypatch.setattr(
        mc_req.urllib.request,
        "urlopen",
        _fake_urlopen_factory(
            {"http://x/api/telemetry-requests/claim": (200, body)},
            captured=captured,
        ),
    )
    mc_req._claim_telemetry_request()

    assert len(captured) == 1
    req = captured[0]
    assert req.full_url == "http://x/api/telemetry-requests/claim"
    assert req.get_method() == "POST"
    assert req.data == b"{}"
    assert req.get_header("Content-type") == "application/json"
    assert req.get_header("Accept") == "application/json"
    assert req.get_header("Authorization") == "Bearer tok"


def test_claim_omits_auth_header_when_token_is_empty(monkeypatch):
    """No ``Authorization`` header is sent for an instance with an empty token."""
    monkeypatch.setattr(mc_req.config, "_debug_log", lambda *a, **k: None)
    monkeypatch.setattr(mc_req.config, "INSTANCES", (("http://x", ""),))
    body = json.dumps({"id": 5, "nodeId": "!aabbccdd", "requestedAt": 1}).encode()
    captured = []
    monkeypatch.setattr(
        mc_req.urllib.request,
        "urlopen",
        _fake_urlopen_factory(
            {"http://x/api/telemetry-requests/claim": (200, body)},
            captured=captured,
        ),
    )
    mc_req._claim_telemetry_request()

    assert len(captured) == 1
    assert captured[0].get_header("Authorization") is None


def test_claim_204_means_empty_queue_but_feature_on(monkeypatch):
    """A 204 (empty queue) still counts as the feature having been seen."""
    monkeypatch.setattr(mc_req.config, "_debug_log", lambda *a, **k: None)
    monkeypatch.setattr(mc_req.config, "INSTANCES", (("http://x", "tok"),))
    monkeypatch.setattr(
        mc_req.urllib.request,
        "urlopen",
        _fake_urlopen_factory({"http://x/api/telemetry-requests/claim": (204, b"")}),
    )
    assert mc_req._claim_telemetry_request() == (None, True)


def test_claim_404_everywhere_reports_feature_off(monkeypatch):
    """Every instance answering 404 is the only case reported as feature off."""
    monkeypatch.setattr(mc_req.config, "_debug_log", lambda *a, **k: None)
    monkeypatch.setattr(mc_req.config, "INSTANCES", (("http://x", "tok"),))
    err = urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b""))
    monkeypatch.setattr(
        mc_req.urllib.request,
        "urlopen",
        _fake_urlopen_factory({"http://x/api/telemetry-requests/claim": err}),
    )
    assert mc_req._claim_telemetry_request() == (None, False)


def test_claim_tolerates_network_errors_and_tries_next_instance(monkeypatch):
    """A network error on one instance falls through to the next instance."""
    monkeypatch.setattr(mc_req.config, "_debug_log", lambda *a, **k: None)
    monkeypatch.setattr(
        mc_req.config, "INSTANCES", (("http://down", ""), ("http://up", "tok"))
    )
    body = json.dumps({"id": 9, "nodeId": "!aabbccdd", "requestedAt": 2}).encode()
    monkeypatch.setattr(
        mc_req.urllib.request,
        "urlopen",
        _fake_urlopen_factory(
            {
                "http://down/api/telemetry-requests/claim": OSError("refused"),
                "http://up/api/telemetry-requests/claim": (200, body),
            }
        ),
    )
    claimed, seen = mc_req._claim_telemetry_request()
    assert claimed["id"] == 9 and seen is True


def test_claim_network_errors_everywhere_are_unknown_not_off(monkeypatch):
    """Every instance being unreachable is "unknown", not "confirmed off".

    Unlike an explicit 404, a network error means the instance's feature
    state was never actually observed, so the caller must keep polling at
    the normal cadence instead of backing off as if the feature were
    disabled server-side.
    """
    monkeypatch.setattr(mc_req.config, "_debug_log", lambda *a, **k: None)
    monkeypatch.setattr(
        mc_req.config, "INSTANCES", (("http://a", ""), ("http://b", "tok"))
    )
    monkeypatch.setattr(
        mc_req.urllib.request,
        "urlopen",
        _fake_urlopen_factory(
            {
                "http://a/api/telemetry-requests/claim": OSError("refused"),
                "http://b/api/telemetry-requests/claim": OSError("timed out"),
            }
        ),
    )
    assert mc_req._claim_telemetry_request() == (None, True)


def test_claim_non_404_http_error_is_logged_and_counts_as_feature_seen(monkeypatch):
    """A non-404 HTTP error (e.g. 500) must not be mistaken for "feature off":

    it is logged and treated as evidence the instance is answering.
    """
    monkeypatch.setattr(mc_req.config, "_debug_log", lambda *a, **k: None)
    monkeypatch.setattr(mc_req.config, "INSTANCES", (("http://x", "tok"),))
    err = urllib.error.HTTPError("u", 500, "boom", {}, io.BytesIO(b""))
    monkeypatch.setattr(
        mc_req.urllib.request,
        "urlopen",
        _fake_urlopen_factory({"http://x/api/telemetry-requests/claim": err}),
    )
    assert mc_req._claim_telemetry_request() == (None, True)


def test_find_roster_contact_matches_and_misses():
    """A roster contact is found by canonical node id, or ``None`` on a miss."""
    iface = _MeshcoreInterface(target=None)
    iface._update_contact({"public_key": _KEY, "adv_name": "Sensor"})
    contact = mc_req._find_roster_contact(iface, "!aabbccdd")
    assert contact is not None and contact["public_key"] == _KEY
    assert mc_req._find_roster_contact(iface, "!00000000") is None


def test_execute_claimed_request_pulls_and_stamps_cooldown(monkeypatch):
    """A claimed request resolves its contact, pulls telemetry, and stamps the cooldown."""
    mc_tel_env = _telemetry_env(
        monkeypatch, contacts=[{"public_key": _TEST_CONTACT_KEY, "adv_name": "Sensor"}]
    )
    mc_tel, iface, stub, captured = mc_tel_env
    node_id = iface.lookup_node_id(_TEST_CONTACT_KEY[:12])

    class _Cmds:
        async def req_telemetry_sync(self, _contact):
            return [{"type": "temperature", "value": 20.0}]

    class _MC:
        commands = _Cmds()

    state: dict = {}
    asyncio.run(
        mc_tel._execute_claimed_request(
            _MC(), iface, stub, state, {"id": 1, "nodeId": node_id}
        )
    )
    assert captured  # telemetry queued through the normal pipeline
    assert _TEST_CONTACT_KEY in state["last_polled"]  # 24 h stamp applied


def test_execute_claimed_request_drops_unknown_contacts(monkeypatch):
    """A claimed request for a node outside the roster is dropped without side effects."""
    mc_tel, iface, stub, captured = _telemetry_env(monkeypatch, contacts=[])
    state: dict = {}
    asyncio.run(
        mc_tel._execute_claimed_request(
            object(), iface, stub, state, {"id": 1, "nodeId": "!00000000"}
        )
    )
    assert captured == [] and state == {}


def test_claim_and_execute_returns_backoff_when_feature_off(monkeypatch):
    """The next claim delay backs off to the long interval once every instance 404s."""
    mc_tel, iface, stub, _captured = _telemetry_env(monkeypatch, contacts=[])
    monkeypatch.setattr(
        mc_tel.telemetry_requests,
        "_claim_telemetry_request",
        lambda: (None, False),
    )
    delay = asyncio.run(mc_tel._claim_and_execute(object(), iface, stub, {}))
    assert delay == mc_tel.telemetry_requests._CLAIM_BACKOFF_SECONDS


def test_claim_and_execute_returns_poll_interval_when_feature_on(monkeypatch):
    """The next claim delay stays at the regular interval while the feature answers."""
    mc_tel, iface, stub, _captured = _telemetry_env(monkeypatch, contacts=[])
    monkeypatch.setattr(
        mc_tel.telemetry_requests,
        "_claim_telemetry_request",
        lambda: (None, True),
    )
    delay = asyncio.run(mc_tel._claim_and_execute(object(), iface, stub, {}))
    assert delay == mc_tel.telemetry_requests._CLAIM_POLL_SECONDS
