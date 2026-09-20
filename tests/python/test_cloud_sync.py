from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from controlefin_cloud_sync import CloudSyncManager


class FakeDrive:
    def __init__(self):
        self.files = []
        self.legacy_files = []
        self.payloads = {}
        self.next_id = 1
        self.index_payload = None

    def list_files(self, *, page_size=100):
        return [dict(item) for item in self.files]

    def create_bytes(
        self,
        *,
        name,
        data,
        mime_type,
        app_properties=None,
    ):
        file_id = f"file-{self.next_id}"
        self.next_id += 1
        item = {
            "id": file_id,
            "name": name,
            "mimeType": mime_type,
            "modifiedTime": f"2026-09-20T12:00:{self.next_id:02d}Z",
            "size": str(len(data)),
            "version": str(self.next_id),
            "appProperties": dict(app_properties or {}),
        }
        self.files.append(item)
        self.payloads[file_id] = bytes(data)
        return dict(item)

    def list_legacy_appdata_files(self, *, page_size=1000):
        return [dict(item) for item in self.legacy_files]

    def move_all_visible_to_legacy(self):
        self.legacy_files.extend(self.files)
        self.files = []

    def write_state_index(self, payload):
        self.index_payload = dict(payload)
        return {
            "id": "index",
            "name": "controlefin-state.json",
        }

    def download_bytes(self, *, file_id=None, name=None):
        if file_id:
            return self.payloads[file_id]
        matches = [item for item in self.files if item["name"] == name]
        if not matches:
            raise FileNotFoundError(name)
        return self.payloads[matches[-1]["id"]]

    def delete_file(self, file_id):
        self.files = [item for item in self.files if item["id"] != file_id]
        self.payloads.pop(file_id, None)


def create_valid_database(path: Path, marker: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE accounts (id TEXT, marker TEXT)")
        connection.execute(
            "CREATE TABLE transactions (id TEXT, description TEXT)"
        )
        connection.execute("CREATE TABLE dataset_catalog (tableName TEXT)")
        connection.execute("CREATE TABLE manifest (key TEXT, value TEXT)")
        connection.execute(
            "INSERT INTO accounts VALUES (?, ?)",
            ("a1", marker),
        )
        connection.execute(
            "INSERT INTO transactions VALUES (?, ?)",
            ("t1", f"SECRET-TX-{marker}"),
        )
        connection.commit()


def database_marker(path: Path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT marker FROM accounts LIMIT 1"
        ).fetchone()[0]


class CloudSyncTests(unittest.TestCase):
    def make_manager(
        self,
        root: Path,
        drive: FakeDrive,
        *,
        marker: str,
        pluggy_secret: str,
    ):
        db_file = root / "data" / "controlefin.db"
        settings_file = root / "user_data" / "ajustes.json"
        pluggy_file = root / "config" / "pluggy.json"

        create_valid_database(db_file, marker)

        settings = {
            "version": 9,
            "customCategories": [f"CAT-{marker}"],
            "customCategoryTranslations": [],
            "manualTransactions": [],
            "transactionOverrides": {},
            "categoryRules": [],
            "fixedExpenseRules": [],
            "fixedIncomeRules": [],
            "incomeSourceCategoryRules": [],
            "uiLanguage": "pt-BR",
        }
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps(settings), encoding="utf-8")

        pluggy_state = {
            "clientId": f"CLIENT-{marker}",
            "clientSecret": pluggy_secret,
            "itemIds": [f"ITEM-{marker}"],
        }
        pluggy_file.parent.mkdir(parents=True, exist_ok=True)
        pluggy_file.write_text("{}", encoding="utf-8")

        protected_clear = {}

        def protect(value):
            token = "enc-" + str(len(protected_clear))
            protected_clear[token] = str(value)
            return {"scheme": "test", "ciphertext": token}

        def unprotect(record):
            return protected_clear[record["ciphertext"]]

        def atomic_writer(path, payload):
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")

        def load_settings():
            return json.loads(settings_file.read_text(encoding="utf-8"))

        def normalize_settings(payload):
            return dict(payload)

        def save_settings(payload):
            atomic_writer(settings_file, payload)

        def load_pluggy_config(*, include_secret=False):
            result = {
                "version": 1,
                "clientId": pluggy_state["clientId"],
                "itemIds": list(pluggy_state["itemIds"]),
                "secretConfigured": True,
            }
            if include_secret:
                result["clientSecret"] = pluggy_state["clientSecret"]
            return result

        def save_pluggy_config(*, client_id, client_secret, item_ids):
            pluggy_state["clientId"] = client_id
            pluggy_state["clientSecret"] = client_secret
            pluggy_state["itemIds"] = list(item_ids)
            pluggy_file.write_text(
                json.dumps({"saved": True}),
                encoding="utf-8",
            )
            return {"clientId": client_id}

        manager = CloudSyncManager(
            google_drive=drive,
            db_file=db_file,
            settings_file=settings_file,
            pluggy_config_file=pluggy_file,
            key_file=root / "config" / "cloud_sync_key.json",
            state_file=root / "config" / "cloud_sync_state.json",
            protect_secret=protect,
            unprotect_secret=unprotect,
            atomic_write_json=atomic_writer,
            load_settings=load_settings,
            normalize_settings=normalize_settings,
            save_settings=save_settings,
            load_pluggy_config=load_pluggy_config,
            save_pluggy_config=save_pluggy_config,
        )
        return manager, db_file, settings_file, pluggy_state

    def test_snapshot_contains_no_plain_financial_or_pluggy_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            drive = FakeDrive()
            manager, _, _, _ = self.make_manager(
                root,
                drive,
                marker="ONE",
                pluggy_secret="SUPER-SECRET-PLUGGY",
            )
            manager.save_passphrase(
                "correct horse battery staple",
                verify_remote=False,
            )
            result = manager.upload()
            self.assertEqual(result["revision"], 1)

            encrypted = drive.payloads[result["fileId"]]
            self.assertNotIn(b"SECRET-TX-ONE", encrypted)
            self.assertNotIn(b"SUPER-SECRET-PLUGGY", encrypted)
            self.assertNotIn(b"CAT-ONE", encrypted)

    def test_second_device_downloads_database_settings_and_pluggy(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            drive = FakeDrive()

            first, _, _, _ = self.make_manager(
                base / "one",
                drive,
                marker="ONE",
                pluggy_secret="PLUGGY-ONE",
            )
            first.save_passphrase(
                "same cloud password 123",
                verify_remote=False,
            )
            first.upload()

            second, second_db, second_settings, second_pluggy = self.make_manager(
                base / "two",
                drive,
                marker="TWO",
                pluggy_secret="PLUGGY-TWO",
            )
            state = second.load_state()
            state["dirty"] = False
            second.save_state(state)
            second.save_passphrase(
                "same cloud password 123",
                verify_remote=True,
            )

            result = second.smart_sync()
            self.assertEqual(result["action"], "DOWNLOADED")
            self.assertEqual(database_marker(second_db), "ONE")

            restored_settings = json.loads(
                second_settings.read_text(encoding="utf-8")
            )
            self.assertEqual(
                restored_settings["customCategories"],
                ["CAT-ONE"],
            )
            self.assertEqual(second_pluggy["clientId"], "CLIENT-ONE")
            self.assertEqual(second_pluggy["clientSecret"], "PLUGGY-ONE")
            self.assertEqual(second_pluggy["itemIds"], ["ITEM-ONE"])

    def test_wrong_cloud_password_is_rejected_before_saving(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            drive = FakeDrive()
            first, _, _, _ = self.make_manager(
                base / "one",
                drive,
                marker="ONE",
                pluggy_secret="S1",
            )
            first.save_passphrase(
                "this is the correct password",
                verify_remote=False,
            )
            first.upload()

            second, _, _, _ = self.make_manager(
                base / "two",
                drive,
                marker="TWO",
                pluggy_secret="S2",
            )
            with self.assertRaisesRegex(ValueError, "senha da nuvem"):
                second.save_passphrase(
                    "this password is definitely wrong",
                    verify_remote=True,
                )
            self.assertFalse(second.key_file.exists())

    def test_force_upload_resolves_same_revision_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            drive = FakeDrive()
            manager, _, _, _ = self.make_manager(
                base / "one",
                drive,
                marker="ONE",
                pluggy_secret="S1",
            )
            manager.save_passphrase(
                "shared password for cloud",
                verify_remote=False,
            )
            first = manager.upload()

            original = drive.payloads[first["fileId"]]
            drive.create_bytes(
                name="controlefin-state-v1-r000000000001-branch.bin",
                data=original,
                mime_type="application/octet-stream",
                app_properties={
                    "kind": "controlefin_state",
                    "formatVersion": "1",
                    "revision": "1",
                    "deviceId": "other-device",
                    "createdAtUtc": "2026-09-20T12:01:00Z",
                },
            )

            manager.mark_dirty("settings")
            result = manager.upload(force=True)
            self.assertEqual(result["revision"], 2)


    def test_remote_newer_replaces_dirty_local_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            drive = FakeDrive()

            first, _, _, _ = self.make_manager(
                base / "one",
                drive,
                marker="REMOTE",
                pluggy_secret="REMOTE-SECRET",
            )
            first.save_passphrase(
                "shared password for newest wins",
                verify_remote=False,
            )
            first.upload(force=True)
            drive.files[-1]["modifiedTime"] = "2035-01-01T12:00:00Z"

            second, second_db, _, _ = self.make_manager(
                base / "two",
                drive,
                marker="LOCAL",
                pluggy_secret="LOCAL-SECRET",
            )
            second.save_passphrase(
                "shared password for newest wins",
                verify_remote=True,
            )

            state = second.load_state()
            state.update(
                {
                    "lastAppliedRevision": 1,
                    "lastRemoteFileId": "older-file",
                    "lastLocalSignature": "older-signature",
                    "dirty": True,
                }
            )
            second.save_state(state)

            old_epoch = 1_700_000_000
            for path in (
                second.db_file,
                second.settings_file,
                second.pluggy_config_file,
            ):
                if path.exists():
                    os.utime(path, (old_epoch, old_epoch))

            result = second.smart_sync()

            self.assertEqual(result["action"], "DOWNLOADED")
            self.assertEqual(result["decision"], "REMOTE_NEWER")
            self.assertEqual(database_marker(second_db), "REMOTE")

    def test_local_newer_uploads_over_older_drive_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            drive = FakeDrive()

            remote, _, _, _ = self.make_manager(
                base / "remote",
                drive,
                marker="REMOTE",
                pluggy_secret="REMOTE-SECRET",
            )
            remote.save_passphrase(
                "shared newest wins password",
                verify_remote=False,
            )
            remote.upload(force=True)
            drive.files[-1]["modifiedTime"] = "2020-01-01T12:00:00Z"

            local, _, _, _ = self.make_manager(
                base / "local",
                drive,
                marker="LOCAL-NEW",
                pluggy_secret="LOCAL-SECRET",
            )
            local.save_passphrase(
                "shared newest wins password",
                verify_remote=True,
            )

            state = local.load_state()
            state.update(
                {
                    "lastAppliedRevision": 1,
                    "lastRemoteFileId": "older-remote-id",
                    "lastLocalSignature": "older-signature",
                    "dirty": True,
                }
            )
            local.save_state(state)

            future_epoch = 2_000_000_000
            for path in (
                local.db_file,
                local.settings_file,
                local.pluggy_config_file,
            ):
                if path.exists():
                    os.utime(path, (future_epoch, future_epoch))

            result = local.smart_sync()

            self.assertEqual(result["action"], "UPLOADED")
            self.assertEqual(
                result["decision"],
                "LOCAL_NEWER_OR_EQUAL",
            )
            self.assertGreaterEqual(result["revision"], 2)

    def test_new_computer_prefers_existing_drive_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            drive = FakeDrive()

            first, _, _, _ = self.make_manager(
                base / "one",
                drive,
                marker="CLOUD",
                pluggy_secret="S1",
            )
            first.save_passphrase(
                "initial restore password",
                verify_remote=False,
            )
            first.upload(force=True)

            second, second_db, _, _ = self.make_manager(
                base / "two",
                drive,
                marker="LOCAL-BOOTSTRAP",
                pluggy_secret="S2",
            )
            second.save_passphrase(
                "initial restore password",
                verify_remote=True,
            )

            state = second.load_state()
            self.assertEqual(state["lastAppliedRevision"], 0)
            self.assertFalse(state["dirty"])

            result = second.smart_sync()

            self.assertEqual(result["action"], "DOWNLOADED")
            self.assertEqual(
                result["decision"],
                "INITIAL_REMOTE_RESTORE",
            )
            self.assertEqual(database_marker(second_db), "CLOUD")

    def test_upload_writes_visible_state_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            drive = FakeDrive()
            manager, _, _, _ = self.make_manager(
                Path(tmp),
                drive,
                marker="INDEX",
                pluggy_secret="S",
            )
            manager.save_passphrase(
                "index password 12345",
                verify_remote=False,
            )
            result = manager.upload(force=True)
            self.assertIsNotNone(drive.index_payload)
            self.assertEqual(
                drive.index_payload["latestRevision"],
                result["revision"],
            )
            self.assertIn("latestFileName", drive.index_payload)

    def test_legacy_appdata_snapshots_are_copied_to_visible_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            drive = FakeDrive()
            manager, _, _, _ = self.make_manager(
                Path(tmp),
                drive,
                marker="LEGACY",
                pluggy_secret="S",
            )
            manager.save_passphrase(
                "legacy migration password",
                verify_remote=False,
            )
            created = manager.upload(force=True)
            legacy_file_id = created["fileId"]
            drive.move_all_visible_to_legacy()

            snapshots = manager.remote_snapshots()

            self.assertEqual(len(snapshots), 1)
            self.assertNotEqual(snapshots[0]["id"], legacy_file_id)
            self.assertEqual(snapshots[0]["revision"], 1)
            self.assertEqual(len(drive.legacy_files), 1)
            self.assertEqual(len(drive.files), 1)
            self.assertIsNotNone(drive.index_payload)



if __name__ == "__main__":
    unittest.main()
