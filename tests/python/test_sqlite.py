from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from common import load_exporter


class SQLiteRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exporter = load_exporter()

    def test_row_key_prefers_real_id(self):
        key = self.exporter.sqlite_row_key(
            "transactions",
            {"id": "tx-123", "date": "2026-09-01"},
            1,
        )
        self.assertEqual(key, "id:tx-123")

    def test_row_key_is_deterministic_without_natural_key(self):
        row = {"b": 2, "a": 1}
        first = self.exporter.sqlite_row_key(
            "custom_table",
            row,
            1,
        )
        second = self.exporter.sqlite_row_key(
            "custom_table",
            row,
            99,
        )
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("sha256:"))

    def test_upsert_updates_and_full_snapshot_removes_stale_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"

            # Importante no Windows:
            # sqlite3.Connection.__exit__ faz commit/rollback,
            # mas NÃO fecha a conexão. Usamos closing() para garantir
            # o fechamento antes de TemporaryDirectory tentar remover
            # test.db / test.db-wal / test.db-shm.
            with closing(
                self.exporter.sqlite_connect(db_path)
            ) as conn:
                self.exporter.upsert_sqlite_dataset(
                    conn,
                    "transactions",
                    [
                        {
                            "id": "tx-1",
                            "date": "2026-08-01",
                            "description": "Antiga",
                            "amount": -10.0,
                            "currencyCode": "BRL",
                        },
                        {
                            "id": "tx-stale",
                            "date": "2026-08-02",
                            "description": "Será removida",
                            "amount": -20.0,
                            "currencyCode": "BRL",
                        },
                    ],
                    sync_id="sync-1",
                    full_snapshot=True,
                )

                self.exporter.upsert_sqlite_dataset(
                    conn,
                    "transactions",
                    [
                        {
                            "id": "tx-1",
                            "date": "2026-08-01",
                            "description": "Atualizada",
                            "amount": -15.0,
                            "currencyCode": "BRL",
                        },
                        {
                            "id": "tx-2",
                            "date": "2026-08-03",
                            "description": "Nova",
                            "amount": -30.0,
                            "currencyCode": "BRL",
                        },
                    ],
                    sync_id="sync-2",
                    full_snapshot=True,
                )

                rows = conn.execute(
                    'SELECT id, description, amount, "_cf_raw_json" '
                    'FROM "transactions" ORDER BY id'
                ).fetchall()

                conn.commit()

            self.assertEqual(
                [row["id"] for row in rows],
                ["tx-1", "tx-2"],
            )
            self.assertEqual(
                rows[0]["description"],
                "Atualizada",
            )
            self.assertEqual(
                rows[0]["amount"],
                -15.0,
            )

            raw = json.loads(
                rows[0]["_cf_raw_json"]
            )
            self.assertEqual(
                raw["description"],
                "Atualizada",
            )

    def test_partial_snapshot_does_not_delete_unseen_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"

            with closing(
                self.exporter.sqlite_connect(db_path)
            ) as conn:
                self.exporter.upsert_sqlite_dataset(
                    conn,
                    "transactions",
                    [
                        {
                            "id": "tx-1",
                            "description": "A",
                        },
                        {
                            "id": "tx-2",
                            "description": "B",
                        },
                    ],
                    sync_id="sync-1",
                    full_snapshot=True,
                )

                self.exporter.upsert_sqlite_dataset(
                    conn,
                    "transactions",
                    [
                        {
                            "id": "tx-1",
                            "description": "A2",
                        }
                    ],
                    sync_id="sync-partial",
                    full_snapshot=False,
                )

                count = conn.execute(
                    'SELECT COUNT(*) FROM "transactions"'
                ).fetchone()[0]

                conn.commit()

            self.assertEqual(count, 2)

    def test_sqlite_indexes_are_created_for_known_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"

            with closing(
                self.exporter.sqlite_connect(db_path)
            ) as conn:
                self.exporter.upsert_sqlite_dataset(
                    conn,
                    "transactions",
                    [
                        {
                            "id": "tx-1",
                            "date": "2026-09-01",
                            "month": "2026-09",
                            "accountId": "acc-1",
                            "itemId": "item-1",
                            "currencyCode": "BRL",
                            "status": "POSTED",
                            "category": "Food",
                        }
                    ],
                    sync_id="sync-1",
                    full_snapshot=True,
                )

                self.exporter.ensure_sqlite_indexes(
                    conn
                )

                indexes = {
                    row[1]
                    for row in conn.execute(
                        'PRAGMA index_list("transactions")'
                    ).fetchall()
                }

                conn.commit()

            self.assertIn(
                "idx_transactions_id",
                indexes,
            )
            self.assertIn(
                "idx_transactions_date",
                indexes,
            )
            self.assertIn(
                "idx_transactions_accountId",
                indexes,
            )


if __name__ == "__main__":
    unittest.main()
