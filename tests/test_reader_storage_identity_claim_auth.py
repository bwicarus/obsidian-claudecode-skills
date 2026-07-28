"""Request-owner and one-time legacy-sidecar claim authorization contracts."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReaderStorageIdentityClaimAuthorizationTest(unittest.TestCase):
    def test_identity_is_fail_closed_and_claim_requires_exact_sole_owner(self) -> None:
        script = textwrap.dedent(
            f"""
            import sys
            from pathlib import Path

            root = Path({str(ROOT)!r})
            sys.path[:0] = [str(root / "_server_deploy"), str(root / "scripts")]
            import app as module

            with module.app.app_context():
                db = module.get_db()
                db.execute(
                    "INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
                    ("sole-reader-owner", "x", "user"),
                )
                db.commit()
                uid_a = int(db.execute(
                    "SELECT id FROM users WHERE username=?",
                    ("sole-reader-owner",),
                ).fetchone()["id"])
                namespace_a = module._reader_storage_namespace(uid_a)
                db.execute(
                    "INSERT INTO api_tokens(user_id,token,label) VALUES(?,?,?)",
                    (uid_a, "sole-owner-token", "identity-test"),
                )
                db.commit()

            with module.app.test_request_context("/pdf/api/reading-pos"):
                module.session["user_id"] = uid_a
                identity_a = module._reader_authenticated_storage_identity()
                assert identity_a == {{
                    "user_id": uid_a,
                    "storage_namespace": namespace_a,
                }}
                assert (
                    module._reader_authenticated_storage_namespace()
                    == namespace_a
                )
                assert module._reader_legacy_sidecar_claim_authorized(identity_a)
                assert not module._reader_legacy_sidecar_claim_authorized({{
                    "user_id": uid_a + 1,
                    "storage_namespace": namespace_a,
                }})
                assert not module._reader_legacy_sidecar_claim_authorized({{
                    "user_id": uid_a,
                    "storage_namespace": "acct-v1-" + "f" * 64,
                }})
                assert not module._reader_legacy_sidecar_claim_authorized(None)

            # An explicit invalid Authorization header is authoritative.  It may
            # not fall back to the otherwise-valid browser session.
            with module.app.test_request_context(
                "/pdf/api/reading-pos",
                headers={{"Authorization": "Bearer invalid-token"}},
            ):
                module.session["user_id"] = uid_a
                assert module._reader_authenticated_storage_identity() is None
                assert module._reader_authenticated_storage_namespace() is None

            with module.app.app_context():
                db = module.get_db()
                db.execute(
                    "INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
                    ("second-reader-owner", "x", "user"),
                )
                db.commit()
                uid_b = int(db.execute(
                    "SELECT id FROM users WHERE username=?",
                    ("second-reader-owner",),
                ).fetchone()["id"])
                namespace_b = module._reader_storage_namespace(uid_b)
                db.execute(
                    "INSERT INTO api_tokens(user_id,token,label) VALUES(?,?,?)",
                    (uid_b, "second-owner-token", "identity-test"),
                )
                db.commit()

            # A valid token for another account must not override or borrow the
            # ambient session account.
            with module.app.test_request_context(
                "/pdf/api/reading-pos",
                headers={{"Authorization": "Bearer second-owner-token"}},
            ):
                module.session["user_id"] = uid_a
                assert module._reader_authenticated_storage_identity() is None
                assert module._reader_authenticated_storage_namespace() is None

            # The development-only inference is disabled as soon as there is
            # more than one account, for both otherwise-valid identities.
            with module.app.test_request_context("/pdf/api/reading-pos"):
                module.session["user_id"] = uid_a
                assert not module._reader_legacy_sidecar_claim_authorized({{
                    "user_id": uid_a,
                    "storage_namespace": namespace_a,
                }})
                assert not module._reader_legacy_sidecar_claim_authorized({{
                    "user_id": uid_b,
                    "storage_namespace": namespace_b,
                }})

            assert (
                module.app.extensions["reader_storage_identity_resolver"]
                is module._reader_authenticated_storage_identity
            )
            assert (
                module.app.extensions["reader_storage_namespace_resolver"]
                is module._reader_authenticated_storage_namespace
            )
            assert (
                module.app.extensions[
                    "reader_legacy_sidecar_claim_authorizer"
                ]
                is module._reader_legacy_sidecar_claim_authorized
            )
            """
        )
        with tempfile.TemporaryDirectory(
            prefix="bw-reader-storage-identity-test-"
        ) as data:
            env = os.environ.copy()
            env.update(
                SECRET_KEY="reader-storage-identity-test-secret",
                WEBAPP_DATA=data,
                PASSWORD_HASH="",
                SESSION_COOKIE_SECURE="0",
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(
            result.returncode,
            0,
            msg=(result.stdout + "\n" + result.stderr).strip(),
        )


if __name__ == "__main__":
    unittest.main()
