from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from controlefin_google_drive import (
    GoogleDriveManager,
    GOOGLE_DRIVE_FILE_SCOPE,
    GOOGLE_DRIVE_LEGACY_APPDATA_SCOPE,
    GOOGLE_DRIVE_SCOPES,
    CONTROLEFIN_DRIVE_FOLDER_NAME,
    escape_drive_query_literal,
    validate_google_oauth_client_config,
)


def valid_client_config():
    return {
        "installed": {
            "client_id": (
                "abc123.apps.googleusercontent.com"
            ),
            "client_secret": "desktop-secret",
            "auth_uri": (
                "https://accounts.google.com/o/oauth2/auth"
            ),
            "token_uri": (
                "https://oauth2.googleapis.com/token"
            ),
            "redirect_uris": [
                "http://localhost"
            ],
        }
    }


class GoogleDriveRegressionTests(unittest.TestCase):
    def test_only_desktop_oauth_client_is_accepted(self):
        validated = (
            validate_google_oauth_client_config(
                valid_client_config()
            )
        )
        self.assertIn(
            "installed",
            validated,
        )

        with self.assertRaisesRegex(
            ValueError,
            "Desktop app",
        ):
            validate_google_oauth_client_config(
                {
                    "web": {
                        "client_id": (
                            "abc.apps.googleusercontent.com"
                        )
                    }
                }
            )

    def test_drive_query_literal_is_escaped(self):
        self.assertEqual(
            escape_drive_query_literal(
                "O'Brien\\state"
            ),
            "O\\'Brien\\\\state",
        )

    def test_google_token_is_not_persisted_in_plain_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clear_value = {}

            def protect(value):
                clear_value["value"] = value
                return {
                    "scheme": "test",
                    "ciphertext": "ENCRYPTED",
                }

            def unprotect(record):
                self.assertEqual(
                    record["ciphertext"],
                    "ENCRYPTED",
                )
                return clear_value["value"]

            def writer(path, payload):
                path.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )
                path.write_text(
                    json.dumps(payload),
                    encoding="utf-8",
                )

            manager = GoogleDriveManager(
                client_config_file=(
                    root / "client.json"
                ),
                token_file=(
                    root / "token.json"
                ),
                protect_secret=protect,
                unprotect_secret=unprotect,
                atomic_write_json=writer,
            )

            token_json = json.dumps(
                {
                    "token": "ACCESS",
                    "refresh_token": (
                        "VERY-SENSITIVE-REFRESH"
                    ),
                    "client_id": "x",
                    "client_secret": "y",
                    "scopes": [
                        (
                            "https://www.googleapis.com/"
                            "auth/drive.file"
                        )
                    ],
                }
            )

            manager.save_token_json(
                token_json
            )

            raw = (
                manager.token_file
                .read_text(
                    encoding="utf-8"
                )
            )

            self.assertNotIn(
                "VERY-SENSITIVE-REFRESH",
                raw,
            )
            self.assertEqual(
                manager.load_token_json(),
                token_json,
            )

    def test_visible_drive_scope_is_primary_and_legacy_scope_is_migration_only(self):
        self.assertEqual(
            GOOGLE_DRIVE_FILE_SCOPE,
            "https://www.googleapis.com/auth/drive.file",
        )
        self.assertIn(
            GOOGLE_DRIVE_FILE_SCOPE,
            GOOGLE_DRIVE_SCOPES,
        )
        self.assertIn(
            GOOGLE_DRIVE_LEGACY_APPDATA_SCOPE,
            GOOGLE_DRIVE_SCOPES,
        )
        self.assertEqual(
            CONTROLEFIN_DRIVE_FOLDER_NAME,
            "ControleFin",
        )

    def test_old_appdata_only_token_requires_reauthorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clear = {}

            def protect(value):
                clear["value"] = value
                return {"scheme": "test", "ciphertext": "x"}

            def unprotect(record):
                return clear["value"]

            def writer(path, payload):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload), encoding="utf-8")

            manager = GoogleDriveManager(
                client_config_file=root / "client.json",
                token_file=root / "token.json",
                protect_secret=protect,
                unprotect_secret=unprotect,
                atomic_write_json=writer,
            )
            manager.save_client_config(valid_client_config(), clear_token=False)
            manager.save_token_json(
                json.dumps(
                    {
                        "token": "ACCESS",
                        "refresh_token": "REFRESH",
                        "client_id": "x",
                        "client_secret": "y",
                        "scopes": [GOOGLE_DRIVE_LEGACY_APPDATA_SCOPE],
                    }
                )
            )

            status = manager.status()
            self.assertFalse(status["connected"])
            self.assertTrue(status["requiresReauthorization"])
            self.assertEqual(status["space"], "drive")

    def test_status_exposes_non_blocking_oauth_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            manager = GoogleDriveManager(
                client_config_file=(
                    root / "client.json"
                ),
                token_file=(
                    root / "token.json"
                ),
                protect_secret=lambda value: {
                    "scheme": "test",
                    "ciphertext": value,
                },
                unprotect_secret=lambda record: (
                    record["ciphertext"]
                ),
                atomic_write_json=lambda path, payload: path.write_text(
                    json.dumps(payload),
                    encoding="utf-8",
                ),
            )

            manager._oauth_pending = {
                "status": "PENDING",
                "startedAt": "2026-09-20T12:00:00Z",
            }

            status = manager.status()

            self.assertTrue(
                status["oauthPending"]
            )
            self.assertEqual(
                status["oauthStartedAt"],
                "2026-09-20T12:00:00Z",
            )

    def test_account_identity_method_is_available(self):
        self.assertTrue(
            callable(
                getattr(
                    GoogleDriveManager,
                    "account_identity",
                    None,
                )
            )
        )

    def test_client_config_replacement_clears_old_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def writer(path, payload):
                path.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )
                path.write_text(
                    json.dumps(payload),
                    encoding="utf-8",
                )

            manager = GoogleDriveManager(
                client_config_file=(
                    root / "client.json"
                ),
                token_file=(
                    root / "token.json"
                ),
                protect_secret=lambda value: {
                    "scheme": "test",
                    "ciphertext": value,
                },
                unprotect_secret=lambda record: (
                    record["ciphertext"]
                ),
                atomic_write_json=writer,
            )

            manager.token_file.write_text(
                "{}",
                encoding="utf-8",
            )

            manager.save_client_config(
                valid_client_config()
            )

            self.assertFalse(
                manager.token_file.exists()
            )
            self.assertTrue(
                manager.client_config_file.exists()
            )


if __name__ == "__main__":
    unittest.main()
