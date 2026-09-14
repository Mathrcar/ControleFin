from __future__ import annotations

import json
import unittest
from pathlib import Path

from common import PROJECT_ROOT, load_server


FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


class SettingsRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = load_server()

    def test_default_schema_is_v9_and_contains_manual_transactions(self):
        settings = self.server.default_settings()

        self.assertEqual(settings["version"], 9)
        self.assertIn("manualTransactions", settings)
        self.assertEqual(settings["manualTransactions"], [])
        self.assertIn("customCategoryTranslations", settings)
        self.assertIn("fixedExpenseRules", settings)
        self.assertIn("fixedIncomeRules", settings)
        self.assertIn("incomeSourceCategoryRules", settings)

    def test_valid_manual_expense_is_normalized(self):
        payload = json.loads(
            (FIXTURES / "settings_manual_valid.json").read_text(
                encoding="utf-8"
            )
        )

        normalized = self.server.normalize_settings(payload)
        rows = normalized["manualTransactions"]

        self.assertEqual(len(rows), 2)

        expense = next(
            row for row in rows if row["manualKind"] == "EXPENSE"
        )
        self.assertEqual(expense["source"], "MANUAL")
        self.assertTrue(expense["manual"])
        self.assertEqual(expense["amount"], -34.90)
        self.assertEqual(expense["normalizedExpense"], 34.90)
        self.assertEqual(expense["normalizedInflow"], 0.0)
        self.assertEqual(expense["accountType"], "CREDIT")
        self.assertEqual(expense["status"], "POSTED")

        income = next(
            row for row in rows if row["manualKind"] == "INCOME"
        )
        self.assertEqual(income["amount"], 5000.0)
        self.assertEqual(income["normalizedInflow"], 5000.0)
        self.assertEqual(income["normalizedExpense"], 0.0)
        self.assertEqual(income["accountType"], "BANK")

    def test_invalid_manual_transaction_is_discarded(self):
        normalized = self.server.normalize_settings(
            {
                "manualTransactions": [
                    {
                        "id": "invalid",
                        "manualKind": "EXPENSE",
                        "date": "",
                        "amount": 0,
                        "description": "",
                    }
                ]
            }
        )

        self.assertEqual(normalized["manualTransactions"], [])

    def test_income_on_credit_account_is_forced_to_bank(self):
        normalized = self.server.normalize_settings(
            {
                "manualTransactions": [
                    {
                        "id": "income-credit",
                        "manualKind": "INCOME",
                        "date": "2026-09-01",
                        "amount": 1000,
                        "description": "Entrada",
                        "category": "@cf:salary",
                        "institutionName": "Banco",
                        "accountName": "Cartão informado incorretamente",
                        "accountId": "acc1",
                        "accountType": "CREDIT",
                        "manualMethod": "Transferência",
                    }
                ]
            }
        )

        self.assertEqual(
            normalized["manualTransactions"][0]["accountType"],
            "BANK",
        )

    def test_ui_language_falls_back_to_pt_br(self):
        normalized = self.server.normalize_settings(
            {"uiLanguage": "xx-YY"}
        )
        self.assertEqual(normalized["uiLanguage"], "pt-BR")


if __name__ == "__main__":
    unittest.main()
