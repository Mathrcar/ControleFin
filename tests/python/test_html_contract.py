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

    def test_cashflow_chart_uses_one_shared_vertical_scale(self):
        start = self.html.index(
            "  function renderCashflow(m) {"
        )
        end = self.html.index(
            "\n  function compact(v)",
            start,
        )
        block = self.html[
            start:end
        ]

        # Entradas, gastos e líquido precisam passar pelo mesmo y().
        self.assertIn(
            "y(row[def.key])",
            block,
        )

        # Protege contra o desenho antigo, que dava ao líquido uma escala
        # independente e distorcia a forma visual da curva.
        self.assertNotIn(
            "netY=",
            block,
        )
        self.assertNotIn(
            "const scale=Math.max(...rows.map(r=>Math.abs(r.net))",
            block,
        )

        # Líquido negativo precisa aparecer abaixo da linha zero.
        self.assertIn(
            "const dataMin=",
            block,
        )
        self.assertIn(
            "if(min<0 && max>0)",
            block,
        )
        self.assertIn(
            "y(0)",
            block,
        )

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

    def test_authentication_gate_contract_is_present(self):
        for element_id in (
            "accessGate",
            "accessLoading",
            "accessGoogleLogin",
            "googleLoginBtn",
            "accessAdditionalPassword",
            "additionalPasswordForm",
            "logoutBtn",
        ):
            self.assertIn(
                f'id="{element_id}"',
                self.html,
            )

        self.assertNotIn(
            'id="welcome-data-loader"',
            self.html,
        )
        self.assertNotIn(
            'id="loginForm"',
            self.html,
        )
        self.assertNotIn(
            'id="setupForm"',
            self.html,
        )
        self.assertIn(
            "function bootstrapAccess()",
            self.html,
        )
        self.assertIn(
            "function beginGooglePrimaryLogin()",
            self.html,
        )
        self.assertIn(
            "function submitAdditionalPassword(",
            self.html,
        )
        self.assertIn(
            "function authenticatedFetch(",
            self.html,
        )
        self.assertIn(
            "bootstrapAccess();",
            self.html,
        )

    def test_server_authentication_contract_is_present(self):
        self.assertIn(
            'AUTH_USERS_FILE = AUTH_DIR / "users.json"',
            self.server,
        )
        self.assertIn(
            'GOOGLE_LOGIN_TOKEN_FILE',
            self.server,
        )
        self.assertIn(
            'hashlib.pbkdf2_hmac(',
            self.server,
        )
        self.assertIn(
            '@app.post("/api/auth/google/start")',
            self.server,
        )
        self.assertIn(
            '@app.post("/api/auth/google/complete")',
            self.server,
        )
        self.assertIn(
            '@app.post("/api/auth/password/verify")',
            self.server,
        )
        self.assertIn(
            '@app.put("/api/auth/security/password")',
            self.server,
        )
        self.assertIn(
            '@app.delete("/api/auth/security/password")',
            self.server,
        )
        self.assertIn(
            '@app.post("/api/auth/logout")',
            self.server,
        )
        self.assertIn(
            'httponly=True',
            self.server,
        )
        self.assertIn(
            'samesite="strict"',
            self.server,
        )
        self.assertIn(
            'path.startswith("/api/")',
            self.server,
        )

    def test_shareable_installation_contract(self):
        self.assertIn(
            "def get_storage_dir():",
            self.server,
        )
        self.assertIn(
            'os.getenv("LOCALAPPDATA")',
            self.server,
        )
        self.assertIn(
            'PLUGGY_CONFIG_FILE = CONFIG_DIR / "pluggy.json"',
            self.server,
        )
        self.assertIn(
            "def protect_local_secret(secret):",
            self.server,
        )
        self.assertIn(
            "CryptProtectData",
            self.server,
        )
        self.assertIn(
            "CryptUnprotectData",
            self.server,
        )
        self.assertIn(
            '@app.get("/api/pluggy/status")',
            self.server,
        )
        self.assertIn(
            '@app.post("/api/pluggy/test")',
            self.server,
        )
        self.assertIn(
            '@app.put("/api/pluggy/config")',
            self.server,
        )
        self.assertIn(
            '@app.post("/api/sync")',
            self.server,
        )

    def test_dashboard_has_pluggy_setup_and_settings(self):
        for element_id in (
            "accessPluggy",
            "pluggySetupForm",
            "pluggySetupClientId",
            "pluggySetupClientSecret",
            "pluggySetupItemIds",
            "pluggySettingsBtn",
            "pluggySettingsModal",
            "pluggySettingsForm",
        ):
            self.assertIn(
                f'id="{element_id}"',
                self.html,
            )

        self.assertIn(
            "function loadPluggyStatus()",
            self.html,
        )
        self.assertIn(
            "function syncFromPluggy(",
            self.html,
        )
        self.assertIn(
            "function submitPluggySetup(",
            self.html,
        )
        self.assertIn(
            "function savePluggySettings(",
            self.html,
        )

    def test_google_drive_oauth_contract_is_present(self):
        self.assertIn(
            'id="googleDriveBtn"',
            self.html,
        )
        self.assertIn(
            'id="googleDriveModal"',
            self.html,
        )
        self.assertIn(
            'id="googleOAuthClientFile"',
            self.html,
        )
        self.assertIn(
            "function loadGoogleDriveStatus()",
            self.html,
        )
        self.assertIn(
            "function connectGoogleDrive()",
            self.html,
        )
        self.assertIn(
            "function waitForGoogleDriveConnection(",
            self.html,
        )
        self.assertIn(
            "./api/google/connect/start",
            self.html,
        )
        self.assertIn(
            "window.location.assign(",
            self.html,
        )
        self.assertIn(
            "bootstrapAndHandleGoogleOauthReturn",
            self.html,
        )
        self.assertIn(
            "OAuth Client do aplicativo não configurado.",
            self.html,
        )
        self.assertNotIn(
            "!clientConfigured || state.googleDriveBusy",
            self.html,
        )
        self.assertIn(
            "function testGoogleDriveAccess()",
            self.html,
        )

        self.assertIn(
            '@app.get("/api/google/status")',
            self.server,
        )
        self.assertIn(
            '@app.put("/api/google/client-config")',
            self.server,
        )
        self.assertIn(
            '@app.post("/api/google/connect/start")',
            self.server,
        )
        self.assertIn(
            '@app.post("/api/google/connect")',
            self.server,
        )
        self.assertIn(
            '@app.post("/api/google/test")',
            self.server,
        )
        self.assertIn(
            '@app.get("/api/google/drive/files")',
            self.server,
        )
        self.assertIn(
            '@app.post("/api/google/disconnect")',
            self.server,
        )

    def test_frozen_build_uses_executable_directory(self):
        self.assertIn(
            "Path(\n                sys.executable",
            self.server,
        )
        self.assertNotIn(
            'if exe_dir.parent.name.lower() == "dist"',
            self.server,
        )

    def test_encrypted_google_drive_cloud_sync_contract_is_present(self):
        self.assertNotIn(
            'id="cloudPassphraseInput"',
            self.html,
        )
        self.assertIn(
            'id="legacyCloudPasswordWrap"',
            self.html,
        )
        self.assertIn(
            'id="connectGoogleDriveBtn"',
            self.html,
        )
        self.assertIn(
            'id="cloudLocalModifiedTime"',
            self.html,
        )
        self.assertIn(
            'id="cloudRemoteModifiedTime"',
            self.html,
        )
        self.assertIn(
            "Automática pela conta Google",
            self.html,
        )
        self.assertIn(
            "function syncGoogleCloudNow()",
            self.html,
        )
        self.assertIn(
            "function connectOrSyncGoogleDrive()",
            self.html,
        )
        self.assertIn(
            "./api/google/cloud/migrate-legacy",
            self.html,
        )
        self.assertIn(
            "./api/google/cloud/restore",
            self.html,
        )

        self.assertIn(
            '@app.get("/api/google/cloud/status")',
            self.server,
        )
        self.assertIn(
            '@app.post("/api/google/cloud/migrate-legacy")',
            self.server,
        )
        self.assertIn(
            '@app.post("/api/google/cloud/sync")',
            self.server,
        )
        self.assertIn(
            'schedule_cloud_sync(',
            self.server,
        )
        self.assertIn(
            '"settings"',
            self.server,
        )

        cloud_sync = (
            PROJECT_ROOT
            / "controlefin_cloud_sync.py"
        ).read_text(
            encoding="utf-8"
        )

        google_drive = (
            PROJECT_ROOT
            / "controlefin_google_drive.py"
        ).read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'CLOUD_FORMAT_VERSION = 2',
            cloud_sync,
        )
        self.assertIn(
            'CLOUD_AUTO_KEY_MODE = "GOOGLE_DRIVE_ACCOUNT"',
            cloud_sync,
        )
        self.assertIn(
            "def ensure_automatic_key(",
            cloud_sync,
        )
        self.assertIn(
            "def migrate_legacy_snapshot(",
            cloud_sync,
        )
        self.assertIn(
            "def read_cloud_key(",
            google_drive,
        )
        self.assertIn(
            "def write_cloud_key(",
            google_drive,
        )

    def test_multiuser_first_access_and_google_binding_contract(self):
        self.assertIn(
            'id="googleLoginBtn"',
            self.html,
        )
        self.assertIn(
            'id="securityBtn"',
            self.html,
        )
        self.assertIn(
            'id="securityModal"',
            self.html,
        )
        self.assertIn(
            'id="googleAccountStatus"',
            self.html,
        )

        self.assertIn(
            "AUTH_USERS_FILE",
            self.server,
        )
        self.assertIn(
            "PROFILES_DIR",
            self.server,
        )
        self.assertIn(
            "def create_google_user(",
            self.server,
        )
        self.assertIn(
            "def find_auth_user_by_google_account(",
            self.server,
        )
        self.assertIn(
            "def activate_profile(",
            self.server,
        )
        self.assertIn(
            "def update_user_google_account(",
            self.server,
        )
        self.assertIn(
            "account_identity()",
            self.server,
        )
        self.assertIn(
            '"localPasswordEnabled": False',
            self.server,
        )

    def test_visible_google_drive_folder_and_shared_oauth_contract(self):
        self.assertIn(
            'id="googleDriveFolderLink"',
            self.html,
        )
        self.assertIn(
            'Meu Drive / ControleFin',
            self.html,
        )
        self.assertIn(
            'drive.file',
            self.html,
        )
        self.assertIn(
            'BUNDLED_GOOGLE_OAUTH_CLIENT_FILE',
            self.server,
        )
        self.assertIn(
            'install_bundled_google_oauth_client()',
            self.server,
        )
        self.assertIn(
            'cloud_storage_info',
            self.server,
        )

    def test_first_access_restore_or_pluggy_onboarding_contract(self):
        self.assertIn(
            'id="accessOnboardingChoice"',
            self.html,
        )
        self.assertIn(
            'id="restoreFromDriveChoiceBtn"',
            self.html,
        )
        self.assertIn(
            'id="configurePluggyChoiceBtn"',
            self.html,
        )
        self.assertIn(
            'id="accessDriveRestore"',
            self.html,
        )
        self.assertIn(
            'id="onboardingCloudPassphrase"',
            self.html,
        )
        self.assertIn(
            "function beginOnboardingRestore()",
            self.html,
        )
        self.assertIn(
            "function restoreOnboardingFromDrive()",
            self.html,
        )
        self.assertIn(
            "ONBOARDING_RESTORE_KEY",
            self.html,
        )
        self.assertIn(
            "./api/google/cloud/restore",
            self.html,
        )

        self.assertIn(
            '@app.post("/api/google/cloud/restore")',
            self.server,
        )
        self.assertIn(
            "Nenhum backup do ControleFin foi encontrado",
            self.server,
        )
        self.assertIn(
            "CLOUD_SYNC.download(",
            self.server,
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
