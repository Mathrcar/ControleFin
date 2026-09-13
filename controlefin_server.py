import sys
import time
import threading
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import json

from pluggy_finance_export import run_export


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
        "version": 2,
        "customCategories": [],
        "transactionOverrides": {},
        "categoryRules": [],
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

    transaction_overrides = payload.get("transactionOverrides", {})
    if not isinstance(transaction_overrides, dict):
        transaction_overrides = {}

    raw_rules = payload.get("categoryRules", [])
    if not isinstance(raw_rules, list):
        raw_rules = []

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

    return {
        "version": 2,
        "customCategories": [
            str(category).strip()
            for category in custom_categories
            if str(category).strip()
        ],
        "transactionOverrides": transaction_overrides,
        "categoryRules": category_rules,
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

        # Migra silenciosamente arquivos antigos (version 1) para version 2.
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
    transaction_overrides = payload.get("transactionOverrides", {})
    category_rules = payload.get("categoryRules", [])

    if not isinstance(custom_categories, list):
        raise HTTPException(
            status_code=400,
            detail="customCategories deve ser uma lista.",
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

    settings = normalize_settings(payload)

    save_settings(settings)

    return {
        "ok": True,
        "settingsFile": str(SETTINGS_FILE),
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
    print(f"Atualizando/sobrescrevendo CSVs em: {DATA_DIR}", flush=True)

    try:
        run_export(
            output_dir=DATA_DIR,
            env_file=ENV_FILE,
        )
    except Exception as exc:
        print(f"Falha ao atualizar os dados: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)

    print("Dados atualizados com sucesso.", flush=True)

    threading.Thread(
        target=open_browser,
        daemon=True,
    ).start()

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8765,
    )
