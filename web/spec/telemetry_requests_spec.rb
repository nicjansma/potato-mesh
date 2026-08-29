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

  before { with_db { |db| db.execute("DELETE FROM telemetry_requests") } }
  after { with_db { |db| db.execute("DELETE FROM telemetry_requests") } }

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
