from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch
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

    def test_purge_legacy_csv_files_removes_only_top_level_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "nested"
            nested.mkdir()

            (root / "transactions.csv").write_text(
                "id\n1\n",
                encoding="utf-8",
            )
            (root / "manifest.csv").write_text(
                "key,value\nx,y\n",
                encoding="utf-8",
            )
            (root / "controlefin.db").write_bytes(
                b"db-placeholder"
            )
            (nested / "keep.csv").write_text(
                "x\n1\n",
                encoding="utf-8",
            )

            removed = self.exporter.purge_legacy_csv_files(
                root
            )

            self.assertEqual(
                sorted(path.name for path in removed),
                ["manifest.csv", "transactions.csv"],
            )
            self.assertFalse(
                (root / "transactions.csv").exists()
            )
            self.assertFalse(
                (root / "manifest.csv").exists()
            )
            self.assertTrue(
                (root / "controlefin.db").exists()
            )
            self.assertTrue(
                (nested / "keep.csv").exists()
            )

    def test_fsync_file_accepts_regular_sqlite_file(self):
        """
        Regressão Windows: fsync em handle somente leitura pode falhar.

        O helper de publicação atômica precisa conseguir sincronizar um
        arquivo normal antes do os.replace().
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "staging.db"
            path.write_bytes(b"controlefin-atomic-test")

            self.exporter.fsync_file(path)

            self.assertEqual(
                path.read_bytes(),
                b"controlefin-atomic-test",
            )

    def test_atomic_export_replaces_database_only_after_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            database_path = output_dir / self.exporter.SQLITE_DATABASE_NAME

            first = self.exporter.ExtractedData()
            first.tables["transactions"] = [
                {
                    "id": "old-tx",
                    "date": "2026-08-01",
                    "accountId": "acc-1",
                    "currencyCode": "BRL",
                    "status": "POSTED",
                }
            ]
            first.tables["accounts"] = [
                {
                    "id": "acc-1",
                    "type": "BANK",
                    "currencyCode": "BRL",
                }
            ]

            self.exporter.export_sqlite_bundle_atomic(
                first,
                output_dir,
                generated_at="2026-09-01T00:00:00Z",
                sync_id="atomic-first",
                full_snapshot=True,
            )

            second = self.exporter.ExtractedData()
            second.tables["transactions"] = [
                {
                    "id": "new-tx",
                    "date": "2026-09-01",
                    "accountId": "acc-1",
                    "currencyCode": "BRL",
                    "status": "POSTED",
                }
            ]
            second.tables["accounts"] = [
                {
                    "id": "acc-1",
                    "type": "BANK",
                    "currencyCode": "BRL",
                }
            ]

            result = self.exporter.export_sqlite_bundle_atomic(
                second,
                output_dir,
                generated_at="2026-09-02T00:00:00Z",
                sync_id="atomic-second",
                full_snapshot=True,
            )

            self.assertEqual(result, database_path)

            with closing(sqlite3.connect(database_path)) as conn:
                ids = [
                    row[0]
                    for row in conn.execute(
                        'SELECT id FROM "transactions" ORDER BY id'
                    ).fetchall()
                ]
                latest_sync = conn.execute(
                    'SELECT syncId, status FROM "_cf_sync_runs" '
                    'ORDER BY startedAtUtc DESC LIMIT 1'
                ).fetchone()

            self.assertEqual(ids, ["new-tx"])
            self.assertEqual(latest_sync, ("atomic-second", "SUCCESS"))

            staging = list(
                output_dir.glob(".*.staging*")
            )
            self.assertEqual(staging, [])

    def test_atomic_export_preserves_old_database_if_staging_validation_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            database_path = output_dir / self.exporter.SQLITE_DATABASE_NAME

            original = self.exporter.ExtractedData()
            original.tables["transactions"] = [
                {
                    "id": "keep-me",
                    "date": "2026-08-01",
                    "accountId": "acc-1",
                    "currencyCode": "BRL",
                    "status": "POSTED",
                }
            ]
            original.tables["accounts"] = [
                {
                    "id": "acc-1",
                    "type": "BANK",
                    "currencyCode": "BRL",
                }
            ]

            self.exporter.export_sqlite_bundle_atomic(
                original,
                output_dir,
                generated_at="2026-09-01T00:00:00Z",
                sync_id="baseline",
                full_snapshot=True,
            )

            candidate = self.exporter.ExtractedData()
            candidate.tables["transactions"] = [
                {
                    "id": "must-not-publish",
                    "date": "2026-09-01",
                    "accountId": "acc-1",
                    "currencyCode": "BRL",
                    "status": "POSTED",
                }
            ]
            candidate.tables["accounts"] = [
                {
                    "id": "acc-1",
                    "type": "BANK",
                    "currencyCode": "BRL",
                }
            ]

            with patch.object(
                self.exporter,
                "validate_sqlite_bundle",
                side_effect=RuntimeError("staging inválido"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "staging inválido",
                ):
                    self.exporter.export_sqlite_bundle_atomic(
                        candidate,
                        output_dir,
                        generated_at="2026-09-02T00:00:00Z",
                        sync_id="candidate",
                        full_snapshot=True,
                    )

            with closing(sqlite3.connect(database_path)) as conn:
                ids = [
                    row[0]
                    for row in conn.execute(
                        'SELECT id FROM "transactions"'
                    ).fetchall()
                ]
                latest_sync = conn.execute(
                    'SELECT syncId FROM "_cf_sync_runs" '
                    'ORDER BY startedAtUtc DESC LIMIT 1'
                ).fetchone()[0]

            self.assertEqual(ids, ["keep-me"])
            self.assertEqual(latest_sync, "baseline")

            staging = list(
                output_dir.glob(".*.staging*")
            )
            self.assertEqual(staging, [])

    def test_blocking_extraction_error_prevents_atomic_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            database_path = output_dir / self.exporter.SQLITE_DATABASE_NAME

            original = self.exporter.ExtractedData()
            original.tables["transactions"] = [
                {
                    "id": "old",
                    "date": "2026-08-01",
                    "accountId": "acc-1",
                    "currencyCode": "BRL",
                    "status": "POSTED",
                }
            ]
            original.tables["accounts"] = [
                {
                    "id": "acc-1",
                    "type": "BANK",
                    "currencyCode": "BRL",
                }
            ]

            self.exporter.export_sqlite_bundle_atomic(
                original,
                output_dir,
                sync_id="baseline-error-test",
                full_snapshot=True,
            )

            failed = self.exporter.ExtractedData()
            failed.tables["transactions"] = [
                {
                    "id": "partial",
                    "date": "2026-09-01",
                    "accountId": "acc-1",
                    "currencyCode": "BRL",
                    "status": "POSTED",
                }
            ]
            failed.tables["accounts"] = [
                {
                    "id": "acc-1",
                    "type": "BANK",
                    "currencyCode": "BRL",
                }
            ]
            failed.errors.append(
                {
                    "severity": "ERROR",
                    "scope": "transactions",
                    "resourceId": "acc-2",
                    "message": "falha simulada",
                }
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "não publicada",
            ):
                self.exporter.export_sqlite_bundle_atomic(
                    failed,
                    output_dir,
                    sync_id="must-not-publish",
                    full_snapshot=True,
                )

            with closing(sqlite3.connect(database_path)) as conn:
                ids = [
                    row[0]
                    for row in conn.execute(
                        'SELECT id FROM "transactions"'
                    ).fetchall()
                ]

            self.assertEqual(ids, ["old"])

    def test_warning_does_not_block_atomic_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)

            data = self.exporter.ExtractedData()
            data.tables["accounts"] = [
                {
                    "id": "acc-1",
                    "type": "BANK",
                    "currencyCode": "BRL",
                }
            ]
            data.tables["transactions"] = []
            data.errors.append(
                {
                    "severity": "WARNING",
                    "scope": "categories",
                    "resourceId": None,
                    "message": "warning simulado",
                }
            )

            path = self.exporter.export_sqlite_bundle_atomic(
                data,
                output_dir,
                sync_id="warning-ok",
                full_snapshot=True,
            )

            validation = self.exporter.validate_sqlite_bundle(
                path,
                expected_sync_id="warning-ok",
            )

            self.assertTrue(validation["ok"])

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
