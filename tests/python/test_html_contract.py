from __future__ import annotations

import re
import unittest

from common import PROJECT_ROOT


class DashboardStructuralRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            PROJECT_ROOT / "controlefin_dashboard.html"
        ).read_text(encoding="utf-8")

        cls.server = (
            PROJECT_ROOT / "controlefin_server.py"
        ).read_text(encoding="utf-8")

    def test_control_dock_overflow_contract_is_preserved(self):
        self.assertIn(
            "grid-template-columns:repeat(6,minmax(0,1fr))",
            self.html,
        )
        self.assertIn(".control-dock>*{min-width:0}", self.html)
        self.assertIn("max-width:100%", self.html)

    def test_bar_null_zero_guard_exists(self):
        self.assertIn(
            "function renderableBarValue(value)",
            self.html,
        )
        self.assertIn(
            "n>0 ? n : null",
            re.sub(r"\s+", " ", self.html),
        )

    def test_manual_transactions_ui_is_present(self):
        for element_id in (
            "addManualExpenseBtn",
            "addManualIncomeBtn",
            "manual-transaction-panel",
            "manualTransactionsTableBody",
        ):
            self.assertIn(f'id="{element_id}"', self.html)

    def test_overview_contract_is_account_focused(self):
        self.assertIn('id="overview-account-kpis"', self.html)
        self.assertIn('id="overview-cashflow"', self.html)
        self.assertIn('id="accountVisualCards"', self.html)

        self.assertNotIn('id="overview-health"', self.html)
        self.assertNotIn('id="overview-insights"', self.html)
        self.assertNotIn('id="overview-net-worth"', self.html)
        self.assertNotIn('id="overview-investments-kpi"', self.html)

    def test_future_projection_contract_is_present(self):
        for element_id in (
            "futureView",
            "futureProjectionChart",
            "futureProjectionTableBody",
            "futureSalaryReferences",
            "futureFixedReferences",
            "futureInstallmentReferences",
        ):
            self.assertIn(f'id="{element_id}"', self.html)

        self.assertIn(
            "function historicalSalaryProjectionReferences()",
            self.html,
        )
        self.assertIn(
            "function historicalFixedProjectionReferences()",
            self.html,
        )
        self.assertIn(
            "function buildFutureProjection()",
            self.html,
        )

    def test_cardbankslip_hard_protection_is_present(self):
        self.assertIn(
            "function isCardBankslipTransaction(r)",
            self.html,
        )
        self.assertIn(
            "const hardCardBankslip=isCardBankslipTransaction(r);",
            self.html,
        )
        self.assertIn(
            "!r._isCardBankslip",
            self.html,
        )

    def test_dashboard_is_sqlite_only(self):
        self.assertNotIn("folderPicker", self.html)
        self.assertNotIn("loadFromCsvHttp", self.html)
        self.assertNotIn("loadFolderFiles", self.html)
        self.assertNotIn("./data/dataset_catalog.csv", self.html)
        self.assertIn("function loadFromSQLite()", self.html)
        self.assertIn("data/controlefin.db", self.html)

    def test_server_does_not_expose_data_directory_or_csv_fallback(self):
        self.assertNotIn("StaticFiles", self.server)
        self.assertNotIn('app.mount(\\n    "/data"', self.server)
        self.assertNotIn("migrate_csv_bundle_to_sqlite", self.server)
        self.assertNotIn("dataset_catalog.csv", self.server)

    def test_exporter_has_no_csv_persistence_path(self):
        exporter = (
            PROJECT_ROOT / "pluggy_finance_export.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("import csv", exporter)
        self.assertNotIn("export_csv_bundle", exporter)
        self.assertNotIn("migrate_csv_bundle_to_sqlite", exporter)
        self.assertIn(
            "def purge_legacy_csv_files",
            exporter,
        )
        self.assertIn(
            '"storage": "sqlite"',
            exporter,
        )

    def test_server_remains_local_only(self):
        self.assertIn(
            'host="127.0.0.1"',
            self.server,
        )
        self.assertNotIn(
            'host="0.0.0.0"',
            self.server,
        )


if __name__ == "__main__":
    unittest.main()
