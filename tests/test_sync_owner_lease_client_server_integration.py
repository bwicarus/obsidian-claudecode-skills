from __future__ import annotations

import contextlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

from flask import Flask, request
from werkzeug.serving import WSGIRequestHandler, make_server


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

from reader_sync_relay import (  # noqa: E402
    CONTRACT,
    OWNER_LEASE_CONTRACT,
    register_reader_sync_relay,
)


ACCOUNT_NAMESPACE = "acct-v1-" + "a" * 64
REGISTRY_DIGEST = (
    "sync-v3:record-parent-state/1|"
    "card-entities:explicit:0:1|"
    "card-states:explicit:0:1|"
    "user-settings:explicit:0:1|"
    "vocabulary-state:explicit:0:1"
)
DEVICE_FAMILY_ID = "pwa-install-v1-" + "b" * 32


class _QuietRequestHandler(WSGIRequestHandler):
    def log(self, request_type, message, *args):  # noqa: ANN001
        del request_type, message, args


class SyncOwnerLeaseClientServerIntegrationTest(unittest.TestCase):
    def test_real_node_client_claims_uses_and_releases_flask_lease(self):
        with tempfile.TemporaryDirectory() as temp_name:
            sync_root = Path(temp_name)
            app = Flask(__name__)
            app.secret_key = "sync-owner-client-server-test"

            def resolver():
                if request.headers.get("X-Test-Account") == "a":
                    return {
                        "user_id": 1,
                        "storage_namespace": ACCOUNT_NAMESPACE,
                    }
                return None

            app.extensions["reader_storage_identity_resolver"] = resolver
            register_reader_sync_relay(app, root=sync_root)

            server = make_server(
                "127.0.0.1",
                0,
                app,
                threaded=True,
                request_handler=_QuietRequestHandler,
            )
            thread = threading.Thread(
                target=server.serve_forever,
                name="sync-owner-test-server",
                daemon=True,
            )
            thread.start()
            origin = f"http://127.0.0.1:{server.server_port}"

            module_path = (
                ROOT
                / "_server_deploy"
                / "static"
                / "reader-runtime"
                / "sync-owner-lease.js"
            )
            node_script = f"""
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const leaseModule = require({json.dumps(str(module_path))});

const origin = {json.dumps(origin)};
const ownerNamespace = {json.dumps(ACCOUNT_NAMESPACE)};
const registryDigest = {json.dumps(REGISTRY_DIGEST)};
const deviceFamilyId = {json.dumps(DEVICE_FAMILY_ID)};
const ownerInstanceId = 'integration-owner-extension';
const deviceId = 'integration-device';
const ownerResponses = [];

async function authenticatedFetch(path, body) {{
  const response = await fetch(origin + path, {{
    method: 'POST',
    headers: {{
      'Accept': 'application/json',
      'Content-Type': 'application/json',
      'X-Test-Account': 'a'
    }},
    body: JSON.stringify(body)
  }});
  const data = await response.json();
  ownerResponses.push({{ path, status: response.status, data }});
  if (!response.ok || !data || data.ok === false) {{
    const error = new Error(data && data.error || 'request failed');
    error.code = data && data.code || 'BW_SYNC_OWNER_INACTIVE';
    error.retryable = (
      !response.status ||
      response.status === 408 ||
      response.status === 429 ||
      response.status >= 500
    );
    error.status = response.status;
    throw error;
  }}
  return data;
}}

async function rawPost(path, body) {{
  const response = await fetch(origin + path, {{
    method: 'POST',
    headers: {{
      'Accept': 'application/json',
      'Content-Type': 'application/json',
      'X-Test-Account': 'a'
    }},
    body: JSON.stringify(body)
  }});
  return {{ response, data: await response.json() }};
}}

(async () => {{
  assert.equal(leaseModule.CONTRACT, {json.dumps(OWNER_LEASE_CONTRACT)});
  const lease = leaseModule.createSyncOwnerLease({{
    ownerNamespace,
    deviceId,
    deviceFamilyId,
    ownerRole: 'extension',
    ownerInstanceId,
    syncContract: 'sync-v3',
    syncChangeContract: 'record-parent-state/1',
    registryDigest,
    request: authenticatedFetch,
    autoRenew: false
  }});

  const fields = await lease.start();
  assert.equal(ownerResponses.length, 1);
  assert.equal(ownerResponses[0].path, leaseModule.CLAIM_PATH);
  assert.equal(ownerResponses[0].status, 200);
  assert.equal(ownerResponses[0].data.contract, leaseModule.CONTRACT);
  assert.equal(ownerResponses[0].data.ownerGeneration, 1);
  assert.equal(ownerResponses[0].data.ownerToken, fields.ownerToken);
  assert.equal(ownerResponses[0].data.released, false);
  assert.equal(ownerResponses[0].data.replayed, false);
  assert.ok(Number.isSafeInteger(ownerResponses[0].data.expiresAt));
  assert.deepEqual(Object.keys(fields).sort(), [
    'deviceFamilyId',
    'ownerGeneration',
    'ownerInstanceId',
    'ownerRole',
    'ownerToken'
  ]);
  assert.equal(fields.deviceFamilyId, deviceFamilyId);
  assert.equal(fields.ownerRole, 'extension');
  assert.equal(fields.ownerInstanceId, ownerInstanceId);
  assert.equal(fields.ownerGeneration, 1);
  assert.match(fields.ownerToken, /^owner-token-v1-[A-Za-z0-9_-]{{24,256}}$/);
  assert.deepEqual(lease.fields(), fields);
  assert.deepEqual(
    {{
      contract: lease.status().contract,
      state: lease.status().state,
      generation: lease.status().generation
    }},
    {{
      contract: {json.dumps(OWNER_LEASE_CONTRACT)},
      state: 'active',
      generation: 1
    }}
  );

  const businessBody = {{
    contract: {json.dumps(CONTRACT)},
    syncContract: 'sync-v3',
    syncChangeContract: 'record-parent-state/1',
    registryDigest,
    ownerNamespace,
    deviceId,
    direction: 'pull',
    cursor: 0,
    limit: 100,
    changes: [],
    ...fields
  }};
  const accepted = await rawPost('/api/reader/sync/exchange', businessBody);
  assert.equal(accepted.response.status, 200);
  assert.equal(accepted.data.ok, true);
  assert.equal(accepted.data.contract, {json.dumps(CONTRACT)});
  assert.equal(accepted.data.headCursor, 0);

  const tokenHash = crypto
    .createHash('sha256')
    .update(fields.ownerToken, 'utf8')
    .digest('hex');
  assert.equal(await lease.release('integration-test'), true);
  assert.equal(lease.status().state, 'stopped');
  assert.equal(ownerResponses.length, 2);
  assert.equal(ownerResponses[1].path, leaseModule.RELEASE_PATH);
  assert.equal(ownerResponses[1].status, 200);
  assert.equal(ownerResponses[1].data.contract, leaseModule.CONTRACT);
  assert.equal(ownerResponses[1].data.ownerGeneration, 1);
  assert.equal(ownerResponses[1].data.released, true);
  assert.equal(ownerResponses[1].data.replayed, false);
  assert.ok(!Object.prototype.hasOwnProperty.call(
    ownerResponses[1].data,
    'ownerToken'
  ));

  const rejected = await rawPost('/api/reader/sync/exchange', businessBody);
  assert.equal(rejected.response.status, 409);
  assert.equal(rejected.data.ok, false);
  assert.equal(rejected.data.contract, {json.dumps(CONTRACT)});
  assert.equal(rejected.data.code, 'BW_SYNC_OWNER_INACTIVE');
  assert.ok(!Object.prototype.hasOwnProperty.call(rejected.data, 'ownerToken'));

  process.stdout.write(JSON.stringify({{
    contract: leaseModule.CONTRACT,
    ownerGeneration: fields.ownerGeneration,
    tokenHash,
    acceptedContract: accepted.data.contract,
    rejectedCode: rejected.data.code
  }}));
}})().catch((error) => {{
  console.error(error && error.stack || error);
  process.exitCode = 1;
}});
"""

            try:
                completed = subprocess.run(
                    ["node", "-e", node_script],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertFalse(thread.is_alive(), "temporary Flask server leaked")
            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr or completed.stdout,
            )
            result = json.loads(completed.stdout)
            self.assertEqual(result["contract"], OWNER_LEASE_CONTRACT)
            self.assertEqual(result["ownerGeneration"], 1)
            self.assertEqual(result["acceptedContract"], CONTRACT)
            self.assertEqual(result["rejectedCode"], "BW_SYNC_OWNER_INACTIVE")

            database = sync_root / ACCOUNT_NAMESPACE / "relay.sqlite3"
            self.assertTrue(database.is_file())
            with contextlib.closing(sqlite3.connect(database)) as connection:
                stored = connection.execute(
                    "SELECT generation,token_sha256,released_at,expires_at "
                    "FROM sync_owner_leases WHERE device_family_id=?",
                    (DEVICE_FAMILY_ID,),
                ).fetchone()
            self.assertIsNotNone(stored)
            self.assertEqual(stored[0], 1)
            self.assertEqual(stored[1], result["tokenHash"])
            self.assertIsNotNone(stored[2])
            self.assertEqual(stored[3], 0)

    def test_real_node_coordinator_migrates_v2_proofless_record_once(self):
        with tempfile.TemporaryDirectory() as temp_name:
            sync_root = Path(temp_name)
            app = Flask(__name__)
            app.secret_key = "sync-causal-migration-client-server-test"

            def resolver():
                if request.headers.get("X-Test-Account") == "a":
                    return {
                        "user_id": 1,
                        "storage_namespace": ACCOUNT_NAMESPACE,
                    }
                return None

            app.extensions["reader_storage_identity_resolver"] = resolver
            register_reader_sync_relay(app, root=sync_root)

            server = make_server(
                "127.0.0.1",
                0,
                app,
                threaded=True,
                request_handler=_QuietRequestHandler,
            )
            thread = threading.Thread(
                target=server.serve_forever,
                name="sync-causal-migration-test-server",
                daemon=True,
            )
            thread.start()
            origin = f"http://127.0.0.1:{server.server_port}"
            runtime_root = (
                ROOT
                / "_server_deploy"
                / "static"
                / "reader-runtime"
            )
            module_paths = {
                name: runtime_root / filename
                for name, filename in {
                    "data_store": "data-store.js",
                    "data_registry": "data-registry.js",
                    "coordinator": "sync-coordinator.js",
                    "gateway": "sync-gateway.js",
                    "transport": "server-sync-transport.js",
                    "lease": "sync-owner-lease.js",
                }.items()
            }
            node_script = f"""
const assert = require('node:assert/strict');
const DataStore = require({json.dumps(str(module_paths["data_store"]))});
const DataRegistry = require({json.dumps(str(module_paths["data_registry"]))});
const Coordinator = require({json.dumps(str(module_paths["coordinator"]))});
const SyncGateway = require({json.dumps(str(module_paths["gateway"]))});
const ServerTransport = require({json.dumps(str(module_paths["transport"]))});
const OwnerLease = require({json.dumps(str(module_paths["lease"]))});

const origin = {json.dumps(origin)};
const ownerNamespace = {json.dumps(ACCOUNT_NAMESPACE)};
const registryDigest = {json.dumps(REGISTRY_DIGEST)};
const deviceFamilyId = {json.dumps(DEVICE_FAMILY_ID)};
const deviceId = 'integration-causal-migration-device';
const legacyDigest =
  'sync-v2:card-entities:explicit:0:1|card-states:explicit:0:1|user-settings:explicit:0:1|vocabulary-state:explicit:0:1';

async function authenticatedRequest(path, body) {{
  const response = await fetch(origin + path, {{
    method: 'POST',
    headers: {{
      'Accept': 'application/json',
      'Content-Type': 'application/json',
      'X-Test-Account': 'a'
    }},
    body: JSON.stringify(body)
  }});
  const data = await response.json();
  if (!response.ok || !data || data.ok === false) {{
    const error = new Error(data && data.error || 'request failed');
    error.code = data && data.code || 'BW_SYNC_HTTP';
    error.status = response.status;
    error.retryable = response.status >= 500;
    throw error;
  }}
  return data;
}}

(async () => {{
  const ownerLease = OwnerLease.createSyncOwnerLease({{
    ownerNamespace,
    deviceId,
    deviceFamilyId,
    ownerRole: 'extension',
    ownerInstanceId: 'integration-causal-migration-owner',
    syncContract: 'sync-v3',
    syncChangeContract: 'record-parent-state/1',
    registryDigest,
    request: authenticatedRequest,
    autoRenew: false
  }});
  await ownerLease.start();

  const backend = DataStore.createMemoryBackend();
  const legacyStore = DataStore.createDataStore({{
    backend,
    deviceId: 'legacy-v2-device',
    causalCollections: []
  }});
  const legacyRecord = await legacyStore.put(
    'user-settings',
    {{ id: 'theme', mode: 'dark' }},
    {{ mutationId: 'integration-v2-theme' }}
  );
  assert.equal(Object.hasOwn(legacyRecord, 'causal'), false);
  const store = DataStore.createDataStore({{
    backend,
    deviceId,
    causalCollections: DataRegistry.syncCollections()
  }});
  const checkpointStore = Coordinator.createMemoryCheckpointStore({{
    contract: Coordinator.CONTRACT,
    schema: 1,
    registryDigest: legacyDigest,
    generation: 1,
    server: {{
      localCursor: 0,
      remoteCursor: 0,
      reconciliationEpoch: 0
    }},
    peers: {{}}
  }});
  const transport = ServerTransport.createServerSyncTransport({{
    origin,
    ownerNamespace,
    deviceId,
    syncContract: 'sync-v3',
    syncChangeContract: 'record-parent-state/1',
    registryDigest,
    ownerLease,
    credentials: 'omit',
    headers: {{ 'X-Test-Account': 'a' }}
  }});
  const gateway = SyncGateway.createSyncGateway({{
    transport,
    deviceId
  }});
  const coordinator = Coordinator.createSyncCoordinator({{
    store,
    registry: DataRegistry,
    serverGateway: gateway,
    checkpointStore
  }});

  const first = await coordinator.runOnce();
  assert.equal(first.causalMigration.ok, true);
  assert.equal(first.causalMigration.migrated, 1);
  assert.equal(first.causalMigration.baselineCursor, 0);
  assert.equal(first.server.ok, true);
  assert.equal(first.server.localCursor, 1);
  const localChange = (await store.changes({{ after: 0, limit: 10 }})).changes[0];
  assert.deepEqual(localChange.record.causal, {{
    contract: 'record-parent-state/1',
    parent: null
  }});
  const remoteAfterFirst = await gateway.snapshot({{
    offset: 0,
    limit: 10
  }});
  assert.equal(remoteAfterFirst.snapshotCursor, 1);
  assert.equal(remoteAfterFirst.changes.length, 1);
  assert.deepEqual(remoteAfterFirst.changes[0].record.causal, {{
    contract: 'record-parent-state/1',
    parent: null
  }});
  assert.equal(checkpointStore.inspect().registryDigest, registryDigest);

  const restarted = Coordinator.createSyncCoordinator({{
    store,
    registry: DataRegistry,
    serverGateway: gateway,
    checkpointStore
  }});
  const second = await restarted.runOnce();
  assert.equal(second.causalMigration, null);
  const remoteAfterSecond = await gateway.snapshot({{
    offset: 0,
    limit: 10
  }});
  assert.equal(remoteAfterSecond.snapshotCursor, 1);
  assert.equal(remoteAfterSecond.changes.length, 1);
  await ownerLease.release('integration-causal-migration-complete');

  process.stdout.write(JSON.stringify({{
    migrated: first.causalMigration.migrated,
    localCursor: first.server.localCursor,
    remoteCursor: remoteAfterSecond.snapshotCursor,
    causalParent: remoteAfterSecond.changes[0].record.causal.parent,
    checkpointDigest: checkpointStore.inspect().registryDigest
  }}));
}})().catch((error) => {{
  console.error(error && error.stack || error);
  process.exitCode = 1;
}});
"""

            try:
                completed = subprocess.run(
                    ["node", "-e", node_script],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertFalse(thread.is_alive(), "temporary Flask server leaked")
            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr or completed.stdout,
            )
            result = json.loads(completed.stdout)
            self.assertEqual(result["migrated"], 1)
            self.assertEqual(result["localCursor"], 1)
            self.assertEqual(result["remoteCursor"], 1)
            self.assertIsNone(result["causalParent"])
            self.assertEqual(result["checkpointDigest"], REGISTRY_DIGEST)

            database = sync_root / ACCOUNT_NAMESPACE / "relay.sqlite3"
            with contextlib.closing(sqlite3.connect(database)) as connection:
                events = connection.execute(
                    "SELECT COUNT(*) FROM relay_events"
                ).fetchone()[0]
                stored_change = json.loads(connection.execute(
                    "SELECT change_json FROM relay_heads "
                    "WHERE collection='user-settings' AND record_id='theme'"
                ).fetchone()[0])
            self.assertEqual(events, 1)
            self.assertEqual(
                stored_change["record"]["causal"],
                {"contract": "record-parent-state/1", "parent": None},
            )


if __name__ == "__main__":
    unittest.main()
