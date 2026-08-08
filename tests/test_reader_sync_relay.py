from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from flask import Flask, request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

from reader_sync_relay import (  # noqa: E402
    CONTRACT,
    MAX_SIGNAL_PEERS,
    MAX_SIGNAL_PAYLOAD_BYTES,
    MAX_SIGNALS,
    NATIVE_SYNC_BOOTSTRAP_CONTRACT,
    NATIVE_SYNC_BOOTSTRAP_TTL_SECONDS,
    OWNER_LEASE_CONTRACT,
    OWNER_LEASE_TTL_SECONDS,
    SIGNAL_CONTRACT,
    SIGNAL_MESSAGE_TTL_SECONDS,
    SIGNAL_PRESENCE_TTL_SECONDS,
    _account_proof,
    register_reader_sync_relay,
)


NS_A = "acct-v1-" + "a" * 64
NS_B = "acct-v1-" + "b" * 64
REGISTRY_DIGEST = (
    "sync-v3:record-parent-state/1|"
    "user-settings:explicit:0:1|"
    "vocabulary-state:explicit:0:1"
)
OTHER_REGISTRY_DIGEST = (
    "sync-v3:record-parent-state/1|"
    "user-settings:explicit:0:2|"
    "vocabulary-state:explicit:0:1"
)


def _family_id(label: str) -> str:
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()[:32]
    return "pwa-install-v1-" + digest


def _native_family_id(label: str) -> str:
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()[:32]
    return "native-app-v1-" + digest


def _parent(record: dict | None):
    if record is None:
        return None
    if record["deleted"]:
        return {"deleted": True}
    return {
        "deleted": False,
        "value": json.loads(json.dumps(record["value"])),
    }


def _change(
    index: int,
    *,
    record_id: str | None = None,
    revision: int = 1,
    deleted: bool = False,
    mutation_id: str | None = None,
    value: dict | None = None,
    parent=None,
    causal: bool = True,
) -> dict:
    record_id = record_id or f"setting-{index}"
    change = {
        "cursor": index,
        "mutationId": mutation_id or f"mutation-{index}",
        "operation": "remove" if deleted else "put",
        "collection": "user-settings",
        "record": {
            "schema": 1,
            "collection": "user-settings",
            "id": record_id,
            "rev": revision,
            "updatedAt": 1_700_000_000_000 + index,
            "updatedBy": "test-device",
            "deleted": deleted,
            "value": value or {"id": record_id, "value": index},
        },
    }
    if causal:
        change["record"]["causal"] = {
            "contract": "record-parent-state/1",
            "parent": json.loads(json.dumps(parent)) if parent is not None else None,
        }
    return change


class ReaderSyncRelayTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Flask(__name__)
        self.app.secret_key = "reader-sync-test"

        def resolver():
            account = request.headers.get("X-Test-Account", "")
            if account == "a":
                return {"user_id": 1, "storage_namespace": NS_A}
            if account == "b":
                return {"user_id": 2, "storage_namespace": NS_B}
            return None

        self.app.extensions["reader_storage_identity_resolver"] = resolver
        register_reader_sync_relay(self.app, root=self.root)
        self.client = self.app.test_client()
        self._leases: dict[tuple[str, str], dict] = {}

    def tearDown(self):
        self.temp.cleanup()

    def _body(self, namespace=NS_A, **updates):
        value = {
            "contract": CONTRACT,
            "syncContract": "sync-v3",
            "syncChangeContract": "record-parent-state/1",
            "registryDigest": REGISTRY_DIGEST,
            "ownerNamespace": namespace,
            "deviceId": "device-test",
            "direction": "push",
            "cursor": 0,
            "limit": 100,
            "changes": [],
        }
        value.update(updates)
        return value

    def _lease_holder(
        self,
        *,
        account: str,
        namespace: str,
        device_id: str,
        family_label: str | None = None,
        owner_role: str = "extension",
        owner_instance_id: str | None = None,
    ) -> dict:
        family_id = _family_id(
            family_label or f"{account}:{device_id}"
        )
        return {
            "deviceFamilyId": family_id,
            "ownerRole": owner_role,
            "ownerInstanceId": owner_instance_id or (
                "test-owner-" + hashlib.sha256(
                    f"{account}:{device_id}:{owner_role}".encode("utf-8")
                ).hexdigest()[:20]
            ),
            "deviceId": device_id,
            "ownerNamespace": namespace,
        }

    def _owner_request(
        self,
        holder: dict,
        *,
        registry_digest=REGISTRY_DIGEST,
        credentials: dict | None = None,
    ) -> dict:
        return {
            "contract": OWNER_LEASE_CONTRACT,
            "syncContract": "sync-v3",
            "syncChangeContract": "record-parent-state/1",
            "registryDigest": registry_digest,
            **holder,
            **(credentials or {}),
        }

    def _claim(
        self,
        holder: dict,
        *,
        account="a",
        registry_digest=REGISTRY_DIGEST,
    ):
        return self.client.post(
            "/api/reader/sync/owner/claim",
            json=self._owner_request(
                holder,
                registry_digest=registry_digest,
            ),
            headers={"X-Test-Account": account},
        )

    def _attach_owner_lease(self, body: dict, *, account: str):
        body = dict(body)
        namespace = str(body.get("ownerNamespace") or "")
        expected_namespace = NS_A if account == "a" else NS_B if account == "b" else ""
        device_id = body.get("deviceId")
        if (
            namespace != expected_namespace
            or not isinstance(device_id, str)
            or not device_id
        ):
            return body, None
        holder = self._lease_holder(
            account=account,
            namespace=namespace,
            device_id=device_id,
        )
        key = (account, holder["deviceFamilyId"])
        lease = self._leases.get(key)
        if lease:
            renewed = self.client.post(
                "/api/reader/sync/owner/renew",
                json=self._owner_request(
                    holder,
                    registry_digest=lease["registryDigest"],
                    credentials={
                        "ownerGeneration": lease["ownerGeneration"],
                        "ownerToken": lease["ownerToken"],
                    },
                ),
                headers={"X-Test-Account": account},
            )
            if renewed.status_code == 200:
                lease = {
                    **renewed.json,
                    "registryDigest": lease["registryDigest"],
                }
            elif renewed.json.get("code") == "BW_SYNC_OWNER_INACTIVE":
                lease = None
            else:
                return body, renewed
        if not lease:
            registry_digest = body.get("registryDigest", REGISTRY_DIGEST)
            claimed = self._claim(
                holder,
                account=account,
                registry_digest=registry_digest,
            )
            if claimed.status_code != 200:
                return body, claimed
            lease = {
                **claimed.json,
                "registryDigest": registry_digest,
            }
        self._leases[key] = lease
        body.update({
            "deviceFamilyId": holder["deviceFamilyId"],
            "ownerRole": holder["ownerRole"],
            "ownerInstanceId": holder["ownerInstanceId"],
            "ownerGeneration": lease["ownerGeneration"],
            "ownerToken": lease["ownerToken"],
        })
        return body, None

    def _post(self, path, body, account="a", *, add_lease=True):
        if path in {
            "/api/reader/sync/exchange",
            "/api/reader/sync/snapshot",
        }:
            body = dict(body)
            body.setdefault("syncContract", "sync-v3")
            body.setdefault("syncChangeContract", "record-parent-state/1")
            body.setdefault(
                "registryDigest",
                REGISTRY_DIGEST,
            )
        if add_lease and path in {
            "/api/reader/sync/exchange",
            "/api/reader/sync/snapshot",
            "/api/reader/sync/signal",
        }:
            body, lease_error = self._attach_owner_lease(
                body,
                account=account,
            )
            if lease_error is not None:
                return lease_error
        return self.client.post(
            path,
            json=body,
            headers={"X-Test-Account": account},
        )

    def _signal_body(self, namespace=NS_A, **updates):
        value = {
            "contract": SIGNAL_CONTRACT,
            "ownerNamespace": namespace,
            "deviceId": "device-a",
            "registryDigest": REGISTRY_DIGEST,
            "serverCursor": 0,
            "localCursor": 0,
            "serverReady": True,
            "signalCursor": 0,
            "signals": [],
        }
        value.update(updates)
        return value

    def _signal(
        self,
        signal_id: str,
        target: str,
        *,
        kind="offer",
        session_id="session-1",
        payload=None,
    ):
        return {
            "signalId": signal_id,
            "toDeviceId": target,
            "sessionId": session_id,
            "kind": kind,
            "payload": payload or {"type": kind, "value": signal_id},
        }

    def test_auth_owner_match_and_account_partition(self):
        denied = self.client.post(
            "/api/reader/sync/exchange",
            json=self._body(changes=[_change(1)]),
        )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.json["code"], "BW_SYNC_AUTH")

        mismatch = self._post(
            "/api/reader/sync/exchange",
            self._body(namespace=NS_B, changes=[_change(1)]),
        )
        self.assertEqual(mismatch.status_code, 403)
        self.assertEqual(mismatch.json["code"], "BW_SYNC_OWNER_MISMATCH")

        pushed = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[_change(1)]),
        )
        self.assertEqual(pushed.status_code, 200)
        pull_b = self._post(
            "/api/reader/sync/exchange",
            self._body(
                namespace=NS_B,
                direction="pull",
                changes=[],
            ),
            account="b",
        )
        self.assertEqual(pull_b.json["changes"], [])
        self.assertEqual(pull_b.json["headCursor"], 0)

    def test_gateway_v2_fence_rejects_old_or_missing_contract_before_storage(self):
        old = self.client.post(
            "/api/reader/sync/exchange",
            json={
                **self._body(),
                "contract": "sync-gateway/1",
            },
            headers={"X-Test-Account": "a"},
        )
        self.assertEqual(old.status_code, 400)
        self.assertEqual(old.json["code"], "BW_SYNC_CONTRACT")

        missing = self._body()
        missing.pop("syncChangeContract")
        response = self.client.post(
            "/api/reader/sync/exchange",
            json=missing,
            headers={"X-Test-Account": "a"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["code"], "BW_SYNC_CONTRACT")
        self.assertFalse((self.root / NS_A / "relay.sqlite3").exists())

    def test_account_pins_exact_registry_before_exchange_snapshot_or_signal(self):
        pinned = self._post(
            "/api/reader/sync/exchange",
            self._body(direction="pull"),
        )
        self.assertEqual(pinned.status_code, 200)

        mismatched_exchange = self._post(
            "/api/reader/sync/exchange",
            self._body(
                registryDigest=OTHER_REGISTRY_DIGEST,
                changes=[_change(1)],
            ),
        )
        mismatched_snapshot = self._post(
            "/api/reader/sync/snapshot",
            self._body(registryDigest=OTHER_REGISTRY_DIGEST),
        )
        mismatched_signal = self._post(
            "/api/reader/sync/signal",
            self._signal_body(
                deviceId="device-other-registry",
                registryDigest=OTHER_REGISTRY_DIGEST,
            ),
        )
        for response in (
            mismatched_exchange,
            mismatched_snapshot,
            mismatched_signal,
        ):
            self.assertEqual(response.status_code, 409)
            self.assertEqual(
                response.json["code"],
                "BW_SYNC_REGISTRY_MISMATCH",
            )

        with contextlib.closing(
            sqlite3.connect(self.root / NS_A / "relay.sqlite3")
        ) as connection:
            pinned_digest = connection.execute(
                "SELECT registry_digest FROM relay_state WHERE singleton=1"
            ).fetchone()[0]
            event_count = connection.execute(
                "SELECT COUNT(*) FROM relay_events"
            ).fetchone()[0]
            snapshot_count = connection.execute(
                "SELECT COUNT(*) FROM relay_snapshots"
            ).fetchone()[0]
            presence_count = connection.execute(
                "SELECT COUNT(*) FROM direct_presence"
            ).fetchone()[0]
        self.assertEqual(pinned_digest, REGISTRY_DIGEST)
        self.assertEqual(event_count, 0)
        self.assertEqual(snapshot_count, 0)
        self.assertEqual(presence_count, 0)

        # Digest 所有权按账户分区，不能误伤真正的另一账户/设备。
        other_account = self._post(
            "/api/reader/sync/exchange",
            self._body(
                namespace=NS_B,
                registryDigest=OTHER_REGISTRY_DIGEST,
                direction="pull",
            ),
            account="b",
        )
        self.assertEqual(other_account.status_code, 200)

    def test_owner_lease_claim_is_linearized_and_scoped_by_device_family(self):
        first = self._lease_holder(
            account="a",
            namespace=NS_A,
            device_id="device-family-a",
            family_label="shared-family",
            owner_instance_id="extension-instance-a",
        )
        competitor = {
            **first,
            "ownerInstanceId": "extension-instance-b",
        }

        def claim(holder: dict):
            with self.app.test_client() as client:
                return client.post(
                    "/api/reader/sync/owner/claim",
                    json=self._owner_request(holder),
                    headers={"X-Test-Account": "a"},
                )

        with mock.patch("reader_sync_relay.time.time", return_value=1_000):
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(claim, (first, competitor)))
        self.assertEqual(
            sorted(response.status_code for response in responses),
            [200, 409],
        )
        winner = next(response for response in responses if response.status_code == 200)
        loser = next(response for response in responses if response.status_code == 409)
        self.assertEqual(winner.json["contract"], OWNER_LEASE_CONTRACT)
        self.assertEqual(winner.json["ownerGeneration"], 1)
        self.assertEqual(
            winner.json["expiresAt"],
            1_000 + OWNER_LEASE_TTL_SECONDS,
        )
        self.assertRegex(
            winner.json["ownerToken"],
            r"^owner-token-v1-[A-Za-z0-9_-]{24,256}$",
        )
        self.assertEqual(loser.json["code"], "BW_SYNC_OWNER_HELD")
        self.assertNotIn("ownerToken", loser.json)
        winner_holder = (
            first
            if winner.json["ownerInstanceId"] == first["ownerInstanceId"]
            else competitor
        )
        winner_credentials = {
            "ownerGeneration": winner.json["ownerGeneration"],
            "ownerToken": winner.json["ownerToken"],
        }
        with mock.patch("reader_sync_relay.time.time", return_value=1_001):
            wrong_role = self.client.post(
                "/api/reader/sync/owner/renew",
                json=self._owner_request(
                    {
                        **winner_holder,
                        "ownerRole": "pwa",
                    },
                    credentials=winner_credentials,
                ),
                headers={"X-Test-Account": "a"},
            )
            wrong_device = self.client.post(
                "/api/reader/sync/owner/renew",
                json=self._owner_request(
                    {
                        **winner_holder,
                        "deviceId": "other-device",
                    },
                    credentials=winner_credentials,
                ),
                headers={"X-Test-Account": "a"},
            )
            renewed = self.client.post(
                "/api/reader/sync/owner/renew",
                json=self._owner_request(
                    winner_holder,
                    credentials=winner_credentials,
                ),
                headers={"X-Test-Account": "a"},
            )
        for rejected in (wrong_role, wrong_device):
            self.assertEqual(rejected.status_code, 409)
            self.assertEqual(
                rejected.json["code"],
                "BW_SYNC_OWNER_INACTIVE",
            )
        self.assertEqual(renewed.status_code, 200)
        self.assertEqual(renewed.json["ownerGeneration"], 1)
        self.assertEqual(renewed.json["ownerToken"], winner.json["ownerToken"])
        self.assertEqual(
            renewed.json["expiresAt"],
            1_001 + OWNER_LEASE_TTL_SECONDS,
        )

        # A different device family in the same account, and the same family
        # identifier in another account database, are independent leases.
        other_family = self._lease_holder(
            account="a",
            namespace=NS_A,
            device_id="device-family-b",
            family_label="other-family",
        )
        other_account = self._lease_holder(
            account="b",
            namespace=NS_B,
            device_id="device-family-a",
            family_label="shared-family",
        )
        with mock.patch("reader_sync_relay.time.time", return_value=1_000):
            self.assertEqual(self._claim(other_family).status_code, 200)
            self.assertEqual(
                self._claim(other_account, account="b").status_code,
                200,
            )

        with contextlib.closing(
            sqlite3.connect(self.root / NS_A / "relay.sqlite3")
        ) as connection:
            token_rows = connection.execute(
                "SELECT token_sha256 FROM sync_owner_leases"
            ).fetchall()
        self.assertEqual(len(token_rows), 2)
        self.assertTrue(all(len(row[0]) == 64 for row in token_rows))
        self.assertNotIn(
            winner.json["ownerToken"],
            {row[0] for row in token_rows},
        )
        invalid_family = {
            **other_family,
            "deviceFamilyId": "pwa-install-v1-" + "A" * 32,
        }
        rejected_family = self._claim(invalid_family)
        self.assertEqual(rejected_family.status_code, 400)
        self.assertEqual(rejected_family.json["code"], "BW_SYNC_INVALID")

    def test_native_owner_requires_authenticated_short_bootstrap(self):
        holder = {
            "deviceFamilyId": _native_family_id("native-a"),
            "ownerRole": "native",
            "ownerInstanceId": "native-owner-instance-a",
            "deviceId": "native-device-a",
            "ownerNamespace": NS_A,
        }
        bootstrap_body = {
            "contract": NATIVE_SYNC_BOOTSTRAP_CONTRACT,
            "requestId": "native-bootstrap-a",
            "syncContract": "sync-v3",
            "syncChangeContract": "record-parent-state/1",
            "registryDigest": REGISTRY_DIGEST,
            "deviceFamilyId": holder["deviceFamilyId"],
            "ownerInstanceId": holder["ownerInstanceId"],
            "deviceId": holder["deviceId"],
        }

        unauthenticated = self.client.post(
            "/api/reader/sync/native/bootstrap",
            json=bootstrap_body,
        )
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(unauthenticated.json["code"], "BW_SYNC_AUTH")
        self.assertNotIn("ownerNamespace", unauthenticated.json)
        self.assertNotIn("nativeBootstrapToken", unauthenticated.json)

        without_bootstrap = self._claim(holder)
        self.assertEqual(without_bootstrap.status_code, 403)
        self.assertEqual(
            without_bootstrap.json["code"],
            "BW_SYNC_NATIVE_BOOTSTRAP",
        )

        with mock.patch("reader_sync_relay.time.time", return_value=1_000):
            bootstrapped = self.client.post(
                "/api/reader/sync/native/bootstrap",
                json=bootstrap_body,
                headers={"X-Test-Account": "a"},
            )
        self.assertEqual(bootstrapped.status_code, 200)
        self.assertEqual(
            bootstrapped.json["contract"],
            NATIVE_SYNC_BOOTSTRAP_CONTRACT,
        )
        self.assertEqual(bootstrapped.json["requestId"], "native-bootstrap-a")
        self.assertEqual(bootstrapped.json["ownerNamespace"], NS_A)
        self.assertEqual(
            bootstrapped.json["expiresAt"],
            1_000 + NATIVE_SYNC_BOOTSTRAP_TTL_SECONDS,
        )
        self.assertRegex(
            bootstrapped.json["nativeBootstrapToken"],
            r"^native-bootstrap-v1-[0-9]{1,12}-[a-f0-9]{64}$",
        )

        claim_body = self._owner_request(holder)
        claim_body["nativeBootstrapToken"] = (
            bootstrapped.json["nativeBootstrapToken"]
        )
        with mock.patch("reader_sync_relay.time.time", return_value=1_001):
            claimed = self.client.post(
                "/api/reader/sync/owner/claim",
                json=claim_body,
                headers={"X-Test-Account": "a"},
            )
        self.assertEqual(claimed.status_code, 200)
        self.assertEqual(claimed.json["ownerRole"], "native")
        self.assertRegex(
            claimed.json["ownerToken"],
            r"^owner-token-v1-[A-Za-z0-9_-]{24,256}$",
        )

        # The bootstrap is account-, registry-, family-, instance- and
        # device-bound. It cannot mint a native lease for another identity.
        other_account_body = {
            **claim_body,
            "ownerNamespace": NS_B,
        }
        with mock.patch("reader_sync_relay.time.time", return_value=1_001):
            cross_account = self.client.post(
                "/api/reader/sync/owner/claim",
                json=other_account_body,
                headers={"X-Test-Account": "b"},
            )
        self.assertEqual(cross_account.status_code, 403)
        self.assertEqual(
            cross_account.json["code"],
            "BW_SYNC_NATIVE_BOOTSTRAP",
        )

    def test_owner_lease_pwa_handoff_renew_release_and_generation(self):
        extension = self._lease_holder(
            account="a",
            namespace=NS_A,
            device_id="device-paired",
            family_label="paired-family",
            owner_role="extension",
            owner_instance_id="extension-worker-a",
        )
        pwa = {
            **extension,
            "ownerRole": "pwa",
            "ownerInstanceId": "pwa-window-a",
        }
        with mock.patch("reader_sync_relay.time.time", return_value=2_000):
            extension_claim = self._claim(extension)
            pwa_held = self._claim(pwa)
        self.assertEqual(extension_claim.status_code, 200)
        self.assertEqual(pwa_held.status_code, 409)
        self.assertEqual(pwa_held.json["code"], "BW_SYNC_OWNER_HELD")
        extension_credentials = {
            "ownerGeneration": extension_claim.json["ownerGeneration"],
            "ownerToken": extension_claim.json["ownerToken"],
        }

        # A PWA handoff request stops extension renewal, but does not invalidate
        # the already-issued token before its original expiry.
        with mock.patch("reader_sync_relay.time.time", return_value=2_001):
            still_active = self._post(
                "/api/reader/sync/exchange",
                {
                    **self._body(deviceId=extension["deviceId"]),
                    **{
                        key: extension[key]
                        for key in (
                            "deviceFamilyId",
                            "ownerRole",
                            "ownerInstanceId",
                        )
                    },
                    **extension_credentials,
                },
                add_lease=False,
            )
            renew_blocked = self.client.post(
                "/api/reader/sync/owner/renew",
                json=self._owner_request(
                    extension,
                    credentials=extension_credentials,
                ),
                headers={"X-Test-Account": "a"},
            )
        self.assertEqual(still_active.status_code, 200)
        self.assertEqual(renew_blocked.status_code, 409)
        self.assertEqual(
            renew_blocked.json["code"],
            "BW_SYNC_OWNER_INACTIVE",
        )

        with contextlib.closing(
            sqlite3.connect(self.root / NS_A / "relay.sqlite3")
        ) as connection:
            expires_at, handoff_role = connection.execute(
                "SELECT expires_at,handoff_role FROM sync_owner_leases "
                "WHERE device_family_id=?",
                (extension["deviceFamilyId"],),
            ).fetchone()
        self.assertEqual(expires_at, 2_000 + OWNER_LEASE_TTL_SECONDS)
        self.assertEqual(handoff_role, "pwa")

        release_body = self._owner_request(
            extension,
            credentials=extension_credentials,
        )
        with mock.patch("reader_sync_relay.time.time", return_value=2_002):
            released = self.client.post(
                "/api/reader/sync/owner/release",
                json=release_body,
                headers={"X-Test-Account": "a"},
            )
            replayed = self.client.post(
                "/api/reader/sync/owner/release",
                json=release_body,
                headers={"X-Test-Account": "a"},
            )
            pwa_claim = self._claim(pwa)
        self.assertEqual(released.status_code, 200)
        self.assertIs(released.json["released"], True)
        self.assertIs(released.json["replayed"], False)
        self.assertNotIn("ownerToken", released.json)
        self.assertEqual(replayed.status_code, 200)
        self.assertIs(replayed.json["replayed"], True)
        self.assertEqual(pwa_claim.status_code, 200)
        self.assertEqual(pwa_claim.json["ownerGeneration"], 2)

        # An old generation/token cannot release the new PWA owner, and an
        # extension cannot preempt an active PWA lease.
        with mock.patch("reader_sync_relay.time.time", return_value=2_003):
            stale_release = self.client.post(
                "/api/reader/sync/owner/release",
                json=release_body,
                headers={"X-Test-Account": "a"},
            )
            extension_held = self._claim(extension)
        self.assertEqual(stale_release.status_code, 409)
        self.assertEqual(
            stale_release.json["code"],
            "BW_SYNC_OWNER_INACTIVE",
        )
        self.assertEqual(extension_held.status_code, 409)
        self.assertEqual(extension_held.json["code"], "BW_SYNC_OWNER_HELD")

    def test_owner_lease_fences_all_business_routes_before_business_state(self):
        holder = self._lease_holder(
            account="a",
            namespace=NS_A,
            device_id="device-fenced",
            family_label="fenced-family",
            owner_instance_id="fenced-owner",
        )

        def body_for(path: str, credentials: dict | None = None):
            owner_fields = {
                key: holder[key]
                for key in (
                    "deviceFamilyId",
                    "ownerRole",
                    "ownerInstanceId",
                )
            }
            owner_fields.update(credentials or {})
            if path.endswith("/signal"):
                return {
                    **self._signal_body(deviceId=holder["deviceId"]),
                    **owner_fields,
                }
            return {
                **self._body(
                    deviceId=holder["deviceId"],
                    changes=(
                        [_change(7_001, mutation_id="must-not-write")]
                        if path.endswith("/exchange")
                        else []
                    ),
                ),
                **owner_fields,
            }

        paths = (
            "/api/reader/sync/exchange",
            "/api/reader/sync/snapshot",
            "/api/reader/sync/signal",
        )
        for path in paths:
            missing = self._post(
                path,
                (
                    self._signal_body(deviceId=holder["deviceId"])
                    if path.endswith("/signal")
                    else self._body(
                        deviceId=holder["deviceId"],
                        changes=(
                            [_change(7_001, mutation_id="missing-lease")]
                            if path.endswith("/exchange")
                            else []
                        ),
                    )
                ),
                add_lease=False,
            )
            self.assertEqual(missing.status_code, 409)
            self.assertEqual(
                missing.json["code"],
                "BW_SYNC_OWNER_INACTIVE",
            )

        with mock.patch("reader_sync_relay.time.time", return_value=3_000):
            lease = self._claim(holder)
        self.assertEqual(lease.status_code, 200)
        valid_credentials = {
            "ownerGeneration": lease.json["ownerGeneration"],
            "ownerToken": lease.json["ownerToken"],
        }
        wrong_credentials = {
            **valid_credentials,
            "ownerToken": "wrong-owner-token",
        }
        for path in paths:
            wrong = self._post(
                path,
                body_for(path, wrong_credentials),
                add_lease=False,
            )
            self.assertEqual(wrong.status_code, 409)
            self.assertEqual(
                wrong.json["code"],
                "BW_SYNC_OWNER_INACTIVE",
            )

        with mock.patch(
            "reader_sync_relay.time.time",
            return_value=3_000 + OWNER_LEASE_TTL_SECONDS + 1,
        ):
            for path in paths:
                expired = self._post(
                    path,
                    body_for(path, valid_credentials),
                    add_lease=False,
                )
                self.assertEqual(expired.status_code, 409)
                self.assertEqual(
                    expired.json["code"],
                    "BW_SYNC_OWNER_INACTIVE",
                )

            takeover_holder = {
                **holder,
                "ownerRole": "pwa",
                "ownerInstanceId": "fenced-pwa-owner",
            }
            takeover = self._claim(takeover_holder)
        self.assertEqual(takeover.status_code, 200)
        self.assertEqual(takeover.json["ownerGeneration"], 2)

        with contextlib.closing(
            sqlite3.connect(self.root / NS_A / "relay.sqlite3")
        ) as connection:
            counts = {
                table: connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in (
                    "relay_events",
                    "relay_mutations",
                    "relay_heads",
                    "relay_snapshots",
                    "relay_snapshot_items",
                    "direct_presence",
                    "direct_signals",
                    "direct_signal_dedupe",
                )
            }
            causal_contract = connection.execute(
                "SELECT causal_contract FROM relay_state WHERE singleton=1"
            ).fetchone()[0]
        self.assertEqual(counts, {table: 0 for table in counts})
        self.assertIsNone(causal_contract)

    def test_legacy_epoch_forces_reset_and_exposes_stable_proofless_snapshot(self):
        # Signal uses the same account database but intentionally does not
        # activate the sync-v3 causal epoch.
        self._post(
            "/api/reader/sync/signal",
            self._signal_body(),
        )
        legacy = _change(
            1,
            record_id="legacy-head",
            mutation_id="legacy-mutation",
            revision=7,
            causal=False,
        )
        public = {
            key: legacy[key]
            for key in ("mutationId", "operation", "collection", "record")
        }
        public["cursor"] = 1
        encoded = json.dumps(
            public,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        db_path = self.root / NS_A / "relay.sqlite3"
        with contextlib.closing(sqlite3.connect(db_path)) as connection:
            with connection:
                connection.execute(
                    "UPDATE relay_state SET current_cursor=1,"
                    "causal_contract=NULL,causal_start_cursor=NULL "
                    "WHERE singleton=1"
                )
                connection.execute(
                    "INSERT INTO relay_events("
                    "cursor,mutation_id,device_id,collection,record_id,"
                    "change_json,created_at"
                    ") VALUES(1,?,?,?,?,?,1)",
                    (
                        legacy["mutationId"],
                        "legacy-device",
                        legacy["collection"],
                        legacy["record"]["id"],
                        encoded,
                    ),
                )
                connection.execute(
                    "INSERT INTO relay_heads("
                    "collection,record_id,rev,deleted,change_json,cursor"
                    ") VALUES(?,?,?,?,?,1)",
                    (
                        legacy["collection"],
                        legacy["record"]["id"],
                        legacy["record"]["rev"],
                        0,
                        encoded,
                    ),
                )

        reset = self._post(
            "/api/reader/sync/exchange",
            self._body(direction="pull", cursor=0, changes=[]),
        ).json
        self.assertIs(reset["resetRequired"], True)
        self.assertEqual(reset["changes"], [])

        snapshot = self._post(
            "/api/reader/sync/snapshot",
            {
                "contract": CONTRACT,
                "ownerNamespace": NS_A,
                "deviceId": "device-test",
                "limit": 10,
            },
        ).json
        self.assertEqual(snapshot["snapshotCursor"], 1)
        self.assertEqual(snapshot["changes"][0]["record"]["id"], "legacy-head")
        self.assertNotIn("causal", snapshot["changes"][0]["record"])
        with contextlib.closing(sqlite3.connect(db_path)) as connection:
            row = connection.execute(
                "SELECT causal_contract,causal_start_cursor "
                "FROM relay_state WHERE singleton=1"
            ).fetchone()
        self.assertEqual(row, ("record-parent-state/1", 1))

    def test_missing_causal_is_preserved_conflict_and_ordered_children_succeed(self):
        missing = _change(
            1,
            record_id="missing-proof",
            mutation_id="missing-proof",
            causal=False,
        )
        rejected = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[missing]),
        ).json
        self.assertEqual(rejected["ackedMutationIds"], [])
        self.assertEqual(
            rejected["conflicts"][0]["reason"],
            "causal-proof-missing",
        )
        self.assertEqual(rejected["headCursor"], 0)

        base = _change(
            2,
            record_id="ordered",
            mutation_id="ordered-base",
            value={"id": "ordered", "state": "base"},
        )
        seeded = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[base]),
        ).json
        self.assertEqual(seeded["ackedMutationIds"], ["ordered-base"])
        child = _change(
            3,
            record_id="ordered",
            revision=1,
            mutation_id="ordered-child",
            value={"id": "ordered", "state": "child"},
            parent=_parent(base["record"]),
        )
        grandchild = _change(
            4,
            record_id="ordered",
            revision=1,
            mutation_id="ordered-grandchild",
            value={"id": "ordered", "state": "grandchild"},
            parent=_parent(child["record"]),
        )
        advanced = self._post(
            "/api/reader/sync/exchange",
            self._body(cursor=1, changes=[child, grandchild]),
        ).json
        self.assertEqual(
            advanced["ackedMutationIds"],
            ["ordered-child", "ordered-grandchild"],
        )
        snapshot = self._post(
            "/api/reader/sync/snapshot",
            {
                "contract": CONTRACT,
                "ownerNamespace": NS_A,
                "deviceId": "device-test",
                "limit": 10,
            },
        ).json
        head = next(
            item["record"]
            for item in snapshot["changes"]
            if item["record"]["id"] == "ordered"
        )
        self.assertEqual(head["value"]["state"], "grandchild")
        self.assertEqual(head["rev"], 3)

        branch = _change(
            5,
            record_id="ordered",
            revision=999,
            mutation_id="ordered-offline-branch",
            value={"id": "ordered", "state": "branch"},
            parent=_parent(base["record"]),
        )
        conflict = self._post(
            "/api/reader/sync/exchange",
            self._body(cursor=3, changes=[branch]),
        ).json
        self.assertEqual(conflict["ackedMutationIds"], [])
        self.assertEqual(
            conflict["conflicts"][0]["reason"],
            "causal-parent-mismatch",
        )

    def test_revision_overflow_is_explicit_and_does_not_change_head(self):
        maximum = _change(
            1,
            record_id="max-rev",
            revision=2**53 - 1,
            mutation_id="max-rev-head",
            value={"id": "max-rev", "state": "head"},
        )
        seeded = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[maximum]),
        ).json
        self.assertEqual(seeded["ackedMutationIds"], ["max-rev-head"])
        child = _change(
            2,
            record_id="max-rev",
            revision=1,
            mutation_id="max-rev-child",
            value={"id": "max-rev", "state": "child"},
            parent=_parent(maximum["record"]),
        )
        rejected = self._post(
            "/api/reader/sync/exchange",
            self._body(cursor=1, changes=[child]),
        ).json
        self.assertEqual(rejected["ackedMutationIds"], [])
        self.assertEqual(
            rejected["conflicts"][0]["reason"],
            "causal-revision-overflow",
        )
        self.assertEqual(rejected["headCursor"], 1)

    def test_push_is_durable_idempotent_and_mutation_reuse_is_explicit(self):
        change = _change(1)
        first = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[change]),
        ).json
        replay = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[change]),
        ).json
        self.assertEqual(first["ackedMutationIds"], ["mutation-1"])
        self.assertEqual(replay["ackedMutationIds"], ["mutation-1"])
        self.assertEqual(replay["headCursor"], 1)

        reused = _change(
            2,
            record_id="other-setting",
            mutation_id="mutation-1",
        )
        conflict = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[reused]),
        ).json
        self.assertEqual(conflict["ackedMutationIds"], [])
        self.assertEqual(conflict["conflicts"][0]["reason"], "mutation-id-reuse")
        self.assertEqual(conflict["headCursor"], 1)

        pull = self._post(
            "/api/reader/sync/exchange",
            self._body(direction="pull", changes=[]),
        ).json
        self.assertEqual([row["mutationId"] for row in pull["changes"]], ["mutation-1"])
        self.assertEqual(pull["changes"][0]["cursor"], 1)

    def test_revision_conflicts_noop_and_tombstone(self):
        base = _change(
            1,
            record_id="shared",
            revision=1,
            value={
                "id": "shared",
                "nested": {"alpha": 1, "beta": 2},
            },
        )
        self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[base]),
        )
        noop = _change(
            2,
            record_id="shared",
            revision=1,
            mutation_id="noop-2",
            value={
                "nested": {"beta": 2, "alpha": 1},
                "id": "shared",
            },
        )
        noop["record"]["updatedAt"] = base["record"]["updatedAt"] + 500
        noop["record"]["updatedBy"] = "other-device"
        response = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[noop]),
        ).json
        self.assertEqual(response["ackedMutationIds"], ["noop-2"])
        self.assertEqual(response["headCursor"], 1)

        same_rev = _change(
            3,
            record_id="shared",
            revision=1,
            mutation_id="same-rev",
            value={"id": "shared", "value": "different"},
        )
        conflict = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[same_rev]),
        ).json
        self.assertEqual(
            conflict["conflicts"][0]["reason"],
            "causal-parent-mismatch",
        )

        tombstone = _change(
            4,
            record_id="shared",
            revision=2,
            mutation_id="remove-shared",
            deleted=True,
            parent=_parent(base["record"]),
        )
        accepted = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[tombstone]),
        ).json
        self.assertEqual(accepted["ackedMutationIds"], ["remove-shared"])

        missing_parent_resurrection = _change(
            5,
            record_id="shared",
            revision=3,
            mutation_id="resurrect-shared-missing-parent",
            value={"id": "shared", "value": "missing-parent"},
            causal=False,
        )
        missing_rejected = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[missing_parent_resurrection]),
        ).json
        self.assertEqual(missing_rejected["ackedMutationIds"], [])
        self.assertEqual(
            missing_rejected["conflicts"][0]["reason"],
            "tombstone-dominates",
        )
        self.assertEqual(missing_rejected["headCursor"], 2)

        wrong_parent_resurrection = _change(
            6,
            record_id="shared",
            revision=3,
            mutation_id="resurrect-shared-wrong-parent",
            value={"id": "shared", "value": "wrong-parent"},
            parent=_parent(base["record"]),
        )
        wrong_parent_rejected = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[wrong_parent_resurrection]),
        ).json
        self.assertEqual(wrong_parent_rejected["ackedMutationIds"], [])
        self.assertEqual(
            wrong_parent_rejected["conflicts"][0]["reason"],
            "tombstone-dominates",
        )
        self.assertEqual(wrong_parent_rejected["headCursor"], 2)

        linear_resurrection = _change(
            7,
            record_id="shared",
            revision=3,
            mutation_id="resurrect-shared-linear",
            value={"id": "shared", "value": "returned-linearly"},
            parent=_parent(tombstone["record"]),
        )
        accepted_resurrection = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[linear_resurrection]),
        ).json
        self.assertEqual(
            accepted_resurrection["ackedMutationIds"],
            ["resurrect-shared-linear"],
        )
        self.assertEqual(accepted_resurrection["conflicts"], [])
        self.assertEqual(accepted_resurrection["headCursor"], 3)
        snapshot = self._post(
            "/api/reader/sync/snapshot",
            {
                "contract": CONTRACT,
                "ownerNamespace": NS_A,
                "deviceId": "device-test",
                "limit": 10,
            },
        ).json
        self.assertIs(snapshot["changes"][0]["record"]["deleted"], False)
        self.assertEqual(
            snapshot["changes"][0]["record"]["value"]["value"],
            "returned-linearly",
        )

    def test_semantic_equality_keeps_real_conflicts_and_advances_higher_revision(self):
        base = _change(
            10,
            record_id="semantic",
            revision=5,
            mutation_id="semantic-base",
            value={
                "id": "semantic",
                "nested": {"alpha": 1, "beta": 2},
                "tags": ["one", "two"],
            },
        )
        seeded = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[base]),
        ).json
        self.assertEqual(seeded["ackedMutationIds"], ["semantic-base"])

        equivalent = _change(
            11,
            record_id="semantic",
            revision=5,
            mutation_id="semantic-equivalent",
            value={
                "tags": ["one", "two"],
                "nested": {"beta": 2, "alpha": 1},
                "id": "semantic",
            },
        )
        equivalent["record"]["updatedAt"] = base["record"]["updatedAt"] + 999
        equivalent["record"]["updatedBy"] = "other-device"
        same = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[equivalent]),
        ).json
        self.assertEqual(same["ackedMutationIds"], ["semantic-equivalent"])
        self.assertEqual(same["conflicts"], [])
        self.assertEqual(same["headCursor"], 1)

        different = _change(
            12,
            record_id="semantic",
            revision=5,
            mutation_id="semantic-different",
            value={
                "id": "semantic",
                "nested": {"alpha": 1, "beta": 3},
                "tags": ["one", "two"],
            },
        )
        conflict = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[different]),
        ).json
        self.assertEqual(conflict["ackedMutationIds"], [])
        self.assertEqual(
            conflict["conflicts"][0]["reason"],
            "causal-parent-mismatch",
        )

        higher_same = _change(
            13,
            record_id="semantic",
            revision=6,
            mutation_id="semantic-higher",
            value=equivalent["record"]["value"],
        )
        advanced = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[higher_same]),
        ).json
        self.assertEqual(advanced["ackedMutationIds"], ["semantic-higher"])
        self.assertEqual(advanced["headCursor"], 2)

        tombstone = _change(
            14,
            record_id="semantic",
            revision=7,
            mutation_id="semantic-remove",
            deleted=True,
            value={"id": "semantic", "retained": "first-copy"},
            parent=_parent(higher_same["record"]),
        )
        removed = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[tombstone]),
        ).json
        self.assertEqual(removed["ackedMutationIds"], ["semantic-remove"])

        same_tombstone = _change(
            15,
            record_id="semantic",
            revision=7,
            mutation_id="semantic-remove-equivalent",
            deleted=True,
            value={"id": "semantic", "retained": "other-copy"},
        )
        same_tombstone["record"]["updatedBy"] = "other-device"
        tombstone_result = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[same_tombstone]),
        ).json
        self.assertEqual(
            tombstone_result["ackedMutationIds"],
            ["semantic-remove-equivalent"],
        )
        self.assertEqual(tombstone_result["conflicts"], [])
        self.assertEqual(tombstone_result["headCursor"], 3)

    def test_higher_revision_never_supersedes_an_unrelated_conflict(self):
        shared_head = _change(
            20,
            record_id="shared-supersede",
            revision=2,
            mutation_id="shared-head",
            value={"id": "shared-supersede", "state": "server"},
        )
        other_head = _change(
            21,
            record_id="real-conflict",
            revision=2,
            mutation_id="other-head",
            value={"id": "real-conflict", "state": "server"},
        )
        self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[shared_head, other_head]),
        )

        old_conflict = _change(
            22,
            record_id="shared-supersede",
            revision=1,
            mutation_id="old-conflict",
            value={"id": "shared-supersede", "state": "older-local"},
        )
        final_change = _change(
            23,
            record_id="shared-supersede",
            revision=3,
            mutation_id="final-change",
            value={"id": "shared-supersede", "state": "final-local"},
            parent=_parent(shared_head["record"]),
        )
        real_conflict = _change(
            24,
            record_id="real-conflict",
            revision=1,
            mutation_id="still-conflicted",
            value={"id": "real-conflict", "state": "different-local"},
        )
        result = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[old_conflict, final_change, real_conflict]),
        ).json
        self.assertEqual(
            result["ackedMutationIds"],
            ["final-change"],
        )
        self.assertEqual(
            [item["mutationId"] for item in result["conflicts"]],
            ["old-conflict", "still-conflicted"],
        )
        self.assertEqual(
            {item["reason"] for item in result["conflicts"]},
            {"causal-parent-mismatch"},
        )

        replay = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[old_conflict]),
        ).json
        self.assertEqual(replay["ackedMutationIds"], [])
        self.assertEqual(
            replay["conflicts"][0]["mutationId"],
            "old-conflict",
        )

        still_real = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[real_conflict]),
        ).json
        self.assertEqual(still_real["ackedMutationIds"], [])
        self.assertEqual(
            still_real["conflicts"][0]["mutationId"],
            "still-conflicted",
        )

    def test_remembered_conflict_resolves_only_when_final_business_state_matches(self):
        record_id = "remembered-cascade"
        server_head = _change(
            30,
            record_id=record_id,
            revision=3,
            mutation_id="cascade-server-head",
            value={"id": record_id, "state": "server-initial"},
        )
        self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[server_head]),
        )

        old_a = _change(
            31,
            record_id=record_id,
            revision=1,
            mutation_id="cascade-old-a",
            value={"id": record_id, "state": "old-a"},
        )
        old_b = _change(
            32,
            record_id=record_id,
            revision=2,
            mutation_id="cascade-old-b",
            value={"id": record_id, "state": "final-b"},
        )
        for change in (old_a, old_b):
            conflicted = self._post(
                "/api/reader/sync/exchange",
                self._body(changes=[change]),
            ).json
            self.assertEqual(conflicted["ackedMutationIds"], [])
            self.assertEqual(
                [item["mutationId"] for item in conflicted["conflicts"]],
                [change["mutationId"]],
            )

        final_head = _change(
            33,
            record_id=record_id,
            revision=4,
            mutation_id="cascade-final-head",
            value={"id": record_id, "state": "final-b"},
            parent=_parent(server_head["record"]),
        )
        advanced = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[final_head]),
        ).json
        self.assertEqual(
            advanced["ackedMutationIds"],
            ["cascade-final-head"],
        )

        replay = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[old_a, old_b]),
        ).json
        self.assertEqual(
            replay["ackedMutationIds"],
            ["cascade-old-b"],
        )
        self.assertEqual(
            [item["mutationId"] for item in replay["conflicts"]],
            ["cascade-old-a"],
        )

        replay_a = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[old_a]),
        ).json
        self.assertEqual(replay_a["ackedMutationIds"], [])
        self.assertEqual(
            replay_a["conflicts"][0]["mutationId"],
            "cascade-old-a",
        )

    def test_pull_pagination_advances_only_to_last_returned_cursor(self):
        for index in range(1, 6):
            response = self._post(
                "/api/reader/sync/exchange",
                self._body(changes=[_change(index)]),
            )
            self.assertEqual(response.status_code, 200)

        cursors = []
        cursor = 0
        for expected_more in (True, True, False):
            page = self._post(
                "/api/reader/sync/exchange",
                self._body(
                    direction="pull",
                    cursor=cursor,
                    limit=2,
                    changes=[],
                ),
            ).json
            cursors.extend(change["cursor"] for change in page["changes"])
            cursor = page["cursor"]
            self.assertIs(page["hasMore"], expected_more)
            self.assertEqual(page["headCursor"], 5)
        self.assertEqual(cursors, [1, 2, 3, 4, 5])
        self.assertEqual(cursor, 5)

    def test_snapshot_is_consistent_while_new_events_arrive(self):
        for index in range(1, 4):
            self._post(
                "/api/reader/sync/exchange",
                self._body(changes=[_change(index)]),
            )
        first = self._post(
            "/api/reader/sync/snapshot",
            {
                "contract": CONTRACT,
                "ownerNamespace": NS_A,
                "deviceId": "device-test",
                "limit": 2,
            },
        ).json
        self.assertEqual(first["snapshotCursor"], 3)
        self.assertIs(first["hasMore"], True)

        self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[_change(4)]),
        )
        second = self._post(
            "/api/reader/sync/snapshot",
            {
                "contract": CONTRACT,
                "ownerNamespace": NS_A,
                "deviceId": "device-test",
                "snapshotId": first["snapshotId"],
                "offset": first["nextOffset"],
                "limit": 2,
            },
        ).json
        combined = first["changes"] + second["changes"]
        self.assertEqual(len(combined), 3)
        self.assertNotIn("mutation-4", [item["mutationId"] for item in combined])
        self.assertEqual(second["snapshotCursor"], 3)

        tail = self._post(
            "/api/reader/sync/exchange",
            self._body(direction="pull", cursor=3, changes=[]),
        ).json
        self.assertEqual([item["mutationId"] for item in tail["changes"]], ["mutation-4"])

    def test_invalid_batch_is_rejected_before_any_change_is_written(self):
        bad = _change(2)
        bad["record"]["rev"] = 0
        response = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[_change(1), bad]),
        )
        self.assertEqual(response.status_code, 400)
        pull = self._post(
            "/api/reader/sync/exchange",
            self._body(direction="pull", changes=[]),
        ).json
        self.assertEqual(pull["headCursor"], 0)
        self.assertEqual(pull["changes"], [])

    def test_sync_v2_record_schema_rejects_malformed_envelopes_atomically(self):
        def missing(field):
            return lambda change: change["record"].pop(field)

        cases = [
            ("missing-schema", missing("schema"), "BW_SYNC_SCHEMA"),
            (
                "wrong-schema",
                lambda change: change["record"].__setitem__("schema", 2),
                "BW_SYNC_SCHEMA",
            ),
            ("missing-deleted", missing("deleted"), "BW_SYNC_INVALID"),
            (
                "non-boolean-deleted",
                lambda change: change["record"].__setitem__("deleted", 0),
                "BW_SYNC_INVALID",
            ),
            (
                "collection-mismatch",
                lambda change: change["record"].__setitem__(
                    "collection", "vocabulary-state"
                ),
                "BW_SYNC_INVALID",
            ),
            (
                "rev-zero",
                lambda change: change["record"].__setitem__("rev", 0),
                "BW_SYNC_INVALID",
            ),
            (
                "rev-fraction",
                lambda change: change["record"].__setitem__("rev", 1.5),
                "BW_SYNC_INVALID",
            ),
            (
                "rev-string",
                lambda change: change["record"].__setitem__("rev", "1"),
                "BW_SYNC_INVALID",
            ),
            ("missing-value", missing("value"), "BW_SYNC_INVALID"),
            (
                "invalid-json-value",
                lambda change: change["record"].__setitem__(
                    "value", {"payload": float("nan")}
                ),
                "BW_SYNC_INVALID",
            ),
            ("missing-updated-at", missing("updatedAt"), "BW_SYNC_INVALID"),
            (
                "updated-at-string",
                lambda change: change["record"].__setitem__(
                    "updatedAt", "1700000000000"
                ),
                "BW_SYNC_INVALID",
            ),
            (
                "updated-at-negative",
                lambda change: change["record"].__setitem__("updatedAt", -1),
                "BW_SYNC_INVALID",
            ),
            ("missing-updated-by", missing("updatedBy"), "BW_SYNC_INVALID"),
            (
                "updated-by-empty",
                lambda change: change["record"].__setitem__("updatedBy", ""),
                "BW_SYNC_INVALID",
            ),
            (
                "updated-by-non-string",
                lambda change: change["record"].__setitem__("updatedBy", 7),
                "BW_SYNC_INVALID",
            ),
            (
                "put-tombstone-mismatch",
                lambda change: change["record"].__setitem__("deleted", True),
                "BW_SYNC_INVALID",
            ),
            (
                "remove-active-mismatch",
                lambda change: change.__setitem__("operation", "remove"),
                "BW_SYNC_INVALID",
            ),
            (
                "unknown-operation",
                lambda change: change.__setitem__("operation", "merge"),
                "BW_SYNC_INVALID",
            ),
        ]
        for index, (label, mutate, expected_code) in enumerate(cases, 1):
            with self.subTest(label=label):
                invalid = _change(
                    1000 + index,
                    mutation_id=f"invalid-envelope-{index}",
                )
                mutate(invalid)
                response = self._post(
                    "/api/reader/sync/exchange",
                    self._body(changes=[_change(900 + index), invalid]),
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json["code"], expected_code)

        pull = self._post(
            "/api/reader/sync/exchange",
            self._body(direction="pull", changes=[]),
        ).json
        self.assertEqual(pull["headCursor"], 0)
        self.assertEqual(pull["changes"], [])

    def test_sync_v2_record_schema_preserves_unknown_json_extension_fields(self):
        extended = _change(
            2000,
            record_id="extended-record",
            mutation_id="extended-envelope",
        )
        extended["futureChangeField"] = {"version": 2}
        extended["record"]["futureRecordField"] = {"retained": True}
        pushed = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[extended]),
        )
        self.assertEqual(pushed.status_code, 200)
        self.assertEqual(pushed.json["ackedMutationIds"], ["extended-envelope"])

        pulled = self._post(
            "/api/reader/sync/exchange",
            self._body(direction="pull", changes=[]),
        ).json
        self.assertEqual(
            pulled["changes"][0]["record"]["futureRecordField"],
            {"retained": True},
        )
        self.assertNotIn(
            "futureChangeField",
            pulled["changes"][0],
            "relay 的公开 change envelope 仍只返回既有固定字段",
        )

    def test_journal_gap_fails_closed(self):
        for index in range(1, 4):
            self._post(
                "/api/reader/sync/exchange",
                self._body(changes=[_change(index)]),
            )
        db_path = self.root / NS_A / "relay.sqlite3"
        with contextlib.closing(sqlite3.connect(db_path)) as connection:
            with connection:
                connection.execute("DELETE FROM relay_events WHERE cursor=1")
        result = self._post(
            "/api/reader/sync/exchange",
            self._body(direction="pull", cursor=0, changes=[]),
        ).json
        self.assertIs(result["resetRequired"], True)
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["oldestCursor"], 2)

    def test_concurrent_push_assigns_unique_contiguous_cursors(self):
        shared_body, lease_error = self._attach_owner_lease(
            self._body(),
            account="a",
        )
        self.assertIsNone(lease_error)

        def push(index: int):
            with self.app.test_client() as client:
                return client.post(
                    "/api/reader/sync/exchange",
                    json={
                        **shared_body,
                        "changes": [_change(index)],
                    },
                    headers={"X-Test-Account": "a"},
                ).status_code

        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(push, range(1, 13)))
        self.assertEqual(statuses, [200] * 12)
        pulled = self._post(
            "/api/reader/sync/exchange",
            self._body(direction="pull", changes=[]),
        ).json
        self.assertEqual(
            sorted(item["cursor"] for item in pulled["changes"]),
            list(range(1, 13)),
        )
        self.assertEqual(pulled["headCursor"], 12)

    def test_response_is_private_and_never_echoes_owner_or_credentials(self):
        response = self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[_change(1)]),
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store, private")
        encoded = json.dumps(response.json)
        self.assertNotIn(NS_A, encoded)
        self.assertNotIn("user_id", encoded)
        lease = next(iter(self._leases.values()))
        self.assertNotIn(lease["ownerToken"], encoded)

    def test_account_proof_is_stable_only_within_registry_generation(self):
        with self.app.app_context():
            first = _account_proof(NS_A, REGISTRY_DIGEST)
            same_generation = _account_proof(NS_A, REGISTRY_DIGEST)
            next_generation = _account_proof(NS_A, OTHER_REGISTRY_DIGEST)
            other_account = _account_proof(NS_B, REGISTRY_DIGEST)

        self.assertRegex(first, r"^account-proof-v1-[a-f0-9]{64}$")
        self.assertEqual(first, same_generation)
        self.assertNotEqual(first, next_generation)
        self.assertNotEqual(first, other_account)
        self.assertNotIn(NS_A, first)
        self.assertNotIn(REGISTRY_DIGEST, first)

    def test_signal_requires_auth_and_returns_generation_scoped_account_proof(self):
        denied = self.client.post(
            "/api/reader/sync/signal",
            json=self._signal_body(),
        )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.json["contract"], SIGNAL_CONTRACT)
        self.assertEqual(denied.json["code"], "BW_SYNC_AUTH")
        self.assertNotIn("accountProof", denied.json)

        mismatch = self._post(
            "/api/reader/sync/signal",
            self._signal_body(namespace=NS_B),
        )
        self.assertEqual(mismatch.status_code, 403)
        self.assertEqual(mismatch.json["code"], "BW_SYNC_OWNER_MISMATCH")
        self.assertNotIn("accountProof", mismatch.json)

        first = self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-a"),
        )
        second = self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-b"),
        )
        other_account = self._post(
            "/api/reader/sync/signal",
            self._signal_body(namespace=NS_B, deviceId="device-c"),
            account="b",
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json["accountProof"], second.json["accountProof"])
        with self.app.app_context():
            self.assertEqual(
                first.json["accountProof"],
                _account_proof(NS_A, REGISTRY_DIGEST),
            )
        self.assertNotEqual(
            first.json["accountProof"],
            other_account.json["accountProof"],
        )
        encoded = json.dumps(first.json)
        self.assertNotIn(NS_A, encoded)
        self.assertNotIn("user_id", encoded)

    def test_signal_presence_requires_exact_registry_and_reports_server_baseline(self):
        digest = REGISTRY_DIGEST
        first = self._post(
            "/api/reader/sync/signal",
            self._signal_body(
                deviceId="device-a",
                registryDigest=digest,
            ),
        ).json
        self.assertIs(first["baselineReady"], True)
        self.assertEqual(first["baselineLocalCursor"], 0)
        self.assertEqual(first["headCursor"], 0)
        self.assertEqual(first["peers"], [])

        second = self._post(
            "/api/reader/sync/signal",
            self._signal_body(
                deviceId="device-b",
                registryDigest=digest,
                serverReady=False,
            ),
        ).json
        self.assertIs(second["baselineReady"], False)
        self.assertEqual(
            second["peers"],
            [{
                "deviceId": "device-a",
                "baselineReady": True,
                "baselineLocalCursor": 0,
            }],
        )
        isolated = self._post(
            "/api/reader/sync/signal",
            self._signal_body(
                deviceId="device-c",
                registryDigest=OTHER_REGISTRY_DIGEST,
            ),
        )
        self.assertEqual(isolated.status_code, 409)
        self.assertEqual(
            isolated.json["code"],
            "BW_SYNC_REGISTRY_MISMATCH",
        )
        self.assertNotIn("accountProof", isolated.json)

        seen = self._post(
            "/api/reader/sync/signal",
            self._signal_body(
                deviceId="device-a",
                registryDigest=digest,
            ),
        ).json
        self.assertEqual(
            seen["peers"],
            [{
                "deviceId": "device-b",
                "baselineReady": False,
                "baselineLocalCursor": None,
            }],
        )

        self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[_change(1)]),
        )
        stale = self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-a", serverCursor=0),
        ).json
        self.assertEqual(stale["headCursor"], 1)
        self.assertIs(stale["baselineReady"], False)
        ready_b = self._post(
            "/api/reader/sync/signal",
            self._signal_body(
                deviceId="device-b",
                serverCursor=1,
                localCursor=7,
            ),
        ).json
        self.assertIs(ready_b["baselineReady"], True)
        self.assertEqual(ready_b["baselineLocalCursor"], 7)
        ready_a = self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-a", serverCursor=1),
        ).json
        self.assertEqual(
            ready_a["peers"],
            [{
                "deviceId": "device-b",
                "baselineReady": True,
                "baselineLocalCursor": 7,
            }],
        )

    def test_signal_peer_presence_response_is_bounded(self):
        for index in range(MAX_SIGNAL_PEERS + 7):
            response = self._post(
                "/api/reader/sync/signal",
                self._signal_body(deviceId=f"device-peer-{index:03d}"),
            )
            self.assertEqual(response.status_code, 200)

        observer = self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-observer"),
        )
        self.assertEqual(observer.status_code, 200)
        self.assertEqual(len(observer.json["peers"]), MAX_SIGNAL_PEERS)
        self.assertEqual(
            [peer["deviceId"] for peer in observer.json["peers"]],
            sorted(peer["deviceId"] for peer in observer.json["peers"]),
        )

    def test_signal_exchange_is_incremental_and_outbound_ids_are_idempotent(self):
        self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-a"),
        )
        self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-b"),
        )
        offer = self._signal("offer-1", "device-b")
        sent = self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-a", signals=[offer]),
        ).json
        replay = self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-a", signals=[offer]),
        ).json
        self.assertEqual(sent["ackedSignalIds"], ["offer-1"])
        self.assertEqual(replay["ackedSignalIds"], ["offer-1"])

        received = self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-b", signalCursor=0),
        ).json
        self.assertEqual(len(received["signals"]), 1)
        self.assertEqual(
            received["signals"][0],
            {
                "id": 1,
                "fromDeviceId": "device-a",
                "sessionId": "session-1",
                "kind": "offer",
                "payload": {"type": "offer", "value": "offer-1"},
            },
        )
        self.assertEqual(received["signalCursor"], 1)
        empty = self._post(
            "/api/reader/sync/signal",
            self._signal_body(
                deviceId="device-b",
                signalCursor=received["signalCursor"],
            ),
        ).json
        self.assertEqual(empty["signals"], [])

        answer = self._signal(
            "answer-1",
            "device-a",
            kind="answer",
            payload={"type": "answer", "sdp": "opaque"},
        )
        self._post(
            "/api/reader/sync/signal",
            self._signal_body(
                deviceId="device-b",
                signalCursor=received["signalCursor"],
                signals=[answer],
            ),
        )
        answer_received = self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-a", signalCursor=0),
        ).json
        self.assertEqual(
            [item["kind"] for item in answer_received["signals"]],
            ["answer"],
        )
        self.assertEqual(answer_received["signals"][0]["id"], 2)

        changed = dict(offer)
        changed["payload"] = {"type": "offer", "value": "changed"}
        reused = self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-a", signals=[changed]),
        )
        self.assertEqual(reused.status_code, 409)
        self.assertEqual(reused.json["code"], "BW_DIRECT_SIGNAL_ID_REUSE")

    def test_live_signal_waits_for_sender_presence_instead_of_being_skipped(self):
        with mock.patch("reader_sync_relay.time.time", return_value=1_000):
            self._post(
                "/api/reader/sync/signal",
                self._signal_body(deviceId="device-a"),
            )
            self._post(
                "/api/reader/sync/signal",
                self._signal_body(deviceId="device-b"),
            )
            sent = self._post(
                "/api/reader/sync/signal",
                self._signal_body(
                    deviceId="device-a",
                    signals=[self._signal("presence-gap", "device-b")],
                ),
            )
            self.assertEqual(sent.status_code, 200)

        # Presence expires before the longer-lived mailbox signal. Receiver
        # must not jump its cursor past that offer.
        with mock.patch(
            "reader_sync_relay.time.time",
            return_value=1_000 + SIGNAL_PRESENCE_TTL_SECONDS + 1,
        ):
            waiting = self._post(
                "/api/reader/sync/signal",
                self._signal_body(deviceId="device-b", signalCursor=0),
            ).json
        self.assertEqual(waiting["signals"], [])
        self.assertEqual(waiting["signalCursor"], 0)

        with mock.patch(
            "reader_sync_relay.time.time",
            return_value=1_000 + SIGNAL_PRESENCE_TTL_SECONDS + 2,
        ):
            self._post(
                "/api/reader/sync/signal",
                self._signal_body(deviceId="device-a"),
            )
            received = self._post(
                "/api/reader/sync/signal",
                self._signal_body(deviceId="device-b", signalCursor=0),
            ).json
        self.assertEqual(
            [signal["kind"] for signal in received["signals"]],
            ["offer"],
        )
        self.assertEqual(received["signalCursor"], 1)

    def test_signal_rejects_self_unknown_cross_account_and_unready_targets(self):
        self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-a"),
        )
        self_signal = self._post(
            "/api/reader/sync/signal",
            self._signal_body(
                deviceId="device-a",
                signals=[self._signal("self-1", "device-a")],
            ),
        )
        self.assertEqual(self_signal.status_code, 400)
        self.assertEqual(self_signal.json["code"], "BW_DIRECT_SELF_SIGNAL")

        unknown = self._post(
            "/api/reader/sync/signal",
            self._signal_body(
                deviceId="device-a",
                signals=[self._signal("unknown-1", "device-missing")],
            ),
        )
        self.assertEqual(unknown.status_code, 409)
        self.assertEqual(unknown.json["code"], "BW_DIRECT_TARGET_UNAVAILABLE")

        self._post(
            "/api/reader/sync/signal",
            self._signal_body(
                deviceId="device-b",
                serverReady=False,
            ),
        )
        unready = self._post(
            "/api/reader/sync/signal",
            self._signal_body(
                deviceId="device-a",
                signals=[self._signal("unready-1", "device-b")],
            ),
        )
        self.assertEqual(unready.status_code, 409)
        self.assertEqual(unready.json["code"], "BW_DIRECT_TARGET_UNAVAILABLE")

        mismatched_registry = self._post(
            "/api/reader/sync/signal",
            self._signal_body(
                deviceId="device-other-registry",
                registryDigest=OTHER_REGISTRY_DIGEST,
            ),
        )
        self.assertEqual(mismatched_registry.status_code, 409)
        self.assertEqual(
            mismatched_registry.json["code"],
            "BW_SYNC_REGISTRY_MISMATCH",
        )
        wrong_registry = self._post(
            "/api/reader/sync/signal",
            self._signal_body(
                deviceId="device-a",
                signals=[
                    self._signal("registry-1", "device-other-registry"),
                ],
            ),
        )
        self.assertEqual(wrong_registry.status_code, 409)
        self.assertEqual(
            wrong_registry.json["code"],
            "BW_DIRECT_TARGET_UNAVAILABLE",
        )

        self._post(
            "/api/reader/sync/signal",
            self._signal_body(
                namespace=NS_B,
                deviceId="device-cross-account",
            ),
            account="b",
        )
        cross_account = self._post(
            "/api/reader/sync/signal",
            self._signal_body(
                deviceId="device-a",
                signals=[
                    self._signal("cross-1", "device-cross-account"),
                ],
            ),
        )
        self.assertEqual(cross_account.status_code, 409)
        self.assertEqual(
            cross_account.json["code"],
            "BW_DIRECT_TARGET_UNAVAILABLE",
        )

    def test_signal_requires_both_devices_at_current_ready_server_baseline(self):
        self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-a"),
        )
        self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-b"),
        )
        self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[_change(1)]),
        )
        stale_sender = self._post(
            "/api/reader/sync/signal",
            self._signal_body(
                deviceId="device-a",
                serverCursor=0,
                signals=[self._signal("stale-sender", "device-b")],
            ),
        )
        self.assertEqual(stale_sender.status_code, 409)
        self.assertEqual(
            stale_sender.json["code"],
            "BW_DIRECT_BASELINE_REQUIRED",
        )

        stale_target = self._post(
            "/api/reader/sync/signal",
            self._signal_body(
                deviceId="device-a",
                serverCursor=1,
                signals=[self._signal("stale-target", "device-b")],
            ),
        )
        self.assertEqual(stale_target.status_code, 409)
        self.assertEqual(
            stale_target.json["code"],
            "BW_DIRECT_TARGET_UNAVAILABLE",
        )
        self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-b", serverCursor=1),
        )
        accepted = self._post(
            "/api/reader/sync/signal",
            self._signal_body(
                deviceId="device-a",
                serverCursor=1,
                signals=[self._signal("ready-1", "device-b")],
            ),
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json["ackedSignalIds"], ["ready-1"])

    def test_signal_validation_and_ttl_fail_closed(self):
        missing_local_cursor = self._signal_body(deviceId="missing-local")
        del missing_local_cursor["localCursor"]
        response = self._post(
            "/api/reader/sync/signal",
            missing_local_cursor,
        )
        self.assertEqual(response.status_code, 400)

        not_ready_boolean = self._signal_body(deviceId="bad-ready")
        not_ready_boolean["serverReady"] = 1
        response = self._post(
            "/api/reader/sync/signal",
            not_ready_boolean,
        )
        self.assertEqual(response.status_code, 400)

        response = self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="future-cursor", serverCursor=1),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["code"], "BW_DIRECT_SERVER_CURSOR")

        self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-a"),
        )
        self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-b"),
        )
        not_object = self._signal("bad-type", "device-b")
        not_object["payload"] = "not-an-object"
        response = self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-a", signals=[not_object]),
        )
        self.assertEqual(response.status_code, 400)

        unknown_field = self._signal("bad-field", "device-b")
        unknown_field["unexpected"] = True
        response = self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-a", signals=[unknown_field]),
        )
        self.assertEqual(response.status_code, 400)

        too_large = self._signal(
            "too-large",
            "device-b",
            payload={"sdp": "x" * (MAX_SIGNAL_PAYLOAD_BYTES + 1)},
        )
        response = self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-a", signals=[too_large]),
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json["code"], "BW_DIRECT_SIGNAL_TOO_LARGE")

        response = self._post(
            "/api/reader/sync/signal",
            self._signal_body(
                deviceId="device-a",
                signals=[
                    self._signal(f"batch-{index}", "device-b")
                    for index in range(MAX_SIGNALS + 1)
                ],
            ),
        )
        self.assertEqual(response.status_code, 413)

        start = 1_700_000_000
        with mock.patch("reader_sync_relay.time.time", return_value=start):
            self._post(
                "/api/reader/sync/signal",
                self._signal_body(deviceId="ttl-a"),
            )
            self._post(
                "/api/reader/sync/signal",
                self._signal_body(deviceId="ttl-b"),
            )
            sent = self._post(
                "/api/reader/sync/signal",
                self._signal_body(
                    deviceId="ttl-a",
                    signals=[self._signal("ttl-1", "ttl-b")],
                ),
            )
            self.assertEqual(sent.status_code, 200)
        with mock.patch(
            "reader_sync_relay.time.time",
            return_value=start + SIGNAL_MESSAGE_TTL_SECONDS + 1,
        ):
            expired = self._post(
                "/api/reader/sync/signal",
                self._signal_body(deviceId="ttl-b", signalCursor=0),
            ).json
        self.assertEqual(expired["signals"], [])
        self.assertIs(expired["signalResetRequired"], True)
        self.assertEqual(expired["signalCursor"], 1)

        with mock.patch("reader_sync_relay.time.time", return_value=start):
            self._post(
                "/api/reader/sync/signal",
                self._signal_body(deviceId="presence-a"),
            )
            self._post(
                "/api/reader/sync/signal",
                self._signal_body(deviceId="presence-b"),
            )
        with mock.patch(
            "reader_sync_relay.time.time",
            return_value=start + SIGNAL_PRESENCE_TTL_SECONDS + 1,
        ):
            no_stale_peer = self._post(
                "/api/reader/sync/signal",
                self._signal_body(deviceId="presence-a"),
            ).json
        self.assertNotIn(
            "presence-b",
            [peer["deviceId"] for peer in no_stale_peer["peers"]],
        )

    def test_signal_bound_to_old_server_head_is_never_delivered_later(self):
        self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-a"),
        )
        self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-b"),
        )
        self._post(
            "/api/reader/sync/signal",
            self._signal_body(
                deviceId="device-a",
                signals=[self._signal("old-head", "device-b")],
            ),
        )
        self._post(
            "/api/reader/sync/exchange",
            self._body(changes=[_change(1)]),
        )
        self._post(
            "/api/reader/sync/signal",
            self._signal_body(deviceId="device-a", serverCursor=1),
        )
        caught_up = self._post(
            "/api/reader/sync/signal",
            self._signal_body(
                deviceId="device-b",
                serverCursor=1,
                signalCursor=0,
            ),
        ).json
        self.assertIs(caught_up["baselineReady"], True)
        self.assertEqual(caught_up["signals"], [])
        self.assertEqual(caught_up["signalCursor"], 1)


if __name__ == "__main__":
    unittest.main()
