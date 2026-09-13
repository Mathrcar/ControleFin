import sys
import time
import threading
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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
HTML_FILE = BASE_DIR / "controlefin_dashboard.html"
ENV_FILE = BASE_DIR / ".env"

DATA_DIR.mkdir(parents=True, exist_ok=True)
USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI()


@app.get("/")
def dashboard():
    return FileResponse(HTML_FILE)


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
