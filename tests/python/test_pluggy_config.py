from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common import load_server


class PluggyLocalConfigRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = load_server()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)

        self.original_config_dir = self.server.CONFIG_DIR
        self.original_config_file = self.server.PLUGGY_CONFIG_FILE

        self.server.CONFIG_DIR = root / "config"
        self.server.PLUGGY_CONFIG_FILE = (
            self.server.CONFIG_DIR
            / "pluggy.json"
        )
        self.server.CONFIG_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

    def tearDown(self):
        self.server.CONFIG_DIR = self.original_config_dir
        self.server.PLUGGY_CONFIG_FILE = self.original_config_file
        self.tmp.cleanup()

    def test_item_ids_are_trimmed_deduplicated_and_ordered(self):
        result = self.server.normalize_item_ids(
            " item-a\nitem-b, item-a ; item-c "
        )

        self.assertEqual(
            result,
            [
                "item-a",
                "item-b",
                "item-c",
            ],
        )

    def test_saved_config_never_contains_plain_secret(self):
        def fake_protect(value):
            return {
                "scheme": "windows-dpapi-user",
                "ciphertext": "ENCRYPTED",
            }

        with patch.object(
            self.server,
            "protect_local_secret",
            side_effect=fake_protect,
        ):
            saved = self.server.save_pluggy_config(
                client_id="client-123",
                client_secret="super-secret",
                item_ids=["item-1"],
            )

        raw = self.server.PLUGGY_CONFIG_FILE.read_text(
            encoding="utf-8"
        )

        self.assertNotIn(
            "super-secret",
            raw,
        )
        self.assertEqual(
            saved["clientId"],
            "client-123",
        )

        payload = json.loads(raw)
        self.assertEqual(
            payload["clientSecret"]["scheme"],
            "windows-dpapi-user",
        )

    def test_existing_secret_is_preserved_when_update_omits_secret(self):
        self.server.atomic_write_json(
            self.server.PLUGGY_CONFIG_FILE,
            {
                "version": 1,
                "clientId": "old",
                "clientSecret": {
                    "scheme": "windows-dpapi-user",
                    "ciphertext": "KEEP",
                },
                "itemIds": ["old-item"],
                "updatedAt": "x",
            },
        )

        with patch.object(
            self.server,
            "protect_local_secret",
        ) as protect:
            saved = self.server.save_pluggy_config(
                client_id="new-client",
                client_secret="",
                item_ids=["item-2"],
            )

        protect.assert_not_called()

        payload = json.loads(
            self.server.PLUGGY_CONFIG_FILE.read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(
            payload["clientSecret"]["ciphertext"],
            "KEEP",
        )
        self.assertEqual(
            saved["itemIds"],
            ["item-2"],
        )

    def test_empty_item_list_is_rejected(self):
        with self.assertRaises(ValueError):
            self.server.save_pluggy_config(
                client_id="client",
                client_secret="secret",
                item_ids=[],
            )


if __name__ == "__main__":
    unittest.main()
