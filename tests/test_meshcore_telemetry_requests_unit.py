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

import io
import json
import urllib.error

import data.mesh_ingestor.protocols.meshcore.telemetry_requests as mc_req
from data.mesh_ingestor.protocols.meshcore.interface import _MeshcoreInterface

_KEY = "aabbccddeeff" + "00" * 26


def _fake_urlopen_factory(responses):
    """Return a urlopen stub yielding per-URL (status, body) or raising."""

    class _Resp:
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
        outcome = responses[req.full_url]
        if isinstance(outcome, Exception):
            raise outcome
        return _Resp(*outcome)

    return _fake


def test_claim_returns_payload_and_feature_seen(monkeypatch):
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


def test_claim_204_means_empty_queue_but_feature_on(monkeypatch):
    monkeypatch.setattr(mc_req.config, "_debug_log", lambda *a, **k: None)
    monkeypatch.setattr(mc_req.config, "INSTANCES", (("http://x", "tok"),))
    monkeypatch.setattr(
        mc_req.urllib.request,
        "urlopen",
        _fake_urlopen_factory({"http://x/api/telemetry-requests/claim": (204, b"")}),
    )
    assert mc_req._claim_telemetry_request() == (None, True)


def test_claim_404_everywhere_reports_feature_off(monkeypatch):
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
    iface = _MeshcoreInterface(target=None)
    iface._update_contact({"public_key": _KEY, "adv_name": "Sensor"})
    contact = mc_req._find_roster_contact(iface, "!aabbccdd")
    assert contact is not None and contact["public_key"] == _KEY
    assert mc_req._find_roster_contact(iface, "!00000000") is None
