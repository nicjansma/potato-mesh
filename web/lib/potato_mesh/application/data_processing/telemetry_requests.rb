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
