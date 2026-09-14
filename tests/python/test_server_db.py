from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from common import load_server


class ServerDatabaseRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = load_server()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "controlefin.db"

        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE transactions (
                "_cf_row_key" TEXT PRIMARY KEY,
                "_cf_sync_id" TEXT,
                "_cf_sort_order" INTEGER,
                "_cf_raw_json" TEXT,
                id TEXT,
                date TEXT,
                description TEXT
            );

            CREATE TABLE "_cf_sync_runs" (
                syncId TEXT PRIMARY KEY,
                startedAtUtc TEXT NOT NULL,
                finishedAtUtc TEXT,
                fullSnapshot INTEGER NOT NULL,
                status TEXT NOT NULL,
                datasetCount INTEGER NOT NULL,
                rowCount INTEGER NOT NULL,
                errorCount INTEGER NOT NULL,
                schemaVersion INTEGER NOT NULL
            );

            INSERT INTO transactions VALUES
            ('id:tx-1','sync-1',1,'{"id":"tx-1"}','tx-1','2026-09-01','Teste');

            INSERT INTO "_cf_sync_runs" VALUES
            ('sync-1','2026-09-01T00:00:00Z','2026-09-01T00:01:00Z',
             1,'SUCCESS',1,1,0,1);
            """
        )
        conn.commit()
        conn.close()

        self.original_db_file = self.server.DB_FILE
        self.server.DB_FILE = self.db_path

    def tearDown(self):
        self.server.DB_FILE = self.original_db_file
        self.tmp.cleanup()

    def test_status_reports_available_database(self):
        result = self.server.database_status()

        self.assertTrue(result["available"])
        self.assertEqual(result["transactionCount"], 1)
        self.assertEqual(result["latestSync"]["status"], "SUCCESS")

    def test_public_table_hides_internal_metadata_columns(self):
        result = self.server.database_table("transactions")
        self.assertEqual(result["rowCount"], 1)

        row = result["rows"][0]
        self.assertEqual(row["id"], "tx-1")
        self.assertNotIn("_cf_row_key", row)
        self.assertNotIn("_cf_sync_id", row)
        self.assertNotIn("_cf_raw_json", row)

    def test_internal_table_is_not_public(self):
        with self.assertRaises(self.server.HTTPException) as ctx:
            self.server.database_table("_cf_sync_runs")

        self.assertEqual(ctx.exception.status_code, 404)

    def test_invalid_table_name_is_rejected(self):
        with self.assertRaises(self.server.HTTPException) as ctx:
            self.server.database_table('transactions"; DROP TABLE transactions;--')

        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
