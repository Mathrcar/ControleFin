import sys
import time
import threading
import os
import base64
import hashlib
import hmac
import secrets
import webbrowser
import shutil
import ctypes
from ctypes import wintypes
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
import json

from pluggy_finance_export import (
    run_export,
    purge_legacy_csv_files,
    SQLITE_DATABASE_NAME,
    PluggyClient,
    PluggyAPIError,
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


def get_storage_dir():
    """
    Dados pessoais ficam fora da pasta do executável.

    Windows:
        %LOCALAPPDATA%\\ControleFin

    Desenvolvimento/Linux/macOS:
        CONTROLEFIN_HOME, quando definido; caso contrário BASE_DIR.
    """
    explicit = str(
        os.getenv("CONTROLEFIN_HOME") or ""
    ).strip()

    if explicit:
        return Path(
            explicit
        ).expanduser().resolve()

    local_app_data = str(
        os.getenv("LOCALAPPDATA") or ""
    ).strip()

    if os.name == "nt" and local_app_data:
        return (
            Path(local_app_data)
            / "ControleFin"
        )

    return BASE_DIR


STORAGE_DIR = get_storage_dir()

DATA_DIR = STORAGE_DIR / "data"
USER_DATA_DIR = STORAGE_DIR / "user_data"
CONFIG_DIR = STORAGE_DIR / "config"

SETTINGS_FILE = USER_DATA_DIR / "ajustes.json"
AUTH_FILE = USER_DATA_DIR / "auth.json"
PLUGGY_CONFIG_FILE = CONFIG_DIR / "pluggy.json"

HTML_FILE = BASE_DIR / "controlefin_dashboard.html"
LEGACY_ENV_FILE = BASE_DIR / ".env"

DB_FILE = DATA_DIR / SQLITE_DATABASE_NAME

DATA_DIR.mkdir(parents=True, exist_ok=True)
USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI()



PLUGGY_CONFIG_VERSION = 1
PLUGGY_CONFIG_LOCK = threading.RLock()
SYNC_LOCK = threading.Lock()


def atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp_file = path.with_name(
        f".{path.name}.{secrets.token_hex(6)}.tmp"
    )

    try:
        with temp_file.open(
            "w",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(
                handle.fileno()
            )

        try:
            os.chmod(
                temp_file,
                0o600,
            )
        except OSError:
            pass

        os.replace(
            temp_file,
            path,
        )
    finally:
        try:
            temp_file.unlink()
        except FileNotFoundError:
            pass


def normalize_item_ids(values):
    if isinstance(values, str):
        raw_values = re.split(
            r"[\s,;]+",
            values,
        )
    elif isinstance(values, (list, tuple, set)):
        raw_values = values
    else:
        raw_values = []

    result = []
    seen = set()

    for value in raw_values:
        item_id = str(
            value or ""
        ).strip()

        if not item_id:
            continue

        if len(item_id) > 200:
            raise ValueError(
                "Item ID inválido."
            )

        if item_id in seen:
            continue

        seen.add(item_id)
        result.append(item_id)

    return result


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _bytes_to_blob(value):
    buffer = ctypes.create_string_buffer(
        value
    )
    blob = _DATA_BLOB(
        len(value),
        ctypes.cast(
            buffer,
            ctypes.POINTER(
                ctypes.c_ubyte
            ),
        ),
    )
    return blob, buffer


def protect_local_secret(secret):
    """
    Windows DPAPI: o segredo só pode ser decriptado pelo mesmo usuário do
    Windows no mesmo contexto de perfil.

    Não há fallback em texto puro. Em outros sistemas, CONTROLEFIN permite
    desenvolvimento, mas a tela de configuração segura informa que o recurso
    de armazenamento foi projetado para Windows.
    """
    secret = str(
        secret or ""
    )

    if not secret:
        raise ValueError(
            "Client Secret não informado."
        )

    if os.name != "nt":
        raise RuntimeError(
            "O armazenamento seguro das credenciais Pluggy usa Windows DPAPI."
        )

    crypt32 = ctypes.WinDLL(
        "crypt32",
        use_last_error=True,
    )
    kernel32 = ctypes.WinDLL(
        "kernel32",
        use_last_error=True,
    )

    source, source_buffer = _bytes_to_blob(
        secret.encode("utf-8")
    )
    output = _DATA_BLOB()

    CRYPTPROTECT_UI_FORBIDDEN = 0x01

    ok = crypt32.CryptProtectData(
        ctypes.byref(source),
        "ControleFin Pluggy",
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    )

    if not ok:
        raise OSError(
            ctypes.get_last_error(),
            "Falha ao proteger Client Secret com Windows DPAPI.",
        )

    try:
        encrypted = ctypes.string_at(
            output.pbData,
            output.cbData,
        )
    finally:
        kernel32.LocalFree(
            output.pbData
        )

    return {
        "scheme": "windows-dpapi-user",
        "ciphertext": base64.b64encode(
            encrypted
        ).decode("ascii"),
    }


def unprotect_local_secret(record):
    if not isinstance(record, dict):
        raise ValueError(
            "Registro de Client Secret inválido."
        )

    if record.get("scheme") != "windows-dpapi-user":
        raise ValueError(
            "Formato de Client Secret não suportado."
        )

    if os.name != "nt":
        raise RuntimeError(
            "O Client Secret está protegido pelo Windows DPAPI e só pode ser lido no Windows."
        )

    try:
        encrypted = base64.b64decode(
            str(
                record.get("ciphertext")
                or ""
            ),
            validate=True,
        )
    except Exception as exc:
        raise ValueError(
            "Client Secret criptografado inválido."
        ) from exc

    crypt32 = ctypes.WinDLL(
        "crypt32",
        use_last_error=True,
    )
    kernel32 = ctypes.WinDLL(
        "kernel32",
        use_last_error=True,
    )

    source, source_buffer = _bytes_to_blob(
        encrypted
    )
    output = _DATA_BLOB()

    CRYPTPROTECT_UI_FORBIDDEN = 0x01

    ok = crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    )

    if not ok:
        raise OSError(
            ctypes.get_last_error(),
            "Falha ao desbloquear o Client Secret com Windows DPAPI.",
        )

    try:
        clear_bytes = ctypes.string_at(
            output.pbData,
            output.cbData,
        )
    finally:
        kernel32.LocalFree(
            output.pbData
        )

    return clear_bytes.decode(
        "utf-8"
    )


def load_pluggy_config(
    *,
    include_secret=False,
):
    if not PLUGGY_CONFIG_FILE.exists():
        return None

    try:
        raw = json.loads(
            PLUGGY_CONFIG_FILE.read_text(
                encoding="utf-8"
            )
        )
    except Exception as exc:
        raise RuntimeError(
            "A configuração Pluggy local está corrompida."
        ) from exc

    if not isinstance(raw, dict):
        raise RuntimeError(
            "A configuração Pluggy local está inválida."
        )

    if int(
        raw.get("version") or 0
    ) != PLUGGY_CONFIG_VERSION:
        raise RuntimeError(
            "Versão da configuração Pluggy não suportada."
        )

    client_id = str(
        raw.get("clientId")
        or ""
    ).strip()

    item_ids = normalize_item_ids(
        raw.get("itemIds")
        or []
    )

    secret_record = raw.get(
        "clientSecret"
    )

    if not client_id or not secret_record:
        raise RuntimeError(
            "Configuração Pluggy incompleta."
        )

    config = {
        "version": PLUGGY_CONFIG_VERSION,
        "clientId": client_id,
        "itemIds": item_ids,
        "secretConfigured": True,
        "updatedAt": raw.get(
            "updatedAt"
        ),
    }

    if include_secret:
        config["clientSecret"] = (
            unprotect_local_secret(
                secret_record
            )
        )

    return config


def save_pluggy_config(
    *,
    client_id,
    client_secret,
    item_ids,
):
    client_id = str(
        client_id or ""
    ).strip()

    item_ids = normalize_item_ids(
        item_ids
    )

    if not client_id:
        raise ValueError(
            "Client ID não informado."
        )

    if not item_ids:
        raise ValueError(
            "Informe ao menos um Item ID da Pluggy."
        )

    existing = None
    if PLUGGY_CONFIG_FILE.exists():
        try:
            existing = json.loads(
                PLUGGY_CONFIG_FILE.read_text(
                    encoding="utf-8"
                )
            )
        except Exception:
            existing = None

    if str(
        client_secret or ""
    ).strip():
        protected_secret = (
            protect_local_secret(
                str(client_secret)
            )
        )
    elif isinstance(existing, dict):
        protected_secret = existing.get(
            "clientSecret"
        )
        if not protected_secret:
            raise ValueError(
                "Client Secret não informado."
            )
    else:
        raise ValueError(
            "Client Secret não informado."
        )

    payload = {
        "version": PLUGGY_CONFIG_VERSION,
        "clientId": client_id,
        "clientSecret": protected_secret,
        "itemIds": item_ids,
        "updatedAt": utc_iso_now(),
    }

    atomic_write_json(
        PLUGGY_CONFIG_FILE,
        payload,
    )

    return {
        "version": PLUGGY_CONFIG_VERSION,
        "clientId": client_id,
        "itemIds": item_ids,
        "secretConfigured": True,
        "updatedAt": payload["updatedAt"],
    }


def parse_legacy_env(path):
    path = Path(path)
    if not path.exists():
        return {}

    result = {}

    for raw_line in path.read_text(
        encoding="utf-8"
    ).splitlines():
        line = raw_line.strip()

        if (
            not line
            or line.startswith("#")
            or "=" not in line
        ):
            continue

        key, value = line.split(
            "=",
            1,
        )

        key = key.strip()
        value = value.strip()

        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]

        result[key] = value

    return result


def migrate_legacy_storage():
    """
    Migra os arquivos pessoais da antiga pasta do projeto para LocalAppData.

    O legado é copiado, nunca removido automaticamente.
    """
    if STORAGE_DIR.resolve() == BASE_DIR.resolve():
        return []

    copied = []

    candidates = [
        (
            BASE_DIR / "data" / SQLITE_DATABASE_NAME,
            DB_FILE,
        ),
        (
            BASE_DIR / "user_data" / "ajustes.json",
            SETTINGS_FILE,
        ),
        (
            BASE_DIR / "user_data" / "auth.json",
            AUTH_FILE,
        ),
    ]

    for source, destination in candidates:
        if (
            source.exists()
            and not destination.exists()
        ):
            destination.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            shutil.copy2(
                source,
                destination,
            )
            copied.append(
                destination
            )

    return copied


def migrate_legacy_env():
    """
    Importa o .env histórico uma única vez para o cofre DPAPI.

    Depois de salvar e validar estruturalmente o novo arquivo, o .env antigo é
    apagado para que o Client Secret não permaneça em texto puro.
    """
    if PLUGGY_CONFIG_FILE.exists():
        return False

    if not LEGACY_ENV_FILE.exists():
        return False

    values = parse_legacy_env(
        LEGACY_ENV_FILE
    )

    client_id = str(
        values.get(
            "PLUGGY_CLIENT_ID"
        )
        or ""
    ).strip()

    client_secret = str(
        values.get(
            "PLUGGY_CLIENT_SECRET"
        )
        or ""
    ).strip()

    item_ids = normalize_item_ids(
        values.get(
            "PLUGGY_ITEM_IDS"
        )
        or ""
    )

    if (
        not client_id
        or not client_secret
        or not item_ids
    ):
        return False

    save_pluggy_config(
        client_id=client_id,
        client_secret=client_secret,
        item_ids=item_ids,
    )

    # Confirma que o segredo pode ser recuperado antes de excluir o legado.
    migrated = load_pluggy_config(
        include_secret=True
    )

    if (
        migrated["clientId"] != client_id
        or migrated["clientSecret"]
        != client_secret
    ):
        raise RuntimeError(
            "Falha ao validar a migração das credenciais Pluggy."
        )

    LEGACY_ENV_FILE.unlink()
    return True


def pluggy_public_status():
    config = load_pluggy_config(
        include_secret=False
    )

    return {
        "configured": bool(config),
        "clientId": (
            config.get("clientId")
            if config
            else ""
        ),
        "itemIds": (
            config.get("itemIds")
            if config
            else []
        ),
        "secretConfigured": bool(
            config
            and config.get(
                "secretConfigured"
            )
        ),
        "storage": (
            "windows-dpapi-user"
            if config
            else None
        ),
    }


def resolve_pluggy_credentials(
    payload=None,
):
    payload = (
        payload
        if isinstance(payload, dict)
        else {}
    )

    stored = None
    if PLUGGY_CONFIG_FILE.exists():
        stored = load_pluggy_config(
            include_secret=True
        )

    client_id = str(
        payload.get("clientId")
        or (
            stored.get("clientId")
            if stored
            else ""
        )
        or ""
    ).strip()

    client_secret = str(
        payload.get("clientSecret")
        or (
            stored.get("clientSecret")
            if stored
            else ""
        )
        or ""
    ).strip()

    item_ids = normalize_item_ids(
        payload.get("itemIds")
        if "itemIds" in payload
        else (
            stored.get("itemIds")
            if stored
            else []
        )
    )

    if not client_id or not client_secret:
        raise ValueError(
            "Client ID e Client Secret são obrigatórios."
        )

    return (
        client_id,
        client_secret,
        item_ids,
    )


def test_pluggy_access(
    *,
    client_id,
    client_secret,
    item_ids,
):
    client = PluggyClient(
        client_id=client_id,
        client_secret=client_secret,
    )
    client.authenticate()

    checked_items = []

    for item_id in normalize_item_ids(
        item_ids
    ):
        try:
            item = client.get_json(
                f"/items/{item_id}"
            )
        except Exception as exc:
            raise ValueError(
                f"Item ID inválido ou inacessível: {item_id}"
            ) from exc

        connector_name = ""
        if isinstance(item, dict):
            connector = item.get(
                "connector"
            )
            if isinstance(connector, dict):
                connector_name = str(
                    connector.get("name")
                    or ""
                )

        checked_items.append(
            {
                "itemId": item_id,
                "connectorName": connector_name,
            }
        )

    return {
        "ok": True,
        "itemCount": len(
            checked_items
        ),
        "items": checked_items,
    }


def synchronize_from_saved_pluggy():
    if not SYNC_LOCK.acquire(
        blocking=False
    ):
        raise RuntimeError(
            "Já existe uma sincronização em andamento."
        )

    try:
        config = load_pluggy_config(
            include_secret=True
        )

        if not config:
            raise ValueError(
                "Pluggy ainda não configurada."
            )

        return run_export(
            output_dir=DATA_DIR,
            client_id=config[
                "clientId"
            ],
            client_secret=config[
                "clientSecret"
            ],
            item_ids=config[
                "itemIds"
            ],
        )
    finally:
        SYNC_LOCK.release()


AUTH_FILE_VERSION = 1
AUTH_COOKIE_NAME = "controlefin_session"
AUTH_SESSION_TTL_SECONDS = 12 * 60 * 60
AUTH_PASSWORD_MIN_LENGTH = 8
AUTH_PASSWORD_MAX_LENGTH = 256
AUTH_USERNAME_MIN_LENGTH = 3
AUTH_USERNAME_MAX_LENGTH = 80
AUTH_PBKDF2_ITERATIONS = 600_000

AUTH_LOCK = threading.RLock()
AUTH_SESSIONS_LOCK = threading.RLock()
AUTH_SESSIONS = {}

LOGIN_FAILURE_LOCK = threading.RLock()
LOGIN_FAILURES = {}
LOGIN_FAILURE_WINDOW_SECONDS = 60
LOGIN_FAILURE_MAX_ATTEMPTS = 5


def utc_iso_now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_auth_username(value):
    username = str(value or "").strip()

    if not (
        AUTH_USERNAME_MIN_LENGTH
        <= len(username)
        <= AUTH_USERNAME_MAX_LENGTH
    ):
        raise ValueError(
            "O usuário deve ter entre "
            f"{AUTH_USERNAME_MIN_LENGTH} e "
            f"{AUTH_USERNAME_MAX_LENGTH} caracteres."
        )

    if any(ord(char) < 32 for char in username):
        raise ValueError(
            "O usuário contém caracteres inválidos."
        )

    return username


def validate_auth_password(value):
    password = str(value or "")

    if not (
        AUTH_PASSWORD_MIN_LENGTH
        <= len(password)
        <= AUTH_PASSWORD_MAX_LENGTH
    ):
        raise ValueError(
            "A senha deve ter entre "
            f"{AUTH_PASSWORD_MIN_LENGTH} e "
            f"{AUTH_PASSWORD_MAX_LENGTH} caracteres."
        )

    return password


def encode_auth_bytes(value):
    return base64.urlsafe_b64encode(
        value
    ).decode("ascii")


def decode_auth_bytes(value):
    return base64.urlsafe_b64decode(
        str(value).encode("ascii")
    )


def derive_password_hash(
    password,
    salt,
    *,
    iterations=AUTH_PBKDF2_ITERATIONS,
):
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        int(iterations),
    )


def build_auth_config(username, password):
    username = normalize_auth_username(
        username
    )
    password = validate_auth_password(
        password
    )

    salt = os.urandom(16)
    digest = derive_password_hash(
        password,
        salt,
    )

    return {
        "version": AUTH_FILE_VERSION,
        "username": username,
        "password": {
            "algorithm": "pbkdf2_sha256",
            "iterations": AUTH_PBKDF2_ITERATIONS,
            "salt": encode_auth_bytes(salt),
            "hash": encode_auth_bytes(digest),
        },
        "createdAt": utc_iso_now(),
    }


def validate_auth_config(config):
    if not isinstance(config, dict):
        raise ValueError(
            "Arquivo de autenticação inválido."
        )

    if int(config.get("version") or 0) != AUTH_FILE_VERSION:
        raise ValueError(
            "Versão do arquivo de autenticação não suportada."
        )

    username = normalize_auth_username(
        config.get("username")
    )

    password = config.get("password")
    if not isinstance(password, dict):
        raise ValueError(
            "Registro de senha inválido."
        )

    if password.get("algorithm") != "pbkdf2_sha256":
        raise ValueError(
            "Algoritmo de senha não suportado."
        )

    iterations = int(
        password.get("iterations") or 0
    )
    if iterations < 100_000:
        raise ValueError(
            "Parâmetros de senha inválidos."
        )

    salt = decode_auth_bytes(
        password.get("salt")
    )
    digest = decode_auth_bytes(
        password.get("hash")
    )

    if len(salt) < 16 or len(digest) != 32:
        raise ValueError(
            "Registro de senha inválido."
        )

    return {
        **config,
        "username": username,
    }


def load_auth_config():
    if not AUTH_FILE.exists():
        return None

    try:
        config = json.loads(
            AUTH_FILE.read_text(
                encoding="utf-8"
            )
        )
        return validate_auth_config(
            config
        )
    except Exception as exc:
        raise RuntimeError(
            "O arquivo user_data/auth.json está inválido. "
            "Ele não será recriado automaticamente."
        ) from exc


def save_auth_config(config):
    config = validate_auth_config(
        config
    )

    atomic_write_json(
        AUTH_FILE,
        config,
    )


def verify_auth_password(
    config,
    password,
):
    config = validate_auth_config(
        config
    )

    password_record = config["password"]

    try:
        candidate = derive_password_hash(
            str(password or ""),
            decode_auth_bytes(
                password_record["salt"]
            ),
            iterations=int(
                password_record["iterations"]
            ),
        )
        expected = decode_auth_bytes(
            password_record["hash"]
        )
    except Exception:
        return False

    return hmac.compare_digest(
        candidate,
        expected,
    )


def purge_expired_auth_sessions():
    now = time.time()

    with AUTH_SESSIONS_LOCK:
        expired = [
            token
            for token, session
            in AUTH_SESSIONS.items()
            if float(
                session.get("expiresAt") or 0
            ) <= now
        ]

        for token in expired:
            AUTH_SESSIONS.pop(
                token,
                None,
            )


def create_auth_session(username):
    purge_expired_auth_sessions()

    token = secrets.token_urlsafe(32)
    now = time.time()

    with AUTH_SESSIONS_LOCK:
        AUTH_SESSIONS[token] = {
            "username": username,
            "createdAt": now,
            "expiresAt": (
                now
                + AUTH_SESSION_TTL_SECONDS
            ),
        }

    return token


def auth_session_username(token):
    if not token:
        return None

    purge_expired_auth_sessions()

    with AUTH_SESSIONS_LOCK:
        session = AUTH_SESSIONS.get(
            token
        )

        if not session:
            return None

        return str(
            session.get("username")
            or ""
        ) or None


def auth_request_username(request):
    return auth_session_username(
        request.cookies.get(
            AUTH_COOKIE_NAME
        )
    )


def revoke_auth_session(token):
    if not token:
        return

    with AUTH_SESSIONS_LOCK:
        AUTH_SESSIONS.pop(
            token,
            None,
        )


def set_auth_cookie(
    response,
    token,
):
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        max_age=AUTH_SESSION_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
    )


def clear_auth_cookie(response):
    response.delete_cookie(
        key=AUTH_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="strict",
    )


def auth_client_key(request):
    client = getattr(
        request,
        "client",
        None,
    )
    return str(
        getattr(
            client,
            "host",
            None,
        )
        or "local"
    )


def auth_login_retry_after(
    client_key,
):
    now = time.time()

    with LOGIN_FAILURE_LOCK:
        attempts = [
            value
            for value in LOGIN_FAILURES.get(
                client_key,
                [],
            )
            if (
                now - value
                < LOGIN_FAILURE_WINDOW_SECONDS
            )
        ]

        LOGIN_FAILURES[
            client_key
        ] = attempts

        if (
            len(attempts)
            < LOGIN_FAILURE_MAX_ATTEMPTS
        ):
            return 0

        oldest = min(
            attempts
        )

        return max(
            1,
            int(
                LOGIN_FAILURE_WINDOW_SECONDS
                - (now - oldest)
            )
            + 1,
        )


def register_auth_login_failure(
    client_key,
):
    now = time.time()

    with LOGIN_FAILURE_LOCK:
        attempts = [
            value
            for value in LOGIN_FAILURES.get(
                client_key,
                [],
            )
            if (
                now - value
                < LOGIN_FAILURE_WINDOW_SECONDS
            )
        ]
        attempts.append(now)
        LOGIN_FAILURES[
            client_key
        ] = attempts


def clear_auth_login_failures(
    client_key,
):
    with LOGIN_FAILURE_LOCK:
        LOGIN_FAILURES.pop(
            client_key,
            None,
        )


async def auth_json_payload(request):
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail="JSON inválido.",
        ) from exc

    if not isinstance(
        payload,
        dict,
    ):
        raise HTTPException(
            status_code=400,
            detail="Formato inválido.",
        )

    return payload


@app.middleware("http")
async def local_authentication_middleware(
    request,
    call_next,
):
    path = request.url.path

    # O HTML e os endpoints necessários para setup/login são públicos.
    # Qualquer API financeira/configurável exige uma sessão válida.
    if (
        path.startswith("/api/")
        and not path.startswith(
            "/api/auth/"
        )
    ):
        username = auth_request_username(
            request
        )

        if not username:
            return JSONResponse(
                status_code=401,
                content={
                    "detail": (
                        "Autenticação necessária."
                    )
                },
            )

    response = await call_next(
        request
    )

    if path == "/":
        response.headers[
            "Cache-Control"
        ] = "no-store"

    return response


@app.get("/api/auth/status")
def auth_status(request: Request):
    configured = AUTH_FILE.exists()

    if configured:
        # Não transforma arquivo corrompido em "primeiro acesso".
        load_auth_config()

    username = auth_request_username(
        request
    )

    return {
        "configured": configured,
        "authenticated": bool(username),
        "username": username,
        "passwordMinLength": AUTH_PASSWORD_MIN_LENGTH,
        "sessionTtlSeconds": AUTH_SESSION_TTL_SECONDS,
    }


@app.post("/api/auth/setup")
async def auth_setup(request: Request):
    payload = await auth_json_payload(
        request
    )

    with AUTH_LOCK:
        if AUTH_FILE.exists():
            raise HTTPException(
                status_code=409,
                detail=(
                    "O primeiro usuário já foi criado."
                ),
            )

        password = str(
            payload.get("password")
            or ""
        )
        confirm_password = str(
            payload.get(
                "confirmPassword"
            )
            or ""
        )

        if (
            password
            != confirm_password
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "As senhas não coincidem."
                ),
            )

        try:
            config = build_auth_config(
                payload.get("username"),
                password,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=str(exc),
            ) from exc

        save_auth_config(
            config
        )

    token = create_auth_session(
        config["username"]
    )

    response = JSONResponse(
        {
            "ok": True,
            "authenticated": True,
            "username": config["username"],
        }
    )
    set_auth_cookie(
        response,
        token,
    )
    return response


@app.post("/api/auth/login")
async def auth_login(request: Request):
    payload = await auth_json_payload(
        request
    )

    if not AUTH_FILE.exists():
        raise HTTPException(
            status_code=409,
            detail=(
                "Nenhum usuário foi criado ainda."
            ),
        )

    client_key = auth_client_key(
        request
    )
    retry_after = auth_login_retry_after(
        client_key
    )

    if retry_after:
        raise HTTPException(
            status_code=429,
            detail=(
                "Muitas tentativas de login. "
                f"Tente novamente em {retry_after}s."
            ),
            headers={
                "Retry-After": str(
                    retry_after
                )
            },
        )

    config = load_auth_config()

    username = str(
        payload.get("username")
        or ""
    ).strip()

    password_ok = verify_auth_password(
        config,
        payload.get("password"),
    )
    username_ok = hmac.compare_digest(
        username,
        str(config["username"]),
    )

    if not (
        username_ok
        and password_ok
    ):
        register_auth_login_failure(
            client_key
        )
        raise HTTPException(
            status_code=401,
            detail=(
                "Usuário ou senha inválidos."
            ),
        )

    clear_auth_login_failures(
        client_key
    )

    token = create_auth_session(
        config["username"]
    )

    response = JSONResponse(
        {
            "ok": True,
            "authenticated": True,
            "username": config["username"],
        }
    )
    set_auth_cookie(
        response,
        token,
    )
    return response


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    revoke_auth_session(
        request.cookies.get(
            AUTH_COOKIE_NAME
        )
    )

    response = JSONResponse(
        {
            "ok": True,
        }
    )
    clear_auth_cookie(
        response
    )
    return response


@app.get("/api/pluggy/status")
def api_pluggy_status():
    return pluggy_public_status()


@app.post("/api/pluggy/test")
async def api_pluggy_test(
    request: Request,
):
    payload = await auth_json_payload(
        request
    )

    try:
        (
            client_id,
            client_secret,
            item_ids,
        ) = resolve_pluggy_credentials(
            payload
        )

        return test_pluggy_access(
            client_id=client_id,
            client_secret=client_secret,
            item_ids=item_ids,
        )
    except PluggyAPIError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                "A Pluggy recusou as credenciais. "
                "Confira Client ID e Client Secret."
            ),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc


@app.put("/api/pluggy/config")
async def api_pluggy_config(
    request: Request,
):
    payload = await auth_json_payload(
        request
    )

    try:
        (
            client_id,
            client_secret,
            item_ids,
        ) = resolve_pluggy_credentials(
            payload
        )

        if not item_ids:
            raise ValueError(
                "Informe ao menos um Item ID."
            )

        # Só persiste configurações que realmente autenticam e conseguem
        # acessar todos os Item IDs informados.
        test_pluggy_access(
            client_id=client_id,
            client_secret=client_secret,
            item_ids=item_ids,
        )

        with PLUGGY_CONFIG_LOCK:
            saved = save_pluggy_config(
                client_id=client_id,
                client_secret=(
                    payload.get(
                        "clientSecret"
                    )
                    or None
                ),
                item_ids=item_ids,
            )

        return {
            "ok": True,
            **saved,
        }
    except PluggyAPIError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                "A Pluggy recusou as credenciais."
            ),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc


@app.post("/api/sync")
def api_sync():
    if not PLUGGY_CONFIG_FILE.exists():
        raise HTTPException(
            status_code=409,
            detail=(
                "Configure a Pluggy antes de sincronizar."
            ),
        )

    try:
        data = synchronize_from_saved_pluggy()
    except RuntimeError as exc:
        message = str(exc)

        if (
            "sincronização em andamento"
            in message.lower()
        ):
            raise HTTPException(
                status_code=409,
                detail=message,
            ) from exc

        raise HTTPException(
            status_code=500,
            detail=message,
        ) from exc
    except PluggyAPIError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Falha de comunicação com a Pluggy."
            ),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "Não foi possível atualizar os dados: "
                f"{exc}"
            ),
        ) from exc

    return {
        "ok": True,
        "errorCount": len(
            data.errors
        ),
        "database": database_status(),
    }


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
        headers={
            "Cache-Control": "no-store",
        },
    )


def default_settings():
    return {
        "version": 9,
        "customCategories": [],
        "customCategoryTranslations": [],
        "manualTransactions": [],
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

    raw_manual_transactions = payload.get(
        "manualTransactions",
        [],
    )
    if not isinstance(raw_manual_transactions, list):
        raw_manual_transactions = []

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

    manual_transactions = []
    seen_manual_transaction_ids = set()

    allowed_manual_kinds = {"EXPENSE", "INCOME"}
    allowed_account_types = {"BANK", "CREDIT"}
    allowed_manual_methods = {
        "Cartão de crédito",
        "PIX",
        "Boleto",
        "Transferência",
        "Débito / compra em conta",
        "Dinheiro",
        "Outros",
    }
    allowed_manual_classes = {"FIXED", "VARIABLE"}

    for index, raw_transaction in enumerate(
        raw_manual_transactions,
        start=1,
    ):
        if not isinstance(raw_transaction, dict):
            continue

        manual_id = str(
            raw_transaction.get("id")
            or f"manual_{index}"
        ).strip()

        if (
            not manual_id
            or manual_id in seen_manual_transaction_ids
        ):
            manual_id = f"manual_{index}"

        seen_manual_transaction_ids.add(manual_id)

        kind = str(
            raw_transaction.get("manualKind")
            or raw_transaction.get("kind")
            or ""
        ).strip().upper()

        if kind not in allowed_manual_kinds:
            continue

        date_value = str(
            raw_transaction.get("date") or ""
        ).strip()

        try:
            parsed_date = datetime.strptime(
                date_value,
                "%Y-%m-%d",
            )
        except (TypeError, ValueError):
            continue

        try:
            amount = abs(
                float(raw_transaction.get("amount") or 0)
            )
        except (TypeError, ValueError):
            amount = 0.0

        if amount <= 0:
            continue

        description = str(
            raw_transaction.get("description") or ""
        ).strip()[:240]

        category = str(
            raw_transaction.get("category") or ""
        ).strip()[:120]

        institution_name = str(
            raw_transaction.get("institutionName") or ""
        ).strip()[:120]

        account_name = str(
            raw_transaction.get("accountName") or ""
        ).strip()[:120]

        account_id = str(
            raw_transaction.get("accountId") or ""
        ).strip()[:180]

        account_type = str(
            raw_transaction.get("accountType") or "BANK"
        ).strip().upper()

        method = str(
            raw_transaction.get("manualMethod")
            or raw_transaction.get("method")
            or ""
        ).strip()

        if (
            not description
            or not category
            or not institution_name
            or not account_name
            or not account_id
        ):
            continue

        if account_type not in allowed_account_types:
            account_type = "BANK"

        if method not in allowed_manual_methods:
            continue

        # Entrada em cartão seria semanticamente um estorno/crédito,
        # não uma entrada financeira. Mantemos entradas manuais em BANK.
        if kind == "INCOME" and account_type == "CREDIT":
            account_type = "BANK"

        currency_code = str(
            raw_transaction.get("currencyCode") or "BRL"
        ).strip().upper()

        if not re.fullmatch(r"[A-Z]{3}", currency_code):
            currency_code = "BRL"

        manual_class = str(
            raw_transaction.get("manualClass") or "VARIABLE"
        ).strip().upper()

        if manual_class not in allowed_manual_classes:
            manual_class = "VARIABLE"

        counterparty = str(
            raw_transaction.get("counterparty")
            or raw_transaction.get("merchantName")
            or raw_transaction.get("payer_name")
            or ""
        ).strip()[:180]

        created_at = str(
            raw_transaction.get("createdAt") or ""
        ).strip()

        updated_at = str(
            raw_transaction.get("updatedAt") or ""
        ).strip()

        signed_amount = (
            -amount
            if kind == "EXPENSE"
            else amount
        )

        normalized_expense = (
            amount
            if kind == "EXPENSE"
            else 0.0
        )

        normalized_outflow = (
            amount
            if kind == "EXPENSE"
            and account_type == "BANK"
            else 0.0
        )

        normalized_inflow = (
            amount
            if kind == "INCOME"
            else 0.0
        )

        manual_transactions.append(
            {
                "id": manual_id,
                "source": "MANUAL",
                "manual": True,
                "manualKind": kind,
                "date": parsed_date.strftime("%Y-%m-%d"),
                "month": parsed_date.strftime("%Y-%m"),
                "amount": round(signed_amount, 2),
                "currencyCode": currency_code,
                "status": "POSTED",
                "type": (
                    "DEBIT"
                    if kind == "EXPENSE"
                    else "CREDIT"
                ),
                "description": description,
                "institutionName": institution_name,
                "accountName": account_name,
                "accountId": account_id,
                "accountType": account_type,
                "category": category,
                "manualMethod": method,
                "manualClass": manual_class,
                "counterparty": counterparty,
                "merchantName": (
                    counterparty
                    if kind == "EXPENSE"
                    else ""
                ),
                "payer_name": (
                    counterparty
                    if kind == "INCOME"
                    else ""
                ),
                "normalizedExpense": round(
                    normalized_expense,
                    2,
                ),
                "normalizedOutflow": round(
                    normalized_outflow,
                    2,
                ),
                "normalizedInflow": round(
                    normalized_inflow,
                    2,
                ),
                "normalizedCardCredit": 0.0,
                "createdAt": created_at,
                "updatedAt": updated_at,
            }
        )

    ui_language = str(payload.get("uiLanguage") or "pt-BR").strip()
    if ui_language not in {"pt-BR", "en-US"}:
        ui_language = "pt-BR"

    return {
        "version": 9,
        "customCategories": [
            str(category).strip()
            for category in custom_categories
            if str(category).strip()
        ],
        "customCategoryTranslations": custom_category_translations,
        "manualTransactions": manual_transactions,
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
    atomic_write_json(
        SETTINGS_FILE,
        settings,
    )

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
    manual_transactions = payload.get(
        "manualTransactions",
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

    if not isinstance(manual_transactions, list):
        raise HTTPException(
            status_code=400,
            detail="manualTransactions deve ser uma lista.",
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
                    "fileName": "",
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


def open_browser():
    time.sleep(1.5)

    webbrowser.open(
        "http://127.0.0.1:8765"
    )


if __name__ == "__main__":

    migrated_files = migrate_legacy_storage()

    print(
        f"Pasta do aplicativo: {BASE_DIR}",
        flush=True,
    )
    print(
        f"Dados pessoais: {STORAGE_DIR}",
        flush=True,
    )
    print(
        f"Banco SQLite: {DB_FILE}",
        flush=True,
    )

    if migrated_files:
        print(
            "Arquivos locais antigos copiados para "
            f"{STORAGE_DIR}.",
            flush=True,
        )

    if os.name == "nt":
        try:
            if migrate_legacy_env():
                print(
                    "Credenciais Pluggy do .env foram migradas "
                    "para Windows DPAPI; o .env legado foi removido.",
                    flush=True,
                )
        except Exception as exc:
            print(
                "Aviso: não foi possível migrar automaticamente "
                f"o .env legado: {exc}",
                file=sys.stderr,
                flush=True,
            )

    if DB_FILE.exists():
        removed_csv = purge_legacy_csv_files(
            DATA_DIR
        )
        if removed_csv:
            print(
                f"CSV(s) legado(s) removido(s): {len(removed_csv)}",
                flush=True,
            )

    threading.Thread(
        target=open_browser,
        daemon=True,
    ).start()

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8765,
    )
