import tempfile
import unittest

from auth import AuthStore


class SelectionOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1000.0
        self.auth = AuthStore(self.directory.name, clock=lambda: self.now)

    def login(self, subject, device):
        self.now += 1
        return self.auth.apple_login(subject, device, device)

    def pair(self, device):
        self.now += 1
        code = self.auth.create_pairing_code()["code"]
        return self.auth.pair(code, device, device)

    def owner(self, result):
        return self.auth.authenticate(result["token"])["ownerId"]

    def test_one_apple_account_shares_library_between_devices(self):
        first = self.login("apple-user-one", "ipad")
        second = self.login("apple-user-one", "iphone")
        other = self.login("apple-user-two", "family-ipad")
        self.assertEqual(self.owner(first), self.owner(second))
        self.assertNotEqual(self.owner(first), self.owner(other))

    def test_pairing_never_inherits_an_existing_apple_account(self):
        apple = self.login("apple-user-one", "ipad")
        paired = self.pair("review-device")
        self.assertNotEqual(self.owner(apple), self.owner(paired))
        reopened = AuthStore(self.directory.name, clock=lambda: self.now)
        self.assertEqual(self.owner(paired), reopened.authenticate(paired["token"])["ownerId"])

    def test_legacy_links_only_exact_unique_account_login_records(self):
        paired = self.pair("older-paired-device")
        first = self.login("apple-user-one", "ipad")
        last = self.login("apple-user-one", "iphone")
        expected = self.owner(first)
        with self.auth._connect() as connection:
            connection.execute("UPDATE device_tokens SET owner_id=NULL")
        migrated = AuthStore(self.directory.name, clock=lambda: self.now)
        self.assertEqual(migrated.authenticate(first["token"])["ownerId"], expected)
        self.assertEqual(migrated.authenticate(last["token"])["ownerId"], expected)
        self.assertNotEqual(migrated.authenticate(paired["token"])["ownerId"], expected)

    def test_ambiguous_legacy_records_stay_device_scoped(self):
        first = self.auth.apple_login("apple-user-one", "ipad", "iPad")
        self.auth.apple_login("apple-user-two", "iphone", "iPhone")
        with self.auth._connect() as connection:
            connection.execute("UPDATE device_tokens SET owner_id=NULL")
        migrated = AuthStore(self.directory.name, clock=lambda: self.now)
        self.assertTrue(migrated.authenticate(first["token"])["ownerId"].startswith("device:"))

    def test_library_adoption_requires_valid_matching_device_token(self):
        paired = self.pair("ipad")
        previous_owner = self.owner(paired)
        self.now += 1
        linked = self.auth.apple_login("apple-user-one", "ipad", "iPad", paired["token"])
        self.assertEqual(linked["previousOwnerId"], previous_owner)
        with self.assertRaises(ValueError):
            self.auth.authenticate(paired['token'])
        mismatch = self.auth.apple_login("apple-user-one", "other-ipad", "iPad", paired["token"])
        self.assertNotIn("previousOwnerId", mismatch)
        another_account = self.auth.apple_login("apple-user-two", "ipad", "iPad", linked["token"])
        self.assertNotIn("previousOwnerId", another_account)
        with self.assertRaises(ValueError):
            self.auth.authenticate(linked['token'])


if __name__ == "__main__":
    unittest.main()
