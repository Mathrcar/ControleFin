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
