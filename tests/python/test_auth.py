from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from common import load_server


class AuthenticationRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = load_server()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)

        self.originals = {
            "STORAGE_DIR": self.server.STORAGE_DIR,
            "ROOT_DATA_DIR": self.server.ROOT_DATA_DIR,
            "ROOT_USER_DATA_DIR": self.server.ROOT_USER_DATA_DIR,
            "ROOT_CONFIG_DIR": self.server.ROOT_CONFIG_DIR,
            "AUTH_DIR": self.server.AUTH_DIR,
            "PROFILES_DIR": self.server.PROFILES_DIR,
            "LEGACY_AUTH_FILE": self.server.LEGACY_AUTH_FILE,
            "AUTH_FILE": self.server.AUTH_FILE,
            "AUTH_USERS_FILE": self.server.AUTH_USERS_FILE,
            "GOOGLE_OAUTH_CLIENT_FILE": self.server.GOOGLE_OAUTH_CLIENT_FILE,
        }

        self.server.STORAGE_DIR = root
        self.server.ROOT_DATA_DIR = root / "data"
        self.server.ROOT_USER_DATA_DIR = root / "user_data"
        self.server.ROOT_CONFIG_DIR = root / "config"
        self.server.AUTH_DIR = root / "auth"
        self.server.PROFILES_DIR = root / "profiles"
        self.server.LEGACY_AUTH_FILE = (
            self.server.ROOT_USER_DATA_DIR
            / "auth.json"
        )
        self.server.AUTH_FILE = (
            self.server.LEGACY_AUTH_FILE
        )
        self.server.AUTH_USERS_FILE = (
            self.server.AUTH_DIR
            / "users.json"
        )
        self.server.GOOGLE_OAUTH_CLIENT_FILE = (
            self.server.ROOT_CONFIG_DIR
            / "google_oauth_client.json"
        )

        for path in (
            self.server.ROOT_DATA_DIR,
            self.server.ROOT_USER_DATA_DIR,
            self.server.ROOT_CONFIG_DIR,
            self.server.AUTH_DIR,
            self.server.PROFILES_DIR,
        ):
            path.mkdir(
                parents=True,
                exist_ok=True,
            )

        with self.server.AUTH_SESSIONS_LOCK:
            self.server.AUTH_SESSIONS.clear()

        with self.server.LOGIN_FAILURE_LOCK:
            self.server.LOGIN_FAILURES.clear()

        with self.server.GOOGLE_PENDING_AUTH_LOCK:
            self.server.GOOGLE_PENDING_AUTH.clear()

        self.server.ACTIVE_PROFILE_ID = None
        self.server.ACTIVE_PROFILE_USERNAME = None

    def tearDown(self):
        for name, value in self.originals.items():
            setattr(
                self.server,
                name,
                value,
            )

        with self.server.AUTH_SESSIONS_LOCK:
            self.server.AUTH_SESSIONS.clear()

        with self.server.LOGIN_FAILURE_LOCK:
            self.server.LOGIN_FAILURES.clear()

        with self.server.GOOGLE_PENDING_AUTH_LOCK:
            self.server.GOOGLE_PENDING_AUTH.clear()

        self.server.ACTIVE_PROFILE_ID = None
        self.server.ACTIVE_PROFILE_USERNAME = None

        self.tmp.cleanup()

    def test_password_hash_never_stores_plain_password(self):
        config = self.server.build_auth_config(
            "math",
            "senha-super-segura",
        )

        raw = json.dumps(
            config
        )

        self.assertNotIn(
            "senha-super-segura",
            raw,
        )
        self.assertEqual(
            config["password"]["algorithm"],
            "pbkdf2_sha256",
        )
        self.assertGreaterEqual(
            config["password"]["iterations"],
            100_000,
        )

    def test_two_users_have_distinct_profiles_and_passwords(self):
        first = self.server.create_local_user(
            "alice",
            "senha-alice-123",
        )
        second = self.server.create_local_user(
            "bob",
            "senha-bob-456",
        )

        self.assertNotEqual(
            first["profileId"],
            second["profileId"],
        )

        self.assertTrue(
            self.server.verify_auth_password(
                first,
                "senha-alice-123",
            )
        )
        self.assertFalse(
            self.server.verify_auth_password(
                first,
                "senha-bob-456",
            )
        )

        first_paths = (
            self.server.ensure_profile_directories(
                first["profileId"]
            )
        )
        second_paths = (
            self.server.ensure_profile_directories(
                second["profileId"]
            )
        )

        self.assertNotEqual(
            first_paths["root"],
            second_paths["root"],
        )

    def test_duplicate_username_is_rejected_case_insensitively(self):
        self.server.create_local_user(
            "Alice",
            "senha-alice-123",
        )

        with self.assertRaisesRegex(
            ValueError,
            "Já existe",
        ):
            self.server.create_local_user(
                "alice",
                "outra-senha-456",
            )

    def test_session_contains_profile_id(self):
        user = self.server.create_local_user(
            "usuario",
            "uma-senha-forte",
        )

        token = self.server.create_auth_session(
            user["username"],
            user["profileId"],
        )

        identity = (
            self.server.auth_session_identity(
                token
            )
        )

        self.assertEqual(
            identity["username"],
            "usuario",
        )
        self.assertEqual(
            identity["profileId"],
            user["profileId"],
        )

    def test_new_user_revokes_sessions_from_other_profile(self):
        first = self.server.create_local_user(
            "alice",
            "senha-alice-123",
        )
        second = self.server.create_local_user(
            "bob",
            "senha-bob-456",
        )

        token_a = self.server.create_auth_session(
            "alice",
            first["profileId"],
        )
        token_b = self.server.create_auth_session(
            "bob",
            second["profileId"],
        )

        self.server.revoke_other_auth_sessions(
            second["profileId"]
        )

        self.assertIsNone(
            self.server.auth_session_identity(
                token_a
            )
        )
        self.assertIsNotNone(
            self.server.auth_session_identity(
                token_b
            )
        )

    def test_google_account_cannot_be_bound_to_two_local_users(self):
        first = self.server.create_local_user(
            "alice",
            "senha-alice-123",
        )
        second = self.server.create_local_user(
            "bob",
            "senha-bob-456",
        )

        account = {
            "displayName": "Alice",
            "emailAddress": "alice@example.com",
            "permissionId": "perm-123",
        }

        self.server.update_user_google_account(
            first["profileId"],
            account,
        )

        with self.assertRaisesRegex(
            ValueError,
            "já está associada",
        ):
            self.server.update_user_google_account(
                second["profileId"],
                account,
            )

    def test_legacy_single_user_is_migrated_into_profile(self):
        legacy = self.server.build_auth_config(
            "legado",
            "senha-legado-123",
        )

        self.server.LEGACY_AUTH_FILE.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        self.server.LEGACY_AUTH_FILE.write_text(
            json.dumps(
                legacy
            ),
            encoding="utf-8",
        )

        db = (
            self.server.ROOT_DATA_DIR
            / self.server.SQLITE_DATABASE_NAME
        )
        db.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        db.write_bytes(
            b"legacy-db"
        )

        self.assertTrue(
            self.server.migrate_single_user_auth_to_registry()
        )

        user = self.server.find_auth_user(
            "legado"
        )

        self.assertIsNotNone(
            user
        )

        profile_db = (
            self.server.profile_root(
                user["profileId"]
            )
            / "data"
            / self.server.SQLITE_DATABASE_NAME
        )

        self.assertTrue(
            profile_db.exists()
        )
        self.assertEqual(
            profile_db.read_bytes(),
            b"legacy-db",
        )

    def test_google_account_creates_profile_without_local_password_requirement(self):
        account = {
            "displayName": "Alice",
            "emailAddress": "alice@example.com",
            "permissionId": "google-permission-alice",
            "photoLink": "https://example.invalid/alice.jpg",
        }

        user, created = (
            self.server.create_google_user(
                account
            )
        )

        self.assertTrue(
            created
        )
        self.assertEqual(
            user["authMode"],
            "google",
        )
        self.assertFalse(
            self.server.user_local_password_enabled(
                user
            )
        )
        self.assertEqual(
            user["googleAccount"][
                "emailAddress"
            ],
            "alice@example.com",
        )

        same_user, created_again = (
            self.server.create_google_user(
                account
            )
        )

        self.assertFalse(
            created_again
        )
        self.assertEqual(
            same_user["profileId"],
            user["profileId"],
        )

    def test_different_google_accounts_get_different_profiles(self):
        first, _ = self.server.create_google_user(
            {
                "displayName": "Alice",
                "emailAddress": "alice@example.com",
                "permissionId": "perm-alice",
            }
        )
        second, _ = self.server.create_google_user(
            {
                "displayName": "Bob",
                "emailAddress": "bob@example.com",
                "permissionId": "perm-bob",
            }
        )

        self.assertNotEqual(
            first["profileId"],
            second["profileId"],
        )

    def test_optional_password_can_be_enabled_and_disabled(self):
        user, _ = self.server.create_google_user(
            {
                "displayName": "Alice",
                "emailAddress": "alice@example.com",
                "permissionId": "perm-alice",
            }
        )

        updated = (
            self.server.set_user_local_password(
                user["profileId"],
                "senha-adicional-123",
            )
        )

        self.assertTrue(
            self.server.user_local_password_enabled(
                updated
            )
        )
        self.assertTrue(
            self.server.verify_auth_password(
                updated,
                "senha-adicional-123",
            )
        )

        disabled = (
            self.server.disable_user_local_password(
                user["profileId"]
            )
        )

        self.assertFalse(
            self.server.user_local_password_enabled(
                disabled
            )
        )

    def test_google_pending_auth_preserves_new_profile_flag(self):
        user, _ = self.server.create_google_user(
            {
                "displayName": "Alice",
                "emailAddress": "alice@example.com",
                "permissionId": "perm-alice",
            }
        )

        token = (
            self.server.create_google_pending_auth(
                user,
                new_profile=True,
            )
        )

        pending = (
            self.server.google_pending_identity(
                token
            )
        )

        self.assertEqual(
            pending["profileId"],
            user["profileId"],
        )
        self.assertTrue(
            pending["newProfile"]
        )

        consumed = (
            self.server.consume_google_pending_auth(
                token
            )
        )

        self.assertTrue(
            consumed["newProfile"]
        )
        self.assertIsNone(
            self.server.google_pending_identity(
                token
            )
        )

    def test_expired_session_is_rejected(self):
        user = self.server.create_local_user(
            "usuario",
            "uma-senha-forte",
        )

        token = self.server.create_auth_session(
            "usuario",
            user["profileId"],
        )

        with self.server.AUTH_SESSIONS_LOCK:
            self.server.AUTH_SESSIONS[
                token
            ]["expiresAt"] = (
                time.time()
                - 1
            )

        self.assertIsNone(
            self.server.auth_session_identity(
                token
            )
        )

    def test_username_and_password_policy(self):
        with self.assertRaises(ValueError):
            self.server.build_auth_config(
                "ab",
                "12345678",
            )

        with self.assertRaises(ValueError):
            self.server.build_auth_config(
                "usuario",
                "1234567",
            )


if __name__ == "__main__":
    unittest.main()
