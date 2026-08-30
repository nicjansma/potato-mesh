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

# frozen_string_literal: true

require "spec_helper"
require "json"

RSpec.describe "Telemetry request storage" do
  let(:app) { Sinatra::Application }

  def with_db(readonly: false)
    db = PotatoMesh::Application.open_database(readonly: readonly)
    db.busy_timeout = PotatoMesh::Config.db_busy_timeout_ms
    yield db
  ensure
    db&.close
  end

  # Bare object exposing the DataProcessing mixin under test.
  let(:helpers) do
    Class.new do
      include PotatoMesh::App::DataProcessing
    end.new
  end

  let(:json_headers) { { "CONTENT_TYPE" => "application/json" } }

  def with_feature(enabled: "1", cooldown: nil, cap: nil)
    original = {
      "TELEMETRY_REQUESTS" => ENV["TELEMETRY_REQUESTS"],
      "TELEMETRY_REQUEST_COOLDOWN_SECONDS" => ENV["TELEMETRY_REQUEST_COOLDOWN_SECONDS"],
      "TELEMETRY_REQUEST_HOURLY_CAP" => ENV["TELEMETRY_REQUEST_HOURLY_CAP"],
    }
    ENV["TELEMETRY_REQUESTS"] = enabled
    ENV["TELEMETRY_REQUEST_COOLDOWN_SECONDS"] = cooldown
    ENV["TELEMETRY_REQUEST_HOURLY_CAP"] = cap
    yield
  ensure
    original.each { |k, v| v.nil? ? ENV.delete(k) : ENV[k] = v }
  end

  def seed_node(node_id, protocol)
    with_db do |db|
      db.execute(
        "INSERT OR REPLACE INTO nodes(node_id, last_heard, protocol) VALUES(?, ?, ?)",
        [node_id, Time.now.to_i, protocol],
      )
    end
  end

  before do
    with_db do |db|
      db.execute("DELETE FROM telemetry_requests")
      db.execute("DELETE FROM nodes WHERE node_id LIKE '!aaaa%' OR node_id LIKE '!bbbb%' OR node_id LIKE '!cccc%' OR node_id LIKE '!dddd%'")
    end
  end
  after do
    with_db do |db|
      db.execute("DELETE FROM telemetry_requests")
      db.execute("DELETE FROM nodes WHERE node_id LIKE '!aaaa%' OR node_id LIKE '!bbbb%' OR node_id LIKE '!cccc%' OR node_id LIKE '!dddd%'")
    end
  end

  describe "POST /api/telemetry-requests" do
    it "404s when the feature flag is off" do
      with_feature(enabled: "0") do
        post "/api/telemetry-requests", { nodeId: "!abcd0001" }.to_json, json_headers
        expect(last_response.status).to eq(404)
      end
    end

    it "404s when the hourly cap disables accepts" do
      with_feature(cap: "0") do
        post "/api/telemetry-requests", { nodeId: "!abcd0001" }.to_json, json_headers
        expect(last_response.status).to eq(404)
      end
    end

    it "rejects invalid JSON and payload shapes" do
      with_feature do
        post "/api/telemetry-requests", "{", json_headers
        expect(last_response.status).to eq(400)
        post "/api/telemetry-requests", [1].to_json, json_headers
        expect(last_response.status).to eq(400)
        post "/api/telemetry-requests", { nodeId: "%%%" }.to_json, json_headers
        expect(last_response.status).to eq(400)
      end
    end

    it "404s for unknown nodes and 422s for non-meshcore nodes" do
      with_feature do
        post "/api/telemetry-requests", { nodeId: "!00000000" }.to_json, json_headers
        expect(last_response.status).to eq(404)

        seed_node("!aaaa0001", "meshtastic")
        post "/api/telemetry-requests", { nodeId: "!aaaa0001" }.to_json, json_headers
        expect(last_response.status).to eq(422)
      end
    end

    it "accepts a meshcore node with 202, then 429s inside the cooldown" do
      with_feature do
        seed_node("!bbbb0001", "meshcore")
        post "/api/telemetry-requests", { nodeId: "!bbbb0001" }.to_json, json_headers
        expect(last_response.status).to eq(202)
        body = JSON.parse(last_response.body)
        expect(body["cooldownSeconds"]).to eq(900)

        post "/api/telemetry-requests", { nodeId: "!bbbb0001" }.to_json, json_headers
        expect(last_response.status).to eq(429)
        expect(last_response.headers["Retry-After"].to_i).to be_between(1, 900)
      end
    end

    it "429s when the global hourly cap is exhausted" do
      with_feature(cap: "1") do
        seed_node("!cccc0001", "meshcore")
        seed_node("!cccc0002", "meshcore")
        post "/api/telemetry-requests", { nodeId: "!cccc0001" }.to_json, json_headers
        expect(last_response.status).to eq(202)
        post "/api/telemetry-requests", { nodeId: "!cccc0002" }.to_json, json_headers
        expect(last_response.status).to eq(429)
        expect(last_response.headers["Retry-After"]).to eq("3600")
      end
    end

    it "accepts in private mode (telemetry is not privacy-gated)" do
      original = ENV["PRIVATE"]
      ENV["PRIVATE"] = "1"
      with_feature do
        seed_node("!dddd0001", "meshcore")
        post "/api/telemetry-requests", { nodeId: "!dddd0001" }.to_json, json_headers
        expect(last_response.status).to eq(202)
      end
    ensure
      original.nil? ? ENV.delete("PRIVATE") : ENV["PRIVATE"] = original
    end
  end

  it "inserts and reports the per-node cooldown remainder" do
    now = Time.now.to_i
    with_db do |db|
      helpers.insert_telemetry_request(db, "!abcd0001", now: now)
      expect(helpers.telemetry_request_cooldown_remaining(db, "!abcd0001", 900, now: now + 100)).to eq(800)
      expect(helpers.telemetry_request_cooldown_remaining(db, "!abcd0001", 900, now: now + 901)).to eq(0)
      expect(helpers.telemetry_request_cooldown_remaining(db, "!ffff0000", 900, now: now)).to eq(0)
    end
  end

  it "counts accepts inside the rolling hour only" do
    now = Time.now.to_i
    with_db do |db|
      helpers.insert_telemetry_request(db, "!abcd0001", now: now - 3_700)
      helpers.insert_telemetry_request(db, "!abcd0002", now: now - 60)
      expect(helpers.telemetry_requests_accepted_last_hour(db, now: now)).to eq(1)
    end
  end

  it "claims oldest-first, atomically, skipping claimed and expired rows" do
    now = Time.now.to_i
    with_db do |db|
      helpers.insert_telemetry_request(db, "!old00000", now: now - 700) # expired (> 600 s)
      helpers.insert_telemetry_request(db, "!abcd0001", now: now - 120)
      helpers.insert_telemetry_request(db, "!abcd0002", now: now - 60)

      first = helpers.claim_telemetry_request!(db, now: now)
      expect(first[1]).to eq("!abcd0001")
      second = helpers.claim_telemetry_request!(db, now: now)
      expect(second[1]).to eq("!abcd0002")
      expect(helpers.claim_telemetry_request!(db, now: now)).to be_nil

      claimed = db.get_first_value(
        "SELECT claimed_at FROM telemetry_requests WHERE id = ?", [first[0]]
      )
      expect(claimed).to eq(now)
    end
  end

  it "prunes rows older than the prune age during claim" do
    now = Time.now.to_i
    with_db do |db|
      helpers.insert_telemetry_request(db, "!ancient0", now: now - 700_000)
      helpers.claim_telemetry_request!(db, now: now)
      count = db.get_first_value("SELECT COUNT(*) FROM telemetry_requests").to_i
      expect(count).to eq(0)
    end
  end
end
