import sys
import time
import threading
import webbrowser
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import json

from pluggy_finance_export import (
    run_export,
    migrate_csv_bundle_to_sqlite,
    SQLITE_DATABASE_NAME,
)


def get_app_dir():
    """
    Retorna a pasta permanente do ControleFin.

    Desenvolvimento:
        C:/ControleFin/controlefin_server.py
        -> BASE_DIR = C:/ControleFin

    Executável PyInstaller --onedir:
        C:/ControleFin/dist/ControleFin/ControleFin.exe
        -> BASE_DIR = C:/ControleFin

    Dessa forma, recompilar/apagar dist não afeta .env, data, user_data
    nem controlefin_dashboard.html.
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent

        # Estrutura padrão criada por:
        # pyinstaller --clean --onedir --name ControleFin controlefin_server.py
        if exe_dir.parent.name.lower() == "dist":
            return exe_dir.parent.parent

        # Fallback: se o .exe for movido para outro lugar,
        # usa a própria pasta do executável como pasta da aplicação.
        return exe_dir

    return Path(__file__).resolve().parent


BASE_DIR = get_app_dir()

DATA_DIR = BASE_DIR / "data"
USER_DATA_DIR = BASE_DIR / "user_data"
SETTINGS_FILE = USER_DATA_DIR / "ajustes.json"
HTML_FILE = BASE_DIR / "controlefin_dashboard.html"
ENV_FILE = BASE_DIR / ".env"
DB_FILE = DATA_DIR / SQLITE_DATABASE_NAME

DATA_DIR.mkdir(parents=True, exist_ok=True)
USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI()

@app.get("/")
def dashboard():
    if not HTML_FILE.exists():
        raise HTTPException(
            status_code=500,
            detail=f"Dashboard HTML não encontrado em: {HTML_FILE}",
        )

    return FileResponse(
        HTML_FILE,
        media_type="text/html",
    )


def default_settings():
    return {
        "version": 8,
        "customCategories": [],
        "customCategoryTranslations": [],
        "transactionOverrides": {},
        "categoryRules": [],
        "fixedExpenseRules": [],
        "fixedIncomeRules": [],
        "incomeSourceCategoryRules": [],
        "uiLanguage": "pt-BR",
    }


def normalize_settings(payload):
    """
    Normaliza o arquivo de ajustes e mantém compatibilidade com a versão 1.
    A ordem de categoryRules é preservada porque a primeira regra compatível
    tem prioridade.
    """
    if not isinstance(payload, dict):
        return default_settings()

    custom_categories = payload.get("customCategories", [])
    if not isinstance(custom_categories, list):
        custom_categories = []

    raw_custom_category_translations = payload.get(
        "customCategoryTranslations",
        [],
    )
    if not isinstance(raw_custom_category_translations, list):
        raw_custom_category_translations = []

    transaction_overrides = payload.get("transactionOverrides", {})
    if not isinstance(transaction_overrides, dict):
        transaction_overrides = {}

    raw_rules = payload.get("categoryRules", [])
    if not isinstance(raw_rules, list):
        raw_rules = []

    raw_fixed_expense_rules = payload.get("fixedExpenseRules", [])
    if not isinstance(raw_fixed_expense_rules, list):
        raw_fixed_expense_rules = []

    raw_fixed_income_rules = payload.get("fixedIncomeRules", [])
    if not isinstance(raw_fixed_income_rules, list):
        raw_fixed_income_rules = []

    raw_income_source_category_rules = payload.get("incomeSourceCategoryRules", [])
    if not isinstance(raw_income_source_category_rules, list):
        raw_income_source_category_rules = []

    allowed_fields = {"merchant", "description", "bank", "account", "method"}
    allowed_operators = {"contains", "equals", "startsWith"}

    category_rules = []
    seen_rule_ids = set()

    for index, raw_rule in enumerate(raw_rules, start=1):
        if not isinstance(raw_rule, dict):
            continue

        field = str(raw_rule.get("field") or "").strip()
        operator = str(raw_rule.get("operator") or "").strip()
        value = str(raw_rule.get("value") or "").strip()
        category = str(raw_rule.get("category") or "").strip()

        if field not in allowed_fields:
            continue
        if operator not in allowed_operators:
            continue
        if not value or not category:
            continue

        rule_id = str(raw_rule.get("id") or f"rule_{index}").strip()
        if not rule_id or rule_id in seen_rule_ids:
            rule_id = f"rule_{index}"

        seen_rule_ids.add(rule_id)

        category_rules.append(
            {
                "id": rule_id,
                "enabled": raw_rule.get("enabled", True) is not False,
                "field": field,
                "operator": operator,
                "value": value,
                "category": category,
            }
        )

    fixed_expense_rules = []
    seen_fixed_rule_ids = set()

    for index, raw_rule in enumerate(raw_fixed_expense_rules, start=1):
        if not isinstance(raw_rule, dict):
            continue

        rule_id = str(raw_rule.get("id") or f"fixed_rule_{index}").strip()
        if not rule_id or rule_id in seen_fixed_rule_ids:
            rule_id = f"fixed_rule_{index}"
        seen_fixed_rule_ids.add(rule_id)

        merchant_signature = str(raw_rule.get("merchantSignature") or "").strip()
        description_signature = str(raw_rule.get("descriptionSignature") or "").strip()

        # Sem nenhum sinal textual, a regra seria ampla demais.
        if not merchant_signature and not description_signature:
            continue

        try:
            amount = float(raw_rule.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0

        fixed_expense_rules.append(
            {
                "id": rule_id,
                "enabled": raw_rule.get("enabled", True) is not False,
                "merchantSignature": merchant_signature,
                "descriptionSignature": description_signature,
                "institution": str(raw_rule.get("institution") or "").strip(),
                "accountId": str(raw_rule.get("accountId") or "").strip(),
                "method": str(raw_rule.get("method") or "").strip(),
                "category": str(raw_rule.get("category") or "").strip(),
                "amount": amount,
                "reimbursementMode": (
                    str(raw_rule.get("reimbursementMode") or "NONE").strip()
                    if str(raw_rule.get("reimbursementMode") or "NONE").strip()
                    in {"NONE", "FULL", "SHARED"}
                    else "NONE"
                ),
                "splitPeople": max(
                    2,
                    int(raw_rule.get("splitPeople") or 2)
                    if str(raw_rule.get("splitPeople") or "2").lstrip("-").isdigit()
                    else 2,
                ),
                "sourceLabel": str(raw_rule.get("sourceLabel") or "").strip(),
                "sourceTransactionKey": str(raw_rule.get("sourceTransactionKey") or "").strip(),
                "createdAt": str(raw_rule.get("createdAt") or "").strip(),
            }
        )

    fixed_income_rules = []
    seen_fixed_income_rule_ids = set()

    for index, raw_rule in enumerate(raw_fixed_income_rules, start=1):
        if not isinstance(raw_rule, dict):
            continue

        rule_id = str(raw_rule.get("id") or f"fixed_income_rule_{index}").strip()
        if not rule_id or rule_id in seen_fixed_income_rule_ids:
            rule_id = f"fixed_income_rule_{index}"
        seen_fixed_income_rule_ids.add(rule_id)

        merchant_signature = str(raw_rule.get("merchantSignature") or "").strip()
        description_signature = str(raw_rule.get("descriptionSignature") or "").strip()

        if not merchant_signature and not description_signature:
            continue

        try:
            amount = float(raw_rule.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0

        fixed_income_rules.append(
            {
                "id": rule_id,
                "enabled": raw_rule.get("enabled", True) is not False,
                "merchantSignature": merchant_signature,
                "descriptionSignature": description_signature,
                "institution": str(raw_rule.get("institution") or "").strip(),
                "accountId": str(raw_rule.get("accountId") or "").strip(),
                "method": str(raw_rule.get("method") or "").strip(),
                "category": str(raw_rule.get("category") or "").strip(),
                "amount": amount,
                "sourceLabel": str(raw_rule.get("sourceLabel") or "").strip(),
                "sourceTransactionKey": str(raw_rule.get("sourceTransactionKey") or "").strip(),
                "createdAt": str(raw_rule.get("createdAt") or "").strip(),
            }
        )

    income_source_category_rules = []
    seen_income_source_rule_ids = set()

    for index, raw_rule in enumerate(raw_income_source_category_rules, start=1):
        if not isinstance(raw_rule, dict):
            continue

        source_type = str(raw_rule.get("sourceType") or "").strip()
        source_signature = str(raw_rule.get("sourceSignature") or "").strip()
        source_label = str(raw_rule.get("sourceLabel") or "").strip()
        category = str(raw_rule.get("category") or "").strip()

        if source_type not in {"payer", "merchant", "description"}:
            continue
        if not source_signature or not category:
            continue

        rule_id = str(raw_rule.get("id") or f"income_source_rule_{index}").strip()
        if not rule_id or rule_id in seen_income_source_rule_ids:
            rule_id = f"income_source_rule_{index}"
        seen_income_source_rule_ids.add(rule_id)

        income_source_category_rules.append(
            {
                "id": rule_id,
                "enabled": raw_rule.get("enabled", True) is not False,
                "sourceType": source_type,
                "sourceSignature": source_signature,
                "sourceLabel": source_label,
                "accountId": str(raw_rule.get("accountId") or "").strip(),
                "institution": str(raw_rule.get("institution") or "").strip(),
                "category": category,
                "createdAt": str(raw_rule.get("createdAt") or "").strip(),
            }
        )

    custom_category_translations = []
    seen_custom_category_translation_ids = set()

    for index, raw_translation in enumerate(
        raw_custom_category_translations,
        start=1,
    ):
        if not isinstance(raw_translation, dict):
            continue

        canonical = str(
            raw_translation.get("canonical") or ""
        ).strip()
        pt_br = str(
            raw_translation.get("ptBR") or ""
        ).strip()
        en_us = str(
            raw_translation.get("enUS") or ""
        ).strip()
        source_language = str(
            raw_translation.get("sourceLanguage") or "pt-BR"
        ).strip()

        if not canonical:
            continue
        if not pt_br and not en_us:
            continue
        if source_language not in {"pt-BR", "en-US"}:
            source_language = "pt-BR"

        translation_id = str(
            raw_translation.get("id")
            or f"custom_category_translation_{index}"
        ).strip()

        if (
            not translation_id
            or translation_id
            in seen_custom_category_translation_ids
        ):
            translation_id = (
                f"custom_category_translation_{index}"
            )

        seen_custom_category_translation_ids.add(
            translation_id
        )

        custom_category_translations.append(
            {
                "id": translation_id,
                "canonical": canonical,
                "sourceLanguage": source_language,
                "ptBR": pt_br or canonical,
                "enUS": en_us or canonical,
            }
        )

    ui_language = str(payload.get("uiLanguage") or "pt-BR").strip()
    if ui_language not in {"pt-BR", "en-US"}:
        ui_language = "pt-BR"

    return {
        "version": 8,
        "customCategories": [
            str(category).strip()
            for category in custom_categories
            if str(category).strip()
        ],
        "customCategoryTranslations": custom_category_translations,
        "transactionOverrides": transaction_overrides,
        "categoryRules": category_rules,
        "fixedExpenseRules": fixed_expense_rules,
        "fixedIncomeRules": fixed_income_rules,
        "incomeSourceCategoryRules": income_source_category_rules,
        "uiLanguage": ui_language,
    }


def load_settings():
    if not SETTINGS_FILE.exists():
        settings = default_settings()
        save_settings(settings)
        return settings

    try:
        raw_settings = json.loads(
            SETTINGS_FILE.read_text(encoding="utf-8")
        )
        settings = normalize_settings(raw_settings)

        # Migra silenciosamente arquivos antigos para o schema atual.
        if settings != raw_settings:
            save_settings(settings)

        return settings
    except Exception:
        return default_settings()


def save_settings(settings):
    USER_DATA_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    temp_file = SETTINGS_FILE.with_suffix(".tmp")

    temp_file.write_text(
        json.dumps(
            settings,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    temp_file.replace(SETTINGS_FILE)

@app.get("/api/settings")
def get_settings():
    return load_settings()


@app.put("/api/settings")
async def update_settings(request: Request):
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail="JSON inválido.",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail="Formato inválido.",
        )

    custom_categories = payload.get("customCategories", [])
    custom_category_translations = payload.get(
        "customCategoryTranslations",
        [],
    )
    transaction_overrides = payload.get("transactionOverrides", {})
    category_rules = payload.get("categoryRules", [])
    fixed_expense_rules = payload.get("fixedExpenseRules", [])
    fixed_income_rules = payload.get("fixedIncomeRules", [])
    income_source_category_rules = payload.get("incomeSourceCategoryRules", [])

    if not isinstance(custom_categories, list):
        raise HTTPException(
            status_code=400,
            detail="customCategories deve ser uma lista.",
        )

    if not isinstance(custom_category_translations, list):
        raise HTTPException(
            status_code=400,
            detail=(
                "customCategoryTranslations deve ser "
                "uma lista."
            ),
        )

    if not isinstance(transaction_overrides, dict):
        raise HTTPException(
            status_code=400,
            detail="transactionOverrides deve ser um objeto.",
        )

    if not isinstance(category_rules, list):
        raise HTTPException(
            status_code=400,
            detail="categoryRules deve ser uma lista.",
        )

    if not isinstance(fixed_expense_rules, list):
        raise HTTPException(
            status_code=400,
            detail="fixedExpenseRules deve ser uma lista.",
        )

    if not isinstance(fixed_income_rules, list):
        raise HTTPException(
            status_code=400,
            detail="fixedIncomeRules deve ser uma lista.",
        )

    if not isinstance(income_source_category_rules, list):
        raise HTTPException(
            status_code=400,
            detail="incomeSourceCategoryRules deve ser uma lista.",
        )

    settings = normalize_settings(payload)

    save_settings(settings)

    return {
        "ok": True,
        "settingsFile": str(SETTINGS_FILE),
    }



DB_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def open_database():
    if not DB_FILE.exists():
        raise HTTPException(
            status_code=503,
            detail="Banco SQLite ainda não foi criado.",
        )

    connection = sqlite3.connect(
        DB_FILE,
        timeout=5,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def database_public_tables(connection):
    rows = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
          AND name NOT LIKE '_cf_%'
        ORDER BY name
        """
    ).fetchall()
    return {
        str(row["name"])
        for row in rows
    }


def database_public_columns(
    connection,
    table_name,
):
    rows = connection.execute(
        f'PRAGMA table_info("{table_name}")'
    ).fetchall()
    return [
        str(row["name"])
        for row in rows
        if not str(row["name"]).startswith("_cf_")
    ]


def read_database_table(
    connection,
    table_name,
):
    if not DB_TABLE_RE.fullmatch(table_name):
        raise HTTPException(
            status_code=400,
            detail="Nome de tabela inválido.",
        )

    public_tables = database_public_tables(connection)
    if table_name not in public_tables:
        raise HTTPException(
            status_code=404,
            detail="Tabela não encontrada.",
        )

    columns = database_public_columns(
        connection,
        table_name,
    )
    if not columns:
        return []

    quoted_columns = ", ".join(
        '"' + column.replace('"', '""') + '"'
        for column in columns
    )

    rows = connection.execute(
        f'SELECT {quoted_columns} '
        f'FROM "{table_name}" '
        'ORDER BY "_cf_sort_order", rowid'
    ).fetchall()

    return [
        dict(row)
        for row in rows
    ]


@app.get("/api/db/status")
def database_status():
    if not DB_FILE.exists():
        return {
            "available": False,
            "database": SQLITE_DATABASE_NAME,
        }

    with closing(open_database()) as connection:
        public_tables = database_public_tables(connection)
        latest_sync = None

        try:
            row = connection.execute(
                """
                SELECT
                    syncId,
                    startedAtUtc,
                    finishedAtUtc,
                    fullSnapshot,
                    status,
                    datasetCount,
                    rowCount,
                    errorCount,
                    schemaVersion
                FROM "_cf_sync_runs"
                ORDER BY startedAtUtc DESC
                LIMIT 1
                """
            ).fetchone()
            if row:
                latest_sync = dict(row)
        except sqlite3.DatabaseError:
            latest_sync = None

        transaction_count = 0
        if "transactions" in public_tables:
            transaction_count = int(
                connection.execute(
                    'SELECT COUNT(*) '
                    'FROM "transactions"'
                ).fetchone()[0]
            )

        return {
            "available": True,
            "database": SQLITE_DATABASE_NAME,
            "sizeBytes": DB_FILE.stat().st_size,
            "datasetCount": len(public_tables),
            "transactionCount": transaction_count,
            "latestSync": latest_sync,
        }


@app.get("/api/db/catalog")
def database_catalog():
    with closing(open_database()) as connection:
        public_tables = database_public_tables(connection)

        if "dataset_catalog" in public_tables:
            return read_database_table(
                connection,
                "dataset_catalog",
            )

        rows = []
        for table_name in sorted(public_tables):
            count = int(
                connection.execute(
                    f'SELECT COUNT(*) '
                    f'FROM "{table_name}"'
                ).fetchone()[0]
            )
            rows.append(
                {
                    "tableName": table_name,
                    "fileName": f"{table_name}.csv",
                    "rowCount": count,
                    "grain": "",
                    "description": "",
                    "storage": "sqlite",
                }
            )
        return rows


@app.get("/api/db/table/{table_name}")
def database_table(table_name: str):
    with closing(open_database()) as connection:
        rows = read_database_table(
            connection,
            table_name,
        )
        return {
            "tableName": table_name,
            "rowCount": len(rows),
            "rows": rows,
        }


app.mount(
    "/data",
    StaticFiles(directory=DATA_DIR),
    name="data",
)


def open_browser():
    time.sleep(1.5)

    webbrowser.open(
        "http://127.0.0.1:8765"
    )


if __name__ == "__main__":

    print(f"Pasta permanente da aplicação: {BASE_DIR}", flush=True)
    print(f"Dados locais: {DATA_DIR}", flush=True)
    print(f"Banco SQLite: {DB_FILE}", flush=True)

    # Primeira ponte de migração: aproveita os CSVs já existentes.
    if (
        not DB_FILE.exists()
        and (DATA_DIR / "dataset_catalog.csv").exists()
    ):
        try:
            migrated = migrate_csv_bundle_to_sqlite(
                DATA_DIR
            )
            if migrated:
                print(
                    f"CSVs existentes migrados para: {migrated}",
                    flush=True,
                )
        except Exception as exc:
            print(
                "Aviso: não foi possível migrar os CSVs "
                f"existentes para SQLite: {exc}",
                file=sys.stderr,
                flush=True,
            )

    try:
        run_export(
            output_dir=DATA_DIR,
            env_file=ENV_FILE,
        )
        print(
            "Dados atualizados com sucesso em CSV + SQLite.",
            flush=True,
        )
    except Exception as exc:
        print(
            f"Falha ao atualizar os dados: {exc}",
            file=sys.stderr,
            flush=True,
        )

        # Preserva o uso offline dos últimos dados válidos.
        if DB_FILE.exists():
            print(
                "Usando o último banco SQLite local disponível.",
                file=sys.stderr,
                flush=True,
            )
        elif (DATA_DIR / "dataset_catalog.csv").exists():
            print(
                "SQLite indisponível; mantendo fallback "
                "pelos CSVs existentes.",
                file=sys.stderr,
                flush=True,
            )
        else:
            raise SystemExit(1)

    threading.Thread(
        target=open_browser,
        daemon=True,
    ).start()

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8765,
    )
