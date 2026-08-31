<!--
  Copyright © 2025-26 l5yth & contributors
  Licensed under the Apache License, Version 2.0 — see LICENSE for details.
-->

# On-Demand MeshCore Telemetry Requests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A viewer-facing "Request telemetry" button on MeshCore node detail views that queues an on-air `req_telemetry` pull executed by the MeshCore ingestor, gated behind an operator env flag, a per-node cooldown, and the existing transmit policy.

**Architecture:** The web app stores clicks in a new `telemetry_requests` SQLite table behind a viewer `POST` (rate-limited) and a token-guarded atomic `claim` route. The MeshCore provider's existing telemetry poll loop additionally polls the claim route (~30 s) while transmission is permitted, executes the claimed pull through the same gated `req_telemetry_sync`/`req_status_sync` path as the background poll, and results flow through the unchanged telemetry ingest pipeline (POST /api/telemetry → SSE).

**Tech Stack:** Ruby/Sinatra + RSpec (web), SQLite, vanilla ES modules + node:test (frontend), Python asyncio + pytest (ingestor).

**Spec:** `docs/superpowers/specs/2026-08-29-telemetry-request-button-design.md`

## Global Constraints

- Every new **source file** (`.rb`, `.py`, `.js`) carries the full Apache v2 header block with the exact string `Copyright © 2025-26 l5yth & contributors` (copy the 13-line block from any existing sibling file). Every new **non-source file** (`.sql`, docs) carries the 2-line notice (SQL files use `--` comments; see `data/ingestors.sql`).
- Ruby files start with `# frozen_string_literal: true`; two-space indent; format with `cd web && bundle exec rufo .` before each Ruby commit.
- Python formatted with `black` (`black data/ tests/`) before each Python commit.
- 100% unit coverage on all new code; JSON API fields are camelCase; JS filenames kebab-case; full API docs (RDoc / JSDoc / PDoc) on every new method plus inline comments where logic isn't self-evident.
- Test commands: `cd web && bundle exec rspec`, `cd web && npm test`, `pytest -q tests/` (repo root, venv active).
- All feature transmissions must pass `tx_policy.transmit_permitted()` immediately before each send, beside `activity.record_tx()` (SPEC MA7).

---

### Task 1: Web config accessors

**Files:**
- Modify: `web/lib/potato_mesh/config.rb` (add 3 constants near the other `DEFAULT_*` constants, 3 accessors beside `live_updates_enabled?` at ~line 351)
- Test: `web/spec/config_spec.rb` (append a describe block; reuse the file's existing `within_env` helper)

**Interfaces:**
- Produces: `PotatoMesh::Config.telemetry_requests_enabled?` → Boolean; `PotatoMesh::Config.telemetry_request_cooldown_seconds` → Integer (≥ 300); `PotatoMesh::Config.telemetry_request_hourly_cap` → Integer (may be ≤ 0, meaning "accepts disabled").

- [ ] **Step 1: Write the failing tests** (append inside the top-level describe of `config_spec.rb`, before the `within_env` def):

```ruby
  describe ".telemetry_requests_enabled?" do
    it "defaults to disabled" do
      within_env("TELEMETRY_REQUESTS" => nil) do
        expect(described_class.telemetry_requests_enabled?).to be(false)
      end
    end

    it "enables only on an exact 1" do
      within_env("TELEMETRY_REQUESTS" => "1") do
        expect(described_class.telemetry_requests_enabled?).to be(true)
      end
      within_env("TELEMETRY_REQUESTS" => "true") do
        expect(described_class.telemetry_requests_enabled?).to be(false)
      end
    end
  end

  describe ".telemetry_request_cooldown_seconds" do
    it "defaults to 900" do
      within_env("TELEMETRY_REQUEST_COOLDOWN_SECONDS" => nil) do
        expect(described_class.telemetry_request_cooldown_seconds).to eq(900)
      end
    end

    it "honours overrides above the floor" do
      within_env("TELEMETRY_REQUEST_COOLDOWN_SECONDS" => "600") do
        expect(described_class.telemetry_request_cooldown_seconds).to eq(600)
      end
    end

    it "clamps values below the 300 second floor up to the floor" do
      within_env("TELEMETRY_REQUEST_COOLDOWN_SECONDS" => "60") do
        expect(described_class.telemetry_request_cooldown_seconds).to eq(300)
      end
    end

    it "falls back to the default on junk" do
      within_env("TELEMETRY_REQUEST_COOLDOWN_SECONDS" => "soon") do
        expect(described_class.telemetry_request_cooldown_seconds).to eq(900)
      end
    end
  end

  describe ".telemetry_request_hourly_cap" do
    it "defaults to 12" do
      within_env("TELEMETRY_REQUEST_HOURLY_CAP" => nil) do
        expect(described_class.telemetry_request_hourly_cap).to eq(12)
      end
    end

    it "passes non-positive values through so the route can disable accepts" do
      within_env("TELEMETRY_REQUEST_HOURLY_CAP" => "0") do
        expect(described_class.telemetry_request_hourly_cap).to eq(0)
      end
    end

    it "falls back to the default on junk" do
      within_env("TELEMETRY_REQUEST_HOURLY_CAP" => "lots") do
        expect(described_class.telemetry_request_hourly_cap).to eq(12)
      end
    end
  end
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && bundle exec rspec spec/config_spec.rb -e "telemetry_request"`
Expected: FAIL with `NoMethodError` (undefined method `telemetry_requests_enabled?`)

- [ ] **Step 3: Implement.** In `config.rb`, next to the other defaults add:

```ruby
    # Default per-node cooldown between accepted on-demand telemetry requests.
    DEFAULT_TELEMETRY_REQUEST_COOLDOWN_SECONDS = 900

    # Hard floor for the per-node cooldown; lower operator values clamp up.
    MIN_TELEMETRY_REQUEST_COOLDOWN_SECONDS = 300

    # Default global cap on accepted telemetry requests per hour.
    DEFAULT_TELEMETRY_REQUEST_HOURLY_CAP = 12
```

Beside `live_updates_enabled?` add (RDoc on each, matching neighbours):

```ruby
    # Determine whether viewer-facing on-demand telemetry requests are offered.
    #
    # Off by default; enabled only by +TELEMETRY_REQUESTS=1+. When off, both
    # telemetry-request routes 404 and the UI button is never rendered.
    #
    # @return [Boolean] true when TELEMETRY_REQUESTS=1 in the environment.
    def telemetry_requests_enabled?
      ENV.fetch("TELEMETRY_REQUESTS", "0").to_s.strip == "1"
    end

    # Per-node cooldown (seconds) between accepted telemetry requests.
    #
    # Overridable via +TELEMETRY_REQUEST_COOLDOWN_SECONDS+ but clamped to a
    # 300 second floor: every accepted request is real LoRa airtime, so the
    # knob fails safe upward rather than rejecting a low value.
    #
    # @return [Integer] cooldown seconds, never below the floor.
    def telemetry_request_cooldown_seconds
      value = fetch_positive_integer(
        "TELEMETRY_REQUEST_COOLDOWN_SECONDS",
        DEFAULT_TELEMETRY_REQUEST_COOLDOWN_SECONDS,
      )
      value < MIN_TELEMETRY_REQUEST_COOLDOWN_SECONDS ? MIN_TELEMETRY_REQUEST_COOLDOWN_SECONDS : value
    end

    # Global cap on accepted telemetry requests per rolling hour.
    #
    # Non-positive values are passed through unchanged: the POST route treats
    # +<= 0+ as "accepts disabled" (belt-and-braces beside the feature flag).
    #
    # @return [Integer] configured cap, or the default on junk input.
    def telemetry_request_hourly_cap
      raw = ENV["TELEMETRY_REQUEST_HOURLY_CAP"]
      return DEFAULT_TELEMETRY_REQUEST_HOURLY_CAP if raw.nil? || raw.strip.empty?

      Integer(raw.strip, 10)
    rescue ArgumentError
      DEFAULT_TELEMETRY_REQUEST_HOURLY_CAP
    end
```

- [ ] **Step 4: Run to verify pass**

Run: `cd web && bundle exec rspec spec/config_spec.rb`
Expected: PASS (all examples)

- [ ] **Step 5: Format and commit**

```bash
cd web && bundle exec rufo lib/potato_mesh/config.rb spec/config_spec.rb
git add web/lib/potato_mesh/config.rb web/spec/config_spec.rb
git commit -m "Add telemetry-request config accessors (flag, cooldown floor, hourly cap)"
```

---

### Task 2: `telemetry_requests` table and DB helpers

**Files:**
- Create: `data/telemetry_requests.sql`
- Create: `web/lib/potato_mesh/application/data_processing/telemetry_requests.rb`
- Modify: `web/lib/potato_mesh/application/database.rb` (init_db `%w[...]` list at ~line 113; append an upgrade block at the end of `ensure_schema_upgrades` mirroring the `destinations` block at ~line 593)
- Modify: `web/lib/potato_mesh/application/data_processing.rb` (add `require_relative "data_processing/telemetry_requests"` after the `telemetry` require)
- Test: `web/spec/telemetry_requests_spec.rb` (new file — helper portion)

**Interfaces:**
- Consumes: `with_busy_retry` (existing DataProcessing helper), `open_database`.
- Produces (all DataProcessing instance methods): `insert_telemetry_request(db, node_id, now:)` → true; `telemetry_request_cooldown_remaining(db, node_id, cooldown_seconds, now:)` → Integer seconds (0 when clear); `telemetry_requests_accepted_last_hour(db, now:)` → Integer; `claim_telemetry_request!(db, now:)` → `[id, node_id, requested_at]` Array or nil. Constants `TELEMETRY_REQUEST_CLAIM_WINDOW_SECONDS = 600`, `TELEMETRY_REQUEST_PRUNE_AGE_SECONDS = 604_800`.

- [ ] **Step 1: Write the schema file** `data/telemetry_requests.sql` (2-line notice in `--` comments, then):

```sql
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS telemetry_requests (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id      TEXT NOT NULL,
  requested_at INTEGER NOT NULL,
  claimed_at   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_telemetry_requests_node_time
  ON telemetry_requests(node_id, requested_at);
```

- [ ] **Step 2: Write the failing helper specs** — new `web/spec/telemetry_requests_spec.rb` (full Apache header, `frozen_string_literal`), copying the `with_db` helper idiom from `spec/ingestors_spec.rb`:

```ruby
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
```

- [ ] **Step 3: Run to verify failure**

Run: `cd web && bundle exec rspec spec/telemetry_requests_spec.rb`
Expected: FAIL (`no such table: telemetry_requests` or `NoMethodError`)

- [ ] **Step 4: Implement.**
  1. `database.rb`: change the init list to `%w[nodes messages positions telemetry neighbors instances traces ingestors ingestor_activity waypoints telemetry_requests]`; at the end of `ensure_schema_upgrades` (after the destinations block, before the `rescue`) add:

```ruby
        telemetry_request_tables =
          db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='telemetry_requests'").flatten
        if telemetry_request_tables.empty?
          telemetry_requests_schema = File.expand_path("../../../../data/telemetry_requests.sql", __dir__)
          db.execute_batch(File.read(telemetry_requests_schema))
        end
```

  2. New helper module `data_processing/telemetry_requests.rb` (full header, RDoc per method):

```ruby
module PotatoMesh
  module App
    # On-demand telemetry request queue helpers (viewer POST -> ingestor claim).
    module DataProcessing
      # Age past which an unclaimed request is no longer claimable.
      TELEMETRY_REQUEST_CLAIM_WINDOW_SECONDS = 600

      # Age past which rows are deleted outright (claim-time prune).
      TELEMETRY_REQUEST_PRUNE_AGE_SECONDS = 7 * 24 * 60 * 60

      # Insert one accepted telemetry request.
      #
      # @param db [SQLite3::Database] open writable handle.
      # @param node_id [String] canonical node id.
      # @param now [Integer] unix seconds (injectable for tests).
      # @return [Boolean] true.
      def insert_telemetry_request(db, node_id, now: Time.now.to_i)
        with_busy_retry do
          db.execute(
            "INSERT INTO telemetry_requests(node_id, requested_at) VALUES(?, ?)",
            [node_id, now],
          )
        end
        true
      end

      # Seconds until the node may be requested again; 0 when clear.
      #
      # Counts from the newest request regardless of claim state so a failed
      # or expired request still honours the cooldown (airtime was intended).
      #
      # @param db [SQLite3::Database] open handle.
      # @param node_id [String] canonical node id.
      # @param cooldown_seconds [Integer] configured per-node cooldown.
      # @param now [Integer] unix seconds.
      # @return [Integer] non-negative remainder.
      def telemetry_request_cooldown_remaining(db, node_id, cooldown_seconds, now: Time.now.to_i)
        newest = db.get_first_value(
          "SELECT MAX(requested_at) FROM telemetry_requests WHERE node_id = ?",
          [node_id],
        )
        return 0 unless newest

        remaining = newest.to_i + cooldown_seconds - now
        remaining.positive? ? remaining : 0
      end

      # Count accepted requests inside the rolling hour (global cap input).
      #
      # @param db [SQLite3::Database] open handle.
      # @param now [Integer] unix seconds.
      # @return [Integer] accepted-request count.
      def telemetry_requests_accepted_last_hour(db, now: Time.now.to_i)
        db.get_first_value(
          "SELECT COUNT(*) FROM telemetry_requests WHERE requested_at > ?",
          [now - 3600],
        ).to_i
      end

      # Atomically claim the oldest unexpired pending request.
      #
      # The single UPDATE both selects and stamps the row, so co-operating
      # ingestors can never double-claim (first writer wins). Rows past
      # {TELEMETRY_REQUEST_PRUNE_AGE_SECONDS} are deleted opportunistically —
      # the table needs no retention sweeper.
      #
      # @param db [SQLite3::Database] open writable handle.
      # @param now [Integer] unix seconds.
      # @return [Array(Integer, String, Integer), nil] +[id, node_id, requested_at]+ or nil.
      def claim_telemetry_request!(db, now: Time.now.to_i)
        row = nil
        with_busy_retry do
          db.execute(
            "DELETE FROM telemetry_requests WHERE requested_at < ?",
            [now - TELEMETRY_REQUEST_PRUNE_AGE_SECONDS],
          )
          row = db.get_first_row(<<~SQL, [now, now - TELEMETRY_REQUEST_CLAIM_WINDOW_SECONDS])
            UPDATE telemetry_requests
               SET claimed_at = ?
             WHERE id = (SELECT id FROM telemetry_requests
                          WHERE claimed_at IS NULL AND requested_at >= ?
                          ORDER BY requested_at ASC, id ASC
                          LIMIT 1)
            RETURNING id, node_id, requested_at
          SQL
        end
        row
      end
    end
  end
end
```

  3. `data_processing.rb`: add `require_relative "data_processing/telemetry_requests"` directly after the `data_processing/telemetry` require.

- [ ] **Step 5: Run to verify pass**

Run: `cd web && bundle exec rspec spec/telemetry_requests_spec.rb spec/database_spec.rb`
Expected: PASS

- [ ] **Step 6: Format and commit**

```bash
cd web && bundle exec rufo .
git add data/telemetry_requests.sql web/lib/potato_mesh/application/database.rb \
  web/lib/potato_mesh/application/data_processing.rb \
  web/lib/potato_mesh/application/data_processing/telemetry_requests.rb \
  web/spec/telemetry_requests_spec.rb
git commit -m "Add telemetry_requests table with insert/cooldown/claim helpers"
```

---

### Task 3: Viewer route `POST /api/telemetry-requests`

**Files:**
- Modify: `web/lib/potato_mesh/application/routes/api.rb` (append inside `self.registered(app)`)
- Test: `web/spec/telemetry_requests_spec.rb` (append a describe block)

**Interfaces:**
- Consumes: Task 1 config accessors; Task 2 helpers; existing `read_json_body`, `canonical_node_parts`, `open_database`.
- Produces: HTTP contract — 404 flag-off/unknown node, 400 bad JSON/payload/node id, 422 non-meshcore, 429 + `Retry-After` on cooldown/cap, 202 `{"status":"ok","cooldownSeconds":N}` on accept.

- [ ] **Step 1: Write the failing route specs** (append to `spec/telemetry_requests_spec.rb`; add `json_headers` let and an env helper at the top of the file):

```ruby
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
```

Also extend both table-cleanup hooks to clear seeded nodes: `db.execute("DELETE FROM nodes WHERE node_id LIKE '!aaaa%' OR node_id LIKE '!bbbb%' OR node_id LIKE '!cccc%' OR node_id LIKE '!dddd%'")`.

- [ ] **Step 2: Run to verify failure**

Run: `cd web && bundle exec rspec spec/telemetry_requests_spec.rb`
Expected: FAIL (404s from Sinatra's not-found handler for every example — no route yet; the flag-off example may false-pass, that's fine)

- [ ] **Step 3: Implement** — append to `routes/api.rb` inside `self.registered(app)` (before the closing `end`), with a YARD comment mirroring neighbours:

```ruby
          app.post "/api/telemetry-requests" do
            content_type :json
            # Feature-flag gate first: an unflagged instance shows no evidence
            # the route exists (404, same body as an unknown path).
            halt 404, { error: "not found" }.to_json unless PotatoMesh::Config.telemetry_requests_enabled?
            cap = PotatoMesh::Config.telemetry_request_hourly_cap
            halt 404, { error: "not found" }.to_json if cap <= 0
            begin
              data = JSON.parse(read_json_body)
            rescue JSON::ParserError
              halt 400, { error: "invalid JSON" }.to_json
            end
            halt 400, { error: "invalid payload" }.to_json unless data.is_a?(Hash)
            parts = canonical_node_parts(data["nodeId"] || data["node_id"])
            halt 400, { error: "invalid node id" }.to_json unless parts
            node_id, = parts
            db = open_database
            protocol = db.get_first_value("SELECT protocol FROM nodes WHERE node_id = ?", [node_id])
            halt 404, { error: "unknown node" }.to_json unless protocol
            halt 422, { error: "unsupported protocol" }.to_json unless protocol == "meshcore"
            cooldown = PotatoMesh::Config.telemetry_request_cooldown_seconds
            remaining = telemetry_request_cooldown_remaining(db, node_id, cooldown)
            if remaining.positive?
              response.headers["Retry-After"] = remaining.to_s
              halt 429, { error: "cooldown", retryAfterSeconds: remaining }.to_json
            end
            if telemetry_requests_accepted_last_hour(db) >= cap
              response.headers["Retry-After"] = "3600"
              halt 429, { error: "rate limited", retryAfterSeconds: 3600 }.to_json
            end
            insert_telemetry_request(db, node_id)
            status 202
            { status: "ok", cooldownSeconds: cooldown }.to_json
          ensure
            db&.close
          end
```

- [ ] **Step 4: Run to verify pass**

Run: `cd web && bundle exec rspec spec/telemetry_requests_spec.rb`
Expected: PASS

- [ ] **Step 5: Format and commit**

```bash
cd web && bundle exec rufo .
git add web/lib/potato_mesh/application/routes/api.rb web/spec/telemetry_requests_spec.rb
git commit -m "Add viewer POST /api/telemetry-requests with cooldown and hourly cap"
```

---

### Task 4: Claim route `POST /api/telemetry-requests/claim`

**Files:**
- Modify: `web/lib/potato_mesh/application/routes/ingest.rb` (append inside `self.registered(app)`)
- Test: `web/spec/telemetry_requests_spec.rb` (append)

**Interfaces:**
- Consumes: `require_token!`, `claim_telemetry_request!` (Task 2), flag accessor (Task 1).
- Produces: HTTP contract — 403 without token, 404 flag off, 204 empty queue, 200 `{"id":N,"nodeId":"!…","requestedAt":N}`.

- [ ] **Step 1: Write the failing specs** (append; reuse `auth_headers` pattern from `ingestors_spec.rb` — add `let(:api_token) { "secret-token" }`, `let(:auth_headers)`, and the API_TOKEN before/after env swap to this spec file's top-level `before`/`after` exactly as `ingestors_spec.rb` lines 31–40 do):

```ruby
  describe "POST /api/telemetry-requests/claim" do
    it "requires the ingest token" do
      with_feature do
        post "/api/telemetry-requests/claim", "{}", json_headers
        expect(last_response.status).to eq(403)
      end
    end

    it "404s when the feature flag is off" do
      with_feature(enabled: "0") do
        post "/api/telemetry-requests/claim", "{}", auth_headers
        expect(last_response.status).to eq(404)
      end
    end

    it "204s when nothing is pending" do
      with_feature do
        post "/api/telemetry-requests/claim", "{}", auth_headers
        expect(last_response.status).to eq(204)
      end
    end

    it "claims the oldest pending request exactly once" do
      with_feature do
        now = Time.now.to_i
        with_db { |db| helpers.insert_telemetry_request(db, "!bbbb0001", now: now - 30) }
        post "/api/telemetry-requests/claim", "{}", auth_headers
        expect(last_response.status).to eq(200)
        body = JSON.parse(last_response.body)
        expect(body["nodeId"]).to eq("!bbbb0001")
        expect(body["requestedAt"]).to eq(now - 30)

        post "/api/telemetry-requests/claim", "{}", auth_headers
        expect(last_response.status).to eq(204)
      end
    end
  end
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && bundle exec rspec spec/telemetry_requests_spec.rb`
Expected: FAIL (404 where 403/204/200 expected)

- [ ] **Step 3: Implement** — append to `routes/ingest.rb` inside `self.registered(app)`:

```ruby
          app.post "/api/telemetry-requests/claim" do
            require_token!
            content_type :json
            halt 404, { error: "not found" }.to_json unless PotatoMesh::Config.telemetry_requests_enabled?
            db = open_database
            row = claim_telemetry_request!(db)
            if row.nil?
              # No pending work: 204 keeps the ingestor's poll loop cheap.
              halt 204
            end
            { id: row[0], nodeId: row[1], requestedAt: row[2] }.to_json
          ensure
            db&.close
          end
```

- [ ] **Step 4: Run to verify pass**

Run: `cd web && bundle exec rspec spec/telemetry_requests_spec.rb`
Expected: PASS

- [ ] **Step 5: Format and commit**

```bash
cd web && bundle exec rufo .
git add web/lib/potato_mesh/application/routes/ingest.rb web/spec/telemetry_requests_spec.rb
git commit -m "Add token-guarded atomic claim route for telemetry requests"
```

---

### Task 5: Expose the flag to the frontend

**Files:**
- Modify: `web/lib/potato_mesh/application/helpers/config_helpers.rb:47-67` (`frontend_app_config`)
- Test: `web/spec/telemetry_requests_spec.rb` (append)

**Interfaces:**
- Produces: `telemetryRequestsEnabled` Boolean key in the `data-app-config` JSON every layout page carries.

- [ ] **Step 1: Write the failing specs** (append):

```ruby
  describe "frontend app config flag" do
    it "serialises telemetryRequestsEnabled into data-app-config" do
      with_feature do
        get "/"
        expect(last_response.status).to eq(200)
        expect(last_response.body).to include(
          Rack::Utils.escape_html('"telemetryRequestsEnabled":true'),
        )
      end
      with_feature(enabled: "0") do
        get "/"
        expect(last_response.body).to include(
          Rack::Utils.escape_html('"telemetryRequestsEnabled":false'),
        )
      end
    end
  end
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && bundle exec rspec spec/telemetry_requests_spec.rb -e "frontend app config"`
Expected: FAIL (key absent from body)

- [ ] **Step 3: Implement** — in `frontend_app_config`, after `instancesFeatureEnabled:` add:

```ruby
          telemetryRequestsEnabled: PotatoMesh::Config.telemetry_requests_enabled?,
```

- [ ] **Step 4: Run to verify pass**

Run: `cd web && bundle exec rspec spec/telemetry_requests_spec.rb`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd web && bundle exec rufo .
git add web/lib/potato_mesh/application/helpers/config_helpers.rb web/spec/telemetry_requests_spec.rb
git commit -m "Expose telemetryRequestsEnabled in the frontend app config"
```

---

### Task 6: JS telemetry-request module (render + bind)

**Files:**
- Create: `web/public/assets/js/app/node-page/telemetry-request.js`
- Test: `web/public/assets/js/app/node-page/__tests__/telemetry-request.test.js`

**Interfaces:**
- Consumes: `escapeHtml` from `../utils.js`, `stringOrNull` from `../value-helpers.js`.
- Produces: `renderTelemetryRequestButton(node, { enabled }) → string` (empty string unless enabled && meshcore && has id); `bindTelemetryRequestButtons(container, { fetchImpl }) → number` (buttons bound). Button markup: `<button type="button" class="node-detail__telemetry-request" data-telemetry-request="<nodeId>">Request telemetry</button>`.

- [ ] **Step 1: Write the failing tests** (full Apache header; node:test idiom as in `__tests__/destinations.test.js`):

```js
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  renderTelemetryRequestButton,
  bindTelemetryRequestButtons,
} from '../telemetry-request.js';

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

test('bind tolerates a container without matches and a missing fetch', () => {
  assert.equal(bindTelemetryRequestButtons(null, {}), 0);
  assert.equal(bindTelemetryRequestButtons(fakeContainer([]), {}), 0);
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && node --test public/assets/js/app/node-page/__tests__/telemetry-request.test.js`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement** `telemetry-request.js` (full header + JSDoc):

```js
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
```

Note the click listener returns the handler promise so tests can `await button.click()`.

- [ ] **Step 4: Run to verify pass**

Run: `cd web && node --test public/assets/js/app/node-page/__tests__/telemetry-request.test.js`
Expected: PASS (all 6 tests)

- [ ] **Step 5: Commit**

```bash
git add web/public/assets/js/app/node-page/telemetry-request.js \
  web/public/assets/js/app/node-page/__tests__/telemetry-request.test.js
git commit -m "Add telemetry-request button renderer and click binding module"
```

---

### Task 7: Wire the button through detail HTML, node page, overlay, and CSS

**Files:**
- Modify: `web/public/assets/js/app/node-page/detail-html.js` (option + header markup)
- Modify: `web/public/assets/js/app/node-page/bootstrap.js` (thread option; bind after render)
- Modify: `web/public/assets/js/app/node-detail-overlay.js` (option pass-through; bind after render)
- Modify: `web/public/assets/js/app/main.js` (pass flag when creating the overlay manager)
- Modify: `web/public/assets/styles/base.css` (button styles after the `.node-detail__badge` rule at ~line 1356)
- Test: `web/public/assets/js/app/node-page/__tests__/telemetry-request.test.js` (append detail-html integration tests)

**Interfaces:**
- Consumes: Task 6 module; `readAppConfig()` from `app/config.js`.
- Produces: `renderNodeDetailHtml(node, { …, telemetryRequestsEnabled })`; `fetchNodeDetailHtml(referenceData, { …, telemetryRequestsEnabled })`; `createNodeDetailOverlayManager({ …, telemetryRequestsEnabled })`.

- [ ] **Step 1: Write the failing integration tests** (append to `telemetry-request.test.js`):

```js
import { renderNodeDetailHtml } from '../detail-html.js';

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
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && node --test public/assets/js/app/node-page/__tests__/telemetry-request.test.js`
Expected: FAIL (button markup absent)

- [ ] **Step 3: Implement the wiring.**
  1. **detail-html.js**: add `import { renderTelemetryRequestButton } from './telemetry-request.js';`; add `telemetryRequestsEnabled = false,` to the destructured options (and to the JSDoc options typedef); before the `return` add `const telemetryButtonHtml = renderTelemetryRequestButton(node, { enabled: telemetryRequestsEnabled });`; change the header line of the returned template to:

```js
    <header class="node-detail__header">
      <h2 class="node-detail__title">${badgeHtml}${nameHtml}${identifierHtml}</h2>${telemetryButtonHtml}
    </header>
```

  2. **bootstrap.js**: add imports `import { bindTelemetryRequestButtons } from './telemetry-request.js';` and `import { readAppConfig } from '../config.js';`. In `fetchNodeDetailHtml`, document + forward the option by adding `telemetryRequestsEnabled: options.telemetryRequestsEnabled === true,` to the `renderNodeDetailHtml` call's options. In `initializeNodeDetailPage`, compute `const telemetryRequestsEnabled = readAppConfig().telemetryRequestsEnabled === true;`, pass it into the `fetchNodeDetailHtml` options, and directly after `root.innerHTML = html;` add `bindTelemetryRequestButtons(root, { fetchImpl: options.fetchImpl });`.
  3. **node-detail-overlay.js**: add `import { bindTelemetryRequestButtons } from './node-page/telemetry-request.js';`; accept `telemetryRequestsEnabled` in `createNodeDetailOverlayManager` options (JSDoc too), include it in the `fetchDetail(reference, { … })` options object inside `open()`, and directly after `content.innerHTML = html;` add `bindTelemetryRequestButtons(content, { fetchImpl });`.
  4. **main.js**: add `import { readAppConfig } from './config.js';` (if not already imported) and extend the `createNodeDetailOverlayManager({ document, privateMode: isPrivateMode })` call at ~line 2290 with `telemetryRequestsEnabled: readAppConfig().telemetryRequestsEnabled === true,`.
  5. **base.css**: after the `.node-detail__badge` rule add:

```css
.node-detail__telemetry-request {
  margin: 0.4rem 0 0;
  padding: 0.25rem 0.7rem;
  font: inherit;
  font-size: 0.85rem;
  border: 1px solid var(--border-color, #4444);
  border-radius: 4px;
  background: transparent;
  color: inherit;
  cursor: pointer;
}

.node-detail__telemetry-request:disabled {
  opacity: 0.6;
  cursor: default;
}
```

  (If `--border-color` is not an existing custom property in base.css, use the literal the neighbouring button-like rules use instead — match the file, don't invent a token.)

- [ ] **Step 4: Run to verify pass, plus the whole JS suite**

Run: `cd web && npm test`
Expected: PASS (new tests green, no regressions)

- [ ] **Step 5: Commit**

```bash
git add web/public/assets/js/app/node-page/detail-html.js \
  web/public/assets/js/app/node-page/bootstrap.js \
  web/public/assets/js/app/node-detail-overlay.js \
  web/public/assets/js/app/main.js \
  web/public/assets/styles/base.css \
  web/public/assets/js/app/node-page/__tests__/telemetry-request.test.js
git commit -m "Render and bind the telemetry request button on node detail views"
```

---

### Task 8: Extract the shared contact-pull helper (ingestor refactor)

**Files:**
- Modify: `data/mesh_ingestor/protocols/meshcore/telemetry.py` (split `_poll_contact_telemetry`)
- Test: `tests/test_provider_unit.py` (existing telemetry tests must stay green; add one direct test)

**Interfaces:**
- Produces: `async _request_contact_telemetry(mc, iface, handlers, contact, node_id) -> bool` — the gated telemetry-then-status pull for one already-resolved contact; `True` when usable data was queued. `_poll_contact_telemetry` keeps its exact signature and becomes: entry gate → `_next_poll_contact` → node-id resolve → delegate.

- [ ] **Step 1: Write the failing test** (append to `tests/test_provider_unit.py` beside the other `mc_tel` tests):

```python
def test_request_contact_telemetry_pull_with_status_fallback(monkeypatch):
    """The extracted single-contact pull gates, counts, and falls back."""
    mc_tel, iface, stub, captured = _telemetry_env(
        monkeypatch, contacts=[{"public_key": _TEST_CONTACT_KEY, "adv_name": "Sensor"}]
    )
    contact = {"public_key": _TEST_CONTACT_KEY, "adv_name": "Sensor"}

    class _Cmds:
        async def req_telemetry_sync(self, _contact):
            return None  # timeout → falls back to status

        async def req_status_sync(self, _contact):
            return {"bat": 4056}

    class _MC:
        commands = _Cmds()

    ok = asyncio.run(
        mc_tel._request_contact_telemetry(_MC(), iface, stub, contact, "!11223344")
    )
    assert ok is True
    assert captured[0]["decoded"]["telemetry"]["deviceMetrics"] == {"voltage": 4.056}

    # Transmission forbidden → nothing sent, nothing queued.
    monkeypatch.setattr(mc_tel.config, "TX_ENABLED", False)
    captured.clear()
    ok = asyncio.run(
        mc_tel._request_contact_telemetry(_MC(), iface, stub, contact, "!11223344")
    )
    assert ok is False and captured == []
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest -q tests/test_provider_unit.py -k request_contact_telemetry`
Expected: FAIL (`AttributeError: … has no attribute '_request_contact_telemetry'`)

- [ ] **Step 3: Implement the split.** In `telemetry.py`, replace `_poll_contact_telemetry` with two functions. The new `_request_contact_telemetry` contains the current body **from the "poll initiated" debug log through the end**, verbatim except: it takes `contact` and `node_id` as parameters, gains a leading `if not tx_policy.transmit_permitted(): return False` (one gate per transmit site — the first send now carries its own), and returns `True`/`False` (`True` where the current code `return`s after a successful `_queue_meshcore_telemetry`, `False` on every failure path). Full PDoc docstring explaining it serves both the background round-robin and on-demand claims. The slimmed `_poll_contact_telemetry` (docstring updated):

```python
async def _poll_contact_telemetry(mc, iface, handlers, state) -> None:
    if not tx_policy.transmit_permitted():
        return
    contact = _next_poll_contact(iface, state)
    if contact is None:
        return
    node_id = iface.lookup_node_id((contact.get("public_key") or "")[:12])
    if node_id is None:
        return
    await _request_contact_telemetry(mc, iface, handlers, contact, node_id)
```

- [ ] **Step 4: Run to verify pass — the full provider suite (refactor must not regress)**

Run: `pytest -q tests/test_provider_unit.py`
Expected: PASS

- [ ] **Step 5: Format and commit**

```bash
black data/mesh_ingestor/protocols/meshcore/telemetry.py tests/test_provider_unit.py
git add data/mesh_ingestor/protocols/meshcore/telemetry.py tests/test_provider_unit.py
git commit -m "Extract shared single-contact telemetry pull helper"
```

---

### Task 9: Claim client module (ingestor)

**Files:**
- Create: `data/mesh_ingestor/protocols/meshcore/telemetry_requests.py`
- Test: `tests/test_meshcore_telemetry_requests_unit.py` (new file)

**Interfaces:**
- Consumes: `config.INSTANCES` (tuple of `(instance_url, api_token)` pairs), `config._debug_log`.
- Produces: `_claim_telemetry_request() -> tuple[dict | None, bool]` — `(claimed payload or None, feature_seen)`, `feature_seen=False` only when every instance 404s (feature off → caller backs off); `_find_roster_contact(iface, node_id) -> dict | None`; constants `_CLAIM_POLL_SECONDS = 30`, `_CLAIM_BACKOFF_SECONDS = 300`, `_CLAIM_TIMEOUT_SECS = 10`.

- [ ] **Step 1: Write the failing tests** — new `tests/test_meshcore_telemetry_requests_unit.py` (full header + module docstring):

```python
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


def test_find_roster_contact_matches_and_misses():
    iface = _MeshcoreInterface(target=None)
    iface._update_contact({"public_key": _KEY, "adv_name": "Sensor"})
    contact = mc_req._find_roster_contact(iface, "!aabbccdd")
    assert contact is not None and contact["public_key"] == _KEY
    assert mc_req._find_roster_contact(iface, "!00000000") is None
```

(Adjust the expected node id to whatever `iface.lookup_node_id(_KEY[:12])` canonically returns — assert equality against that call rather than a literal if the derivation differs.)

- [ ] **Step 2: Run to verify failure**

Run: `pytest -q tests/test_meshcore_telemetry_requests_unit.py`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement** `telemetry_requests.py` (full header; module PDoc explaining TI-A3 on-demand extension and MA7 gating happens at the execution site in `telemetry.py`):

```python
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
    instance.  Network and HTTP errors are logged at debug severity and never
    raise — a dead instance must not kill the poll loop.

    Returns:
        ``(payload, feature_seen)`` — the claimed request mapping (or ``None``)
        and whether any instance answered something other than 404 (callers
        back off to :data:`_CLAIM_BACKOFF_SECONDS` when ``False``).
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
            config._debug_log(
                "telemetry request claim errored",
                context="meshcore.telemetry.request",
                severity="warning",
                url=url,
                error=str(exc),
            )
            continue
        if isinstance(payload, dict) and payload.get("nodeId"):
            return payload, True
    return None, saw_feature


def _find_roster_contact(iface, node_id: str):
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
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest -q tests/test_meshcore_telemetry_requests_unit.py`
Expected: PASS

- [ ] **Step 5: Format and commit**

```bash
black data/mesh_ingestor/protocols/meshcore/telemetry_requests.py tests/test_meshcore_telemetry_requests_unit.py
git add data/mesh_ingestor/protocols/meshcore/telemetry_requests.py tests/test_meshcore_telemetry_requests_unit.py
git commit -m "Add MeshCore telemetry-request claim client and roster resolver"
```

---

### Task 10: Execute claims from the telemetry poll loop

**Files:**
- Modify: `data/mesh_ingestor/protocols/meshcore/telemetry.py` (add `_execute_claimed_request`, `_claim_and_execute`; extend `_telemetry_poll_loop`)
- Test: `tests/test_meshcore_telemetry_requests_unit.py` (append)

**Interfaces:**
- Consumes: Task 8 `_request_contact_telemetry`, Task 9 module (imported as `from . import telemetry_requests`).
- Produces: `async _execute_claimed_request(mc, iface, handlers, state, request) -> None` (resolves contact, stamps the 24 h cooldown in `state["last_polled"]`, delegates the gated pull); `async _claim_and_execute(mc, iface, handlers, state) -> float` (returns the next claim delay); `_telemetry_poll_loop` gains a third deadline (`next_claim`) active only while `tx_policy.transmit_permitted()`.

- [ ] **Step 1: Write the failing tests** (append; import the telemetry test helpers from `test_provider_unit`):

```python
import asyncio
import time

from test_provider_unit import _telemetry_env, _TEST_CONTACT_KEY


def test_execute_claimed_request_pulls_and_stamps_cooldown(monkeypatch):
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
    mc_tel, iface, stub, captured = _telemetry_env(monkeypatch, contacts=[])
    state: dict = {}
    asyncio.run(
        mc_tel._execute_claimed_request(
            object(), iface, stub, state, {"id": 1, "nodeId": "!00000000"}
        )
    )
    assert captured == [] and state == {}


def test_claim_and_execute_returns_backoff_when_feature_off(monkeypatch):
    mc_tel, iface, stub, _captured = _telemetry_env(monkeypatch, contacts=[])
    monkeypatch.setattr(
        mc_tel.telemetry_requests,
        "_claim_telemetry_request",
        lambda: (None, False),
    )
    delay = asyncio.run(mc_tel._claim_and_execute(object(), iface, stub, {}))
    assert delay == mc_tel.telemetry_requests._CLAIM_BACKOFF_SECONDS


def test_claim_and_execute_returns_poll_interval_when_feature_on(monkeypatch):
    mc_tel, iface, stub, _captured = _telemetry_env(monkeypatch, contacts=[])
    monkeypatch.setattr(
        mc_tel.telemetry_requests,
        "_claim_telemetry_request",
        lambda: (None, True),
    )
    delay = asyncio.run(mc_tel._claim_and_execute(object(), iface, stub, {}))
    assert delay == mc_tel.telemetry_requests._CLAIM_POLL_SECONDS
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest -q tests/test_meshcore_telemetry_requests_unit.py`
Expected: FAIL (`AttributeError: … '_execute_claimed_request'`)

- [ ] **Step 3: Implement.** In `telemetry.py` add `from . import telemetry_requests` to the imports, then:

```python
async def _execute_claimed_request(mc, iface, handlers, state, request) -> None:
    """Execute one claimed on-demand telemetry request (TI-A3 extension).

    Resolves the roster contact for the request's node, stamps the background
    loop's 24 h per-node cooldown *before* transmitting (mirroring
    :func:`_next_poll_contact` so a failed pull is not retried by the
    round-robin either), and delegates to the shared, MA7-gated pull.
    Requests for nodes outside the roster are dropped with a debug line —
    the web app offers the button for every MeshCore node it knows.

    Parameters:
        mc: Connected MeshCore instance.
        iface: Active interface (roster + node-id resolution).
        handlers: The ``data.mesh_ingestor.handlers`` module.
        state: Mutable poll-loop state (shares ``last_polled`` stamps).
        request: Claimed request mapping (``nodeId`` key).
    """
    node_id = request.get("nodeId")
    if not isinstance(node_id, str) or not node_id:
        return
    contact = telemetry_requests._find_roster_contact(iface, node_id)
    if contact is None:
        config._debug_log(
            "MeshCore telemetry request for unknown contact dropped",
            context="meshcore.telemetry.request",
            node_id=node_id,
        )
        return
    state.setdefault("last_polled", {})[contact.get("public_key")] = time.monotonic()
    await _request_contact_telemetry(mc, iface, handlers, contact, node_id)


async def _claim_and_execute(mc, iface, handlers, state) -> float:
    """Poll the claim endpoint once and execute any claimed request.

    The blocking HTTP claim runs in a worker thread so the event loop stays
    responsive.  Returns the delay until the next claim poll: the regular
    interval while any instance serves the feature, the long backoff when
    every instance 404s (feature disabled server-side).

    Parameters mirror :func:`_execute_claimed_request`.

    Returns:
        Seconds until the next claim poll.
    """
    claimed, feature_seen = await asyncio.to_thread(
        telemetry_requests._claim_telemetry_request
    )
    if claimed is not None:
        await _execute_claimed_request(mc, iface, handlers, state, claimed)
    return (
        telemetry_requests._CLAIM_POLL_SECONDS
        if feature_seen
        else telemetry_requests._CLAIM_BACKOFF_SECONDS
    )
```

Extend `_telemetry_poll_loop`: after the `poll_interval` assignment add

```python
    claim_interval = (
        telemetry_requests._CLAIM_POLL_SECONDS if tx_policy.transmit_permitted() else 0
    )
```

include `claim_interval <= 0` in the all-disabled early return (`if self_interval <= 0 and poll_interval <= 0 and claim_interval <= 0:`), add `next_claim = time.monotonic() + claim_interval if claim_interval > 0 else None` beside `next_poll`, and inside the loop body add:

```python
        if next_claim is not None and now >= next_claim:
            delay = await _claim_and_execute(mc, iface, _handlers, state)
            next_claim = time.monotonic() + delay
```

and extend the deadlines list to `(next_self, next_poll, next_claim)`. Update the loop's docstring to mention the claim poll and its MA7 gating.

- [ ] **Step 4: Run to verify pass — both Python suites**

Run: `pytest -q tests/test_meshcore_telemetry_requests_unit.py tests/test_provider_unit.py`
Expected: PASS

- [ ] **Step 5: Format and commit**

```bash
black data/ tests/
git add data/mesh_ingestor/protocols/meshcore/telemetry.py tests/test_meshcore_telemetry_requests_unit.py
git commit -m "Execute claimed telemetry requests from the MeshCore poll loop"
```

---

### Task 11: Documentation, contracts, acceptance, env samples, full suites

**Files:**
- Modify: `data/mesh_ingestor/CONTRACTS.md` (new subsections after `#### POST /api/ingestors`, ~line 292)
- Modify: `SPEC.md` (append a feature section at the end)
- Modify: `ACCEPTANCE.md` (append Layer-C checks in the API-contract area)
- Modify: `.env.example` (after the `MESHCORE_TELEMETRY_POLL_SECONDS` block, ~line 176)
- Modify: `README.md` (three rows in the web configuration/env table — locate the table documenting `PRIVATE`/`API_TOKEN`)

**Interfaces:** none (docs only).

- [ ] **Step 1: CONTRACTS.md** — add:

```markdown
#### `POST /api/telemetry-requests`

Viewer-facing (no token). Exists only when `TELEMETRY_REQUESTS=1`; otherwise
404 (route indistinguishable from absent). Body: `{"nodeId": "!xxxxxxxx"}`
(`node_id` accepted). Gates in order: parseable JSON object (400), canonical
node id (400), node known (404), node protocol `meshcore` (422), per-node
cooldown `TELEMETRY_REQUEST_COOLDOWN_SECONDS` (default 900, floor 300) and
global `TELEMETRY_REQUEST_HOURLY_CAP` (default 12; `<= 0` disables accepts) —
both 429 with a `Retry-After` header and `retryAfterSeconds` field. Success:
202 `{"status":"ok","cooldownSeconds":N}` and one row inserted into
`telemetry_requests`. Accepted requests are advisory: execution requires an
ingestor with `TX_ENABLED=1` (SPEC MA7); unclaimed rows expire after 600 s.

#### `POST /api/telemetry-requests/claim`

Ingestor-facing (`Authorization: Bearer` ingest token). 404 when the feature
flag is off. Atomically claims the oldest unclaimed request younger than
600 s: 200 `{"id":N,"nodeId":"!xxxxxxxx","requestedAt":N}`, or 204 when none
pending. The single-UPDATE claim makes co-operating ingestors safe — first
claimer wins; rows older than 7 days are pruned in the same transaction.
```

- [ ] **Step 2: SPEC.md** — append:

```markdown
## Feature: On-demand telemetry requests from the UI (MeshCore)

Viewer-facing button on MeshCore node detail views queueing an on-air
`req_telemetry` pull, executed by the MeshCore ingestor through the same
gated path as the background poll loop (TI-A3). Design record:
`docs/superpowers/specs/2026-08-29-telemetry-request-button-design.md`.

| # | Decision | Source |
|---|----------|--------|
| **TQ1** | **Hidden unless enabled.** The whole feature — both routes and the button — exists only when the web operator sets `TELEMETRY_REQUESTS=1` (default `0`). Flag off ⇒ both routes 404 and the flag serialises `false` into the frontend config. | interview |
| **TQ2** | **Fire-and-forget UX.** A click gets an immediate 202; results arrive through the unchanged telemetry ingest → SSE pipeline. No request-lifecycle tracking crosses the web/ingestor boundary. | interview |
| **TQ3** | **Rate limits: configurable per-node cooldown (floor 300 s) + global hourly cap.** `TELEMETRY_REQUEST_COOLDOWN_SECONDS` (default 900) clamps up to a 300 s floor — fail-safe toward less airtime; `TELEMETRY_REQUEST_HOURLY_CAP` (default 12, `<= 0` disables accepts). An executed on-demand pull also stamps the background loop's 24 h per-node cooldown so the round-robin does not re-poll the same node. | interview |
| **TQ4** | **Pull-only claim channel, MA7 intact.** The ingestor polls a token-guarded atomic claim route (~30 s; 5 min backoff while every instance 404s) from inside the existing telemetry poll loop — no broker, no push, apex invariant untouched. Every transmission still passes `tx_policy.transmit_permitted()` at the send site; with `TX_ENABLED=0` accepted requests simply expire unclaimed (600 s claim window). | interview |
| **TQ5** | **All MeshCore nodes show the button; the ingestor validates the roster.** The web app cannot know roster membership, so requests for non-contacts are claimed and dropped with a debug log rather than gated in the UI. | interview |
| **TQ6** | **Engineering bar (D9).** 100 % unit tests across all three languages (route gates incl. cooldown floor and cap, atomic claim, claim-client error paths and backoff, roster miss, MA7 gate per send, UI render gates and click states), full API docs, Apache headers, `black`/`rufo` clean, CONTRACTS/ACCEPTANCE updated. | CLAUDE.md |
```

- [ ] **Step 3: ACCEPTANCE.md** — append beside the other Layer-C route checks:

```markdown
### TQ-C1 — Telemetry-request routes are flag-gated and rate-limited — TQ1/TQ3

Start the web app with `API_TOKEN=acctest TELEMETRY_REQUESTS=0`, then:

    curl -s -o /dev/null -w '%{http_code}' -X POST localhost:41447/api/telemetry-requests -d '{"nodeId":"!deadbeef"}'
    # Expected: 404
    curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Authorization: Bearer acctest' localhost:41447/api/telemetry-requests/claim
    # Expected: 404

Restart with `TELEMETRY_REQUESTS=1`, seed a meshcore node via `POST /api/nodes`
(protocol `meshcore`), then:

    curl -s -o /dev/null -w '%{http_code}' -X POST localhost:41447/api/telemetry-requests -d '{"nodeId":"<seeded id>"}'
    # Expected: 202
    curl -s -o /dev/null -w '%{http_code}' -X POST localhost:41447/api/telemetry-requests -d '{"nodeId":"<seeded id>"}'
    # Expected: 429 (repeat inside the cooldown)
    curl -s -o /dev/null -w '%{http_code}' -X POST localhost:41447/api/telemetry-requests/claim
    # Expected: 403 (no token)
    curl -s -X POST -H 'Authorization: Bearer acctest' localhost:41447/api/telemetry-requests/claim
    # Expected: 200 with {"id":…,"nodeId":"<seeded id>",…}; a second call returns 204
```

- [ ] **Step 4: `.env.example`** — after the `MESHCORE_TELEMETRY_POLL_SECONDS` block add:

```bash
# On-demand telemetry requests from the web UI (0=off, 1=on). Shows a
# "Request telemetry" button on MeshCore nodes; the ingestor only transmits
# the request if TX_ENABLED=1.
# TELEMETRY_REQUESTS=0

# Per-node cooldown between accepted requests, seconds (floor 300).
# TELEMETRY_REQUEST_COOLDOWN_SECONDS=900

# Global cap on accepted requests per hour (0 disables accepts).
# TELEMETRY_REQUEST_HOURLY_CAP=12
```

- [ ] **Step 5: README.md** — add three rows to the web env table (task-oriented, one sentence each):

```markdown
| `TELEMETRY_REQUESTS` | `0` | Set `1` to show a "Request telemetry" button on MeshCore nodes; the ingestor only transmits if it runs with `TX_ENABLED=1`. |
| `TELEMETRY_REQUEST_COOLDOWN_SECONDS` | `900` | Seconds before the same node can be requested again; values under 300 are raised to 300. |
| `TELEMETRY_REQUEST_HOURLY_CAP` | `12` | Accepted requests per hour across all nodes; `0` disables accepting requests. |
```

- [ ] **Step 6: Run every suite and linter**

```bash
cd web && bundle exec rufo . && bundle exec rspec && npm test && cd ..
black --check data/ tests/ && pytest -q tests/
```

Expected: all PASS, formatters clean.

- [ ] **Step 7: Commit**

```bash
git add data/mesh_ingestor/CONTRACTS.md SPEC.md ACCEPTANCE.md .env.example README.md
git commit -m "Document on-demand telemetry requests (contracts, spec TQ1-TQ6, acceptance, env)"
```
