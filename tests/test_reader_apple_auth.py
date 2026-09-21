import hashlib
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask
from werkzeug.security import generate_password_hash

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '_server_deploy'))
import reader_apple_auth as auth


class ReaderAppleAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password_hash TEXT,
          role TEXT DEFAULT 'user',storage_namespace TEXT);
          CREATE TABLE invites(token TEXT PRIMARY KEY,used_by INTEGER,used_at TEXT,expires_at TEXT);''')
        self.db.executescript(auth.SCHEMA)
        self.db.execute('INSERT INTO users VALUES(1,?,?,?,?)', ('original', generate_password_hash('correct'), 'admin', 'original-vault'))
        self.db.execute('INSERT INTO users VALUES(2,?,?,?,?)', ('other', generate_password_hash('different'), 'user', 'other-vault'))
        self.db.commit()
        self.addCleanup(self.db.close)
        app = Flask(__name__)
        app.secret_key = 'isolated-test-session'
        app.testing = True
        auth.register_reader_apple_auth(app, lambda: self.db, lambda name: Path(self.temp.name) / name)
        self.app = app
        self.client = app.test_client()
        self.keys = patch.object(auth, '_keys', SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=self.key.public_key())))
        self.keys.start()
        self.addCleanup(self.keys.stop)

    def challenge(self, client=None):
        response = (client or self.client).post('/login/apple/challenge', json={})
        self.assertEqual(response.status_code, 200)
        return response.json

    def token(self, challenge, subject='apple-original', **overrides):
        claims = dict(iss=auth.APPLE_ISSUER, aud=auth.APPLE_AUDIENCE, exp=int(time.time())+300,
                      iat=int(time.time()), sub=subject, nonce=hashlib.sha256(challenge['nonce'].encode()).hexdigest())
        claims.update(overrides)
        return jwt.encode(claims, self.key, algorithm='RS256', headers={'kid':'local-only'})

    def complete(self, challenge, client=None, **overrides):
        return (client or self.client).post('/login/apple/complete', json=dict(state=challenge['state'], identity_token=self.token(challenge, **overrides)))

    def test_link_preserves_existing_account_and_next_apple_login_reuses_it(self):
        challenge = self.challenge()
        response = self.complete(challenge)
        self.assertTrue(response.json['link_required'])
        ticket = response.json['ticket']
        self.assertIsNone(self.db.execute('SELECT * FROM reader_apple_identities').fetchone())
        wrong = self.client.post('/login/apple/link', json=dict(ticket=ticket, username='original', password='wrong'))
        self.assertEqual(wrong.status_code, 401)
        linked = self.client.post('/login/apple/link', json=dict(ticket=ticket, username='original', password='correct'))
        self.assertEqual(linked.json, dict(ok=True, username='original'))
        with self.client.session_transaction() as session:
            self.assertEqual(session['user_id'], 1)
            self.assertEqual(session['role'], 'admin')
            session.clear()
        self.assertEqual(self.db.execute('SELECT storage_namespace FROM users WHERE id=1').fetchone()[0], 'original-vault')
        again = self.complete(self.challenge())
        self.assertEqual(again.json, dict(ok=True, username='original'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM users').fetchone()[0], 2)

    def test_nonce_signature_audience_expiry_and_browser_flow_are_checked(self):
        challenge = self.challenge()
        for override in [dict(nonce='wrong'), dict(aud='another.app'), dict(iss='https://attacker.test'), dict(exp=int(time.time())-90)]:
            self.assertEqual(self.complete(challenge, **override).status_code, 401)
        impostor = self.app.test_client()
        self.assertEqual(self.complete(challenge, client=impostor).status_code, 400)
        self.assertTrue(self.complete(challenge).json['link_required'])
        self.assertEqual(self.complete(challenge).status_code, 400)
        forged = jwt.encode(dict(iss=auth.APPLE_ISSUER, aud=auth.APPLE_AUDIENCE, exp=int(time.time())+300,
                                iat=int(time.time()), sub='forged', nonce='bad'), 'not-an-apple-key-with-32-characters', algorithm='HS256')
        fresh = self.challenge()
        self.assertEqual(self.client.post('/login/apple/complete', json=dict(state=fresh['state'],identity_token=forged)).status_code,401)

    def test_authenticated_binding_cannot_replace_another_accounts_identity(self):
        with self.client.session_transaction() as session: session['user_id'] = 1
        self.assertEqual(self.complete(self.challenge()).status_code, 200)
        with self.client.session_transaction() as session: session['user_id'] = 2
        denied = self.complete(self.challenge())
        self.assertEqual(denied.status_code,409)
        self.assertEqual(self.db.execute('SELECT user_id FROM reader_apple_identities').fetchone()[0],1)

    def test_new_account_requires_single_use_invitation_and_link_attempts_are_bounded(self):
        ticket = self.complete(self.challenge()).json['ticket']
        body = dict(ticket=ticket,username='new-reader',invite='missing')
        self.assertEqual(self.client.post('/login/apple/link',json=body).status_code,400)
        self.db.execute("INSERT INTO invites(token) VALUES('invited-once')")
        self.db.commit()
        body['invite']='invited-once'
        self.assertEqual(self.client.post('/login/apple/link',json=body).status_code,200)
        self.assertIsNotNone(self.db.execute("SELECT used_by FROM invites WHERE token='invited-once'").fetchone()[0])
        with self.client.session_transaction() as session: session.clear()
        ticket = self.complete(self.challenge(),subject='another-apple').json['ticket']
        bad = dict(ticket=ticket,username='original',password='wrong')
        for _ in range(6): self.assertEqual(self.client.post('/login/apple/link',json=bad).status_code,401)
        bad['password']='correct'
        self.assertEqual(self.client.post('/login/apple/link',json=bad).status_code,401)

    def test_cross_origin_request_and_changed_account_do_not_bind(self):
        denied = self.client.post('/login/apple/challenge',json={},headers={'Origin':'https://attacker.test'})
        self.assertEqual(denied.status_code,400)
        challenge = self.challenge()
        with self.client.session_transaction() as session: session['user_id']=1
        self.assertEqual(self.complete(challenge).status_code,409)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM reader_apple_identities').fetchone()[0],0)


if __name__ == '__main__':
    unittest.main()
