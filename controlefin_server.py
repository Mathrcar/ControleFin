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

from controlefin_google_drive import (
    GoogleDriveManager,
)

from controlefin_cloud_sync import (
    CloudSyncManager,
)


def get_app_dir():
    """
    Diretório dos arquivos do programa.

    Desenvolvimento:
        pasta que contém controlefin_server.py

    PyInstaller --onedir:
        pasta que contém ControleFin.exe

    Dados pessoais permanecem em %LOCALAPPDATA%\\ControleFin.
    """
    if getattr(
        sys,
        "frozen",
        False,
    ):
        return (
            Path(
                sys.executable
            )
            .resolve()
            .parent
        )

    return (
        Path(__file__)
        .resolve()
        .parent
    )


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

# ---------------------------------------------------------------------------
# Layout multiusuário
#
# %LOCALAPPDATA%\ControleFin
# ├── auth\users.json
# ├── config\google_oauth_client.json    <- compartilhado pelo aplicativo
# └── profiles\<profileId>\
#     ├── data\controlefin.db
#     ├── user_data\ajustes.json
#     └── config\
#         ├── pluggy.json
#         ├── google_drive_token.json
#         ├── cloud_sync_key.json
#         └── cloud_sync_state.json
# ---------------------------------------------------------------------------

ROOT_DATA_DIR = STORAGE_DIR / "data"
ROOT_USER_DATA_DIR = STORAGE_DIR / "user_data"
ROOT_CONFIG_DIR = STORAGE_DIR / "config"
AUTH_DIR = STORAGE_DIR / "auth"
PROFILES_DIR = STORAGE_DIR / "profiles"

# Arquivo antigo, usado somente para migração da instalação single-user.
LEGACY_AUTH_FILE = ROOT_USER_DATA_DIR / "auth.json"
AUTH_FILE = LEGACY_AUTH_FILE

AUTH_USERS_FILE = AUTH_DIR / "users.json"
AUTH_REGISTRY_VERSION = 2

# O OAuth Client identifica o ControleFin como aplicativo e pode ser
# compartilhado por todos os perfis locais. O token da conta Google não pode.
GOOGLE_OAUTH_CLIENT_FILE = (
    ROOT_CONFIG_DIR
    / "google_oauth_client.json"
)
GOOGLE_LOGIN_TOKEN_FILE = (
    ROOT_CONFIG_DIR
    / "google_login_token.json"
)
# O administrador pode colocar o OAuth Client do aplicativo ao lado do código
# antes do build. O build o renomeia para este arquivo e todos os usuários usam
# o mesmo Client ID, cada um autorizando a própria conta Google.
BUNDLED_GOOGLE_OAUTH_CLIENT_FILE = (
    BASE_DIR
    / "google_oauth_client.bundled.json"
)

# Antes do login apontamos para o layout legado/bootstrap. Nenhuma API
# financeira é acessível sem sessão; no login estes caminhos são substituídos
# pelos caminhos do perfil autenticado.
DATA_DIR = ROOT_DATA_DIR
USER_DATA_DIR = ROOT_USER_DATA_DIR
CONFIG_DIR = ROOT_CONFIG_DIR

SETTINGS_FILE = USER_DATA_DIR / "ajustes.json"
PLUGGY_CONFIG_FILE = CONFIG_DIR / "pluggy.json"
GOOGLE_DRIVE_TOKEN_FILE = CONFIG_DIR / "google_drive_token.json"
CLOUD_SYNC_KEY_FILE = CONFIG_DIR / "cloud_sync_key.json"
CLOUD_SYNC_STATE_FILE = CONFIG_DIR / "cloud_sync_state.json"
DB_FILE = DATA_DIR / SQLITE_DATABASE_NAME

ACTIVE_PROFILE_LOCK = threading.RLock()
ACTIVE_PROFILE_ID = None
ACTIVE_PROFILE_USERNAME = None

HTML_FILE = BASE_DIR / "controlefin_dashboard.html"
LEGACY_ENV_FILE = BASE_DIR / ".env"

for _directory in (
    ROOT_DATA_DIR,
    ROOT_USER_DATA_DIR,
    ROOT_CONFIG_DIR,
    AUTH_DIR,
    PROFILES_DIR,
):
    _directory.mkdir(
        parents=True,
        exist_ok=True,
    )

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
    Windows DPAPI: o conteúdo protegido só pode ser decriptado pelo mesmo
    usuário do Windows no mesmo contexto de perfil.

    É usado para o Client Secret da Pluggy e para o token OAuth persistente
    do Google Drive.
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
            "O armazenamento seguro local usa Windows DPAPI."
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
        "ControleFin secret",
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
            "O segredo local está protegido pelo Windows DPAPI e só pode ser lido no Windows."
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


GOOGLE_DRIVE_LOCK = threading.Lock()

GOOGLE_DRIVE = GoogleDriveManager(
    client_config_file=(
        GOOGLE_OAUTH_CLIENT_FILE
    ),
    token_file=(
        GOOGLE_DRIVE_TOKEN_FILE
    ),
    protect_secret=(
        protect_local_secret
    ),
    unprotect_secret=(
        unprotect_local_secret
    ),
    atomic_write_json=(
        atomic_write_json
    ),
)

def install_bundled_google_oauth_client():
    """Instala o OAuth Client compartilhado do aplicativo, se empacotado."""
    if GOOGLE_OAUTH_CLIENT_FILE.exists():
        return False

    if not BUNDLED_GOOGLE_OAUTH_CLIENT_FILE.exists():
        return False

    try:
        payload = json.loads(
            BUNDLED_GOOGLE_OAUTH_CLIENT_FILE.read_text(
                encoding="utf-8"
            )
        )
        GOOGLE_DRIVE.save_client_config(
            payload,
            clear_token=False,
        )
        return True
    except Exception as exc:
        raise RuntimeError(
            "O OAuth Client embutido no ControleFin está inválido."
        ) from exc


# Em um build distribuído, isto evita que cada pessoa tenha que importar o
# mesmo credentials.json. Se não houver arquivo embutido, a interface mantém
# a importação manual como fallback para desenvolvimento.
install_bundled_google_oauth_client()

# Manager usado somente enquanto ainda não sabemos qual perfil local pertence à
# conta Google escolhida. O token temporário é movido para o perfil correto
# depois que o Google identifica o usuário.
GOOGLE_LOGIN = GoogleDriveManager(
    client_config_file=(
        GOOGLE_OAUTH_CLIENT_FILE
    ),
    token_file=(
        GOOGLE_LOGIN_TOKEN_FILE
    ),
    protect_secret=(
        protect_local_secret
    ),
    unprotect_secret=(
        unprotect_local_secret
    ),
    atomic_write_json=(
        atomic_write_json
    ),
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
            ROOT_DATA_DIR / SQLITE_DATABASE_NAME,
        ),
        (
            BASE_DIR / "user_data" / "ajustes.json",
            ROOT_USER_DATA_DIR / "ajustes.json",
        ),
        (
            BASE_DIR / "user_data" / "auth.json",
            LEGACY_AUTH_FILE,
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
    target_pluggy_file = (
        PLUGGY_CONFIG_FILE
        if ACTIVE_PROFILE_ID
        else ROOT_CONFIG_DIR
        / "pluggy.json"
    )

    if target_pluggy_file.exists():
        return False

    if AUTH_USERS_FILE.exists():
        # Em modo multiusuário não é possível inferir a qual pessoa um .env
        # legado pertence. A migração automática fica restrita ao bootstrap
        # single-user anterior à criação do registro.
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
AUTH_GOOGLE_PENDING_COOKIE_NAME = "controlefin_google_pending"
AUTH_SESSION_TTL_SECONDS = 12 * 60 * 60
AUTH_GOOGLE_PENDING_TTL_SECONDS = 5 * 60
AUTH_PASSWORD_MIN_LENGTH = 8
AUTH_PASSWORD_MAX_LENGTH = 256
AUTH_USERNAME_MIN_LENGTH = 3
AUTH_USERNAME_MAX_LENGTH = 80
AUTH_PBKDF2_ITERATIONS = 600_000

AUTH_LOCK = threading.RLock()
AUTH_SESSIONS_LOCK = threading.RLock()
AUTH_SESSIONS = {}

GOOGLE_PENDING_AUTH_LOCK = threading.RLock()
GOOGLE_PENDING_AUTH = {}

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


def auth_username_key(
    value,
):
    return normalize_auth_username(
        value
    ).casefold()


def new_profile_id():
    return secrets.token_hex(
        16
    )


def validate_profile_id(
    value,
):
    profile_id = str(
        value
        or ""
    ).strip().lower()

    if not re.fullmatch(
        r"[a-f0-9]{32}",
        profile_id,
    ):
        raise ValueError(
            "Identificador de perfil inválido."
        )

    return profile_id


def empty_auth_registry():
    return {
        "version": (
            AUTH_REGISTRY_VERSION
        ),
        "users": [],
        "updatedAt": utc_iso_now(),
    }


def validate_auth_registry(
    registry,
):
    if not isinstance(
        registry,
        dict,
    ):
        raise ValueError(
            "Registro de usuários inválido."
        )

    if int(
        registry.get("version")
        or 0
    ) != AUTH_REGISTRY_VERSION:
        raise ValueError(
            "Versão do registro de usuários não suportada."
        )

    raw_users = registry.get(
        "users"
    )

    if not isinstance(
        raw_users,
        list,
    ):
        raise ValueError(
            "Lista de usuários inválida."
        )

    users = []
    seen_usernames = set()
    seen_profiles = set()

    for raw_user in raw_users:
        user = validate_auth_config(
            raw_user
        )

        profile_id = validate_profile_id(
            raw_user.get(
                "profileId"
            )
        )

        username_key = auth_username_key(
            user["username"]
        )

        if username_key in seen_usernames:
            raise ValueError(
                "Existem usuários duplicados."
            )

        if profile_id in seen_profiles:
            raise ValueError(
                "Existem perfis duplicados."
            )

        google_account = raw_user.get(
            "googleAccount"
        )

        if (
            google_account is not None
            and not isinstance(
                google_account,
                dict,
            )
        ):
            raise ValueError(
                "Associação Google inválida."
            )

        users.append(
            {
                **user,
                "profileId": profile_id,
                "usernameKey": username_key,
                "googleAccount": (
                    dict(
                        google_account
                    )
                    if isinstance(
                        google_account,
                        dict,
                    )
                    else None
                ),
            }
        )

        seen_usernames.add(
            username_key
        )
        seen_profiles.add(
            profile_id
        )

    return {
        "version": (
            AUTH_REGISTRY_VERSION
        ),
        "users": users,
        "updatedAt": (
            registry.get(
                "updatedAt"
            )
            or utc_iso_now()
        ),
    }


def save_auth_registry(
    registry,
):
    validated = validate_auth_registry(
        registry
    )

    validated[
        "updatedAt"
    ] = utc_iso_now()

    atomic_write_json(
        AUTH_USERS_FILE,
        validated,
    )

    return validated


def profile_root(
    profile_id,
):
    return (
        PROFILES_DIR
        / validate_profile_id(
            profile_id
        )
    )


def ensure_profile_directories(
    profile_id,
):
    root = profile_root(
        profile_id
    )

    paths = {
        "root": root,
        "data": root / "data",
        "user_data": root / "user_data",
        "config": root / "config",
    }

    for path in paths.values():
        path.mkdir(
            parents=True,
            exist_ok=True,
        )

    return paths


def _copy_if_missing(
    source,
    destination,
):
    source = Path(
        source
    )
    destination = Path(
        destination
    )

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
        return True

    return False


def migrate_single_user_auth_to_registry():
    """
    Migração compatível com instalações anteriores.

    O antigo auth.json vira o primeiro usuário do registro e os dados locais
    existentes são COPIADOS para o novo perfil. O legado não é apagado
    automaticamente, permitindo recuperação manual.
    """
    if AUTH_USERS_FILE.exists():
        return False

    if not LEGACY_AUTH_FILE.exists():
        return False

    try:
        legacy = json.loads(
            LEGACY_AUTH_FILE.read_text(
                encoding="utf-8"
            )
        )
        legacy = validate_auth_config(
            legacy
        )
    except Exception as exc:
        raise RuntimeError(
            "O auth.json legado está inválido e não pode ser migrado."
        ) from exc

    profile_id = new_profile_id()
    paths = ensure_profile_directories(
        profile_id
    )

    user = {
        **legacy,
        "profileId": profile_id,
        "usernameKey": auth_username_key(
            legacy[
                "username"
            ]
        ),
        "googleAccount": None,
    }

    registry = empty_auth_registry()
    registry["users"] = [
        user
    ]
    save_auth_registry(
        registry
    )

    copies = (
        (
            ROOT_DATA_DIR
            / SQLITE_DATABASE_NAME,
            paths["data"]
            / SQLITE_DATABASE_NAME,
        ),
        (
            ROOT_USER_DATA_DIR
            / "ajustes.json",
            paths["user_data"]
            / "ajustes.json",
        ),
        (
            ROOT_CONFIG_DIR
            / "pluggy.json",
            paths["config"]
            / "pluggy.json",
        ),
        (
            ROOT_CONFIG_DIR
            / "google_drive_token.json",
            paths["config"]
            / "google_drive_token.json",
        ),
        (
            ROOT_CONFIG_DIR
            / "cloud_sync_key.json",
            paths["config"]
            / "cloud_sync_key.json",
        ),
        (
            ROOT_CONFIG_DIR
            / "cloud_sync_state.json",
            paths["config"]
            / "cloud_sync_state.json",
        ),
    )

    for source, destination in copies:
        _copy_if_missing(
            source,
            destination,
        )

    return True


def load_auth_registry():
    migrate_single_user_auth_to_registry()

    if not AUTH_USERS_FILE.exists():
        return empty_auth_registry()

    try:
        registry = json.loads(
            AUTH_USERS_FILE.read_text(
                encoding="utf-8"
            )
        )
        return validate_auth_registry(
            registry
        )
    except Exception as exc:
        raise RuntimeError(
            "O registro local de usuários está inválido."
        ) from exc


def find_auth_user(
    username,
):
    try:
        key = auth_username_key(
            username
        )
    except ValueError:
        return None

    registry = load_auth_registry()

    for user in registry[
        "users"
    ]:
        if hmac.compare_digest(
            user[
                "usernameKey"
            ],
            key,
        ):
            return user

    return None


def find_auth_user_by_profile(
    profile_id,
):
    profile_id = validate_profile_id(
        profile_id
    )

    registry = load_auth_registry()

    for user in registry[
        "users"
    ]:
        if hmac.compare_digest(
            user[
                "profileId"
            ],
            profile_id,
        ):
            return user

    return None


def create_local_user(
    username,
    password,
):
    registry = load_auth_registry()

    key = auth_username_key(
        username
    )

    if any(
        hmac.compare_digest(
            user[
                "usernameKey"
            ],
            key,
        )
        for user in registry[
            "users"
        ]
    ):
        raise ValueError(
            "Já existe um usuário com esse nome neste computador."
        )

    auth = build_auth_config(
        username,
        password,
    )

    profile_id = new_profile_id()

    user = {
        **auth,
        "profileId": profile_id,
        "usernameKey": key,
        "googleAccount": None,
    }

    ensure_profile_directories(
        profile_id
    )

    registry[
        "users"
    ].append(
        user
    )

    save_auth_registry(
        registry
    )

    return user


def user_local_password_enabled(
    user,
):
    if not isinstance(
        user,
        dict,
    ):
        return False

    # Registros antigos tinham senha obrigatória e não possuíam este campo.
    return bool(
        user.get(
            "localPasswordEnabled",
            True,
        )
    )


def google_account_key(
    account,
):
    account = (
        account
        if isinstance(
            account,
            dict,
        )
        else {}
    )

    permission_id = str(
        account.get(
            "permissionId"
        )
        or ""
    ).strip()

    email = str(
        account.get(
            "emailAddress"
        )
        or ""
    ).strip().casefold()

    return (
        permission_id,
        email,
    )


def find_auth_user_by_google_account(
    account,
):
    permission_id, email = (
        google_account_key(
            account
        )
    )

    if not (
        permission_id
        or email
    ):
        return None

    registry = load_auth_registry()

    for user in registry[
        "users"
    ]:
        other = user.get(
            "googleAccount"
        )

        if not isinstance(
            other,
            dict,
        ):
            continue

        other_permission, other_email = (
            google_account_key(
                other
            )
        )

        same_permission = bool(
            permission_id
            and other_permission
            and hmac.compare_digest(
                permission_id,
                other_permission,
            )
        )

        same_email = bool(
            email
            and other_email
            and hmac.compare_digest(
                email,
                other_email,
            )
        )

        if (
            same_permission
            or same_email
        ):
            return user

    return None


def google_profile_username(
    account,
    registry,
):
    account = (
        account
        if isinstance(
            account,
            dict,
        )
        else {}
    )

    email = str(
        account.get(
            "emailAddress"
        )
        or ""
    ).strip()

    display_name = str(
        account.get(
            "displayName"
        )
        or ""
    ).strip()

    permission_id = str(
        account.get(
            "permissionId"
        )
        or ""
    ).strip()

    candidates = [
        email,
        display_name,
        (
            "google-"
            + (
                permission_id[-12:]
                if permission_id
                else secrets.token_hex(
                    6
                )
            )
        ),
    ]

    existing_keys = {
        str(
            user.get(
                "usernameKey"
            )
            or ""
        )
        for user in registry[
            "users"
        ]
    }

    for raw in candidates:
        raw = "".join(
            char
            for char in str(
                raw
                or ""
            )
            if ord(char) >= 32
        ).strip()

        if not raw:
            continue

        if len(
            raw
        ) > AUTH_USERNAME_MAX_LENGTH:
            raw = raw[
                :AUTH_USERNAME_MAX_LENGTH
            ]

        if len(
            raw
        ) < AUTH_USERNAME_MIN_LENGTH:
            continue

        try:
            key = auth_username_key(
                raw
            )
        except ValueError:
            continue

        if key not in existing_keys:
            return raw

    base = "google-user"

    for index in range(
        1,
        1000,
    ):
        candidate = (
            f"{base}-{index}"
        )
        key = auth_username_key(
            candidate
        )

        if key not in existing_keys:
            return candidate

    raise RuntimeError(
        "Não foi possível criar um identificador local para a conta Google."
    )


def create_google_user(
    account,
):
    account = (
        dict(account)
        if isinstance(
            account,
            dict,
        )
        else {}
    )

    permission_id, email = (
        google_account_key(
            account
        )
    )

    if not (
        permission_id
        or email
    ):
        raise ValueError(
            "O Google não retornou uma identidade de usuário válida."
        )

    existing = (
        find_auth_user_by_google_account(
            account
        )
    )

    if existing:
        return (
            existing,
            False,
        )

    registry = load_auth_registry()

    username = (
        google_profile_username(
            account,
            registry,
        )
    )

    # Mantemos um hash aleatório somente para preservar compatibilidade com o
    # formato antigo do registro. A senha fica DESABILITADA até o usuário
    # decidir criar uma senha adicional.
    auth = build_auth_config(
        username,
        secrets.token_urlsafe(
            48
        ),
    )

    profile_id = new_profile_id()

    user = {
        **auth,
        "profileId": profile_id,
        "usernameKey": (
            auth_username_key(
                username
            )
        ),
        "authMode": "google",
        "localPasswordEnabled": False,
        "googleAccount": {
            "displayName": str(
                account.get(
                    "displayName"
                )
                or ""
            ),
            "emailAddress": str(
                account.get(
                    "emailAddress"
                )
                or ""
            ),
            "permissionId": str(
                account.get(
                    "permissionId"
                )
                or ""
            ),
            "photoLink": str(
                account.get(
                    "photoLink"
                )
                or ""
            ),
            "connectedAt": (
                utc_iso_now()
            ),
        },
    }

    ensure_profile_directories(
        profile_id
    )

    registry[
        "users"
    ].append(
        user
    )

    save_auth_registry(
        registry
    )

    return (
        find_auth_user_by_profile(
            profile_id
        ),
        True,
    )


def set_user_local_password(
    profile_id,
    new_password,
):
    profile_id = validate_profile_id(
        profile_id
    )

    new_password = (
        validate_auth_password(
            new_password
        )
    )

    registry = load_auth_registry()

    changed = False

    for index, user in enumerate(
        registry[
            "users"
        ]
    ):
        if user[
            "profileId"
        ] != profile_id:
            continue

        fresh = build_auth_config(
            user[
                "username"
            ],
            new_password,
        )

        registry[
            "users"
        ][
            index
        ] = {
            **user,
            "password": (
                fresh[
                    "password"
                ]
            ),
            "localPasswordEnabled": (
                True
            ),
            "passwordUpdatedAt": (
                utc_iso_now()
            ),
        }

        changed = True
        break

    if not changed:
        raise ValueError(
            "Perfil não encontrado."
        )

    save_auth_registry(
        registry
    )

    return find_auth_user_by_profile(
        profile_id
    )


def disable_user_local_password(
    profile_id,
):
    profile_id = validate_profile_id(
        profile_id
    )

    registry = load_auth_registry()

    changed = False

    for index, user in enumerate(
        registry[
            "users"
        ]
    ):
        if user[
            "profileId"
        ] != profile_id:
            continue

        registry[
            "users"
        ][
            index
        ] = {
            **user,
            "localPasswordEnabled": (
                False
            ),
            "passwordUpdatedAt": (
                utc_iso_now()
            ),
        }

        changed = True
        break

    if not changed:
        raise ValueError(
            "Perfil não encontrado."
        )

    save_auth_registry(
        registry
    )

    return find_auth_user_by_profile(
        profile_id
    )


def update_user_google_account(
    profile_id,
    account,
):
    profile_id = validate_profile_id(
        profile_id
    )

    account = (
        dict(account)
        if isinstance(
            account,
            dict,
        )
        else None
    )

    registry = load_auth_registry()

    permission_id = str(
        (
            account
            or {}
        ).get(
            "permissionId"
        )
        or ""
    ).strip()

    email = str(
        (
            account
            or {}
        ).get(
            "emailAddress"
        )
        or ""
    ).strip().casefold()

    if account and not (
        permission_id
        or email
    ):
        raise ValueError(
            "Não foi possível identificar a conta Google conectada."
        )

    if account:
        for user in registry[
            "users"
        ]:
            if user[
                "profileId"
            ] == profile_id:
                continue

            other = user.get(
                "googleAccount"
            )

            if not isinstance(
                other,
                dict,
            ):
                continue

            other_permission = str(
                other.get(
                    "permissionId"
                )
                or ""
            ).strip()

            other_email = str(
                other.get(
                    "emailAddress"
                )
                or ""
            ).strip().casefold()

            same_permission = bool(
                permission_id
                and other_permission
                and hmac.compare_digest(
                    permission_id,
                    other_permission,
                )
            )

            same_email = bool(
                email
                and other_email
                and hmac.compare_digest(
                    email,
                    other_email,
                )
            )

            if (
                same_permission
                or same_email
            ):
                raise ValueError(
                    "Esta conta Google já está associada ao usuário local "
                    f"“{user['username']}”. Use outra conta Google para manter "
                    "os dois usuários separados."
                )

    changed = False

    for index, user in enumerate(
        registry[
            "users"
        ]
    ):
        if user[
            "profileId"
        ] != profile_id:
            continue

        registry[
            "users"
        ][
            index
        ] = {
            **user,
            "authMode": (
                "google"
                if account
                else user.get(
                    "authMode"
                )
            ),
            "googleAccount": (
                {
                    "displayName": str(
                        account.get(
                            "displayName"
                        )
                        or ""
                    ),
                    "emailAddress": str(
                        account.get(
                            "emailAddress"
                        )
                        or ""
                    ),
                    "permissionId": str(
                        account.get(
                            "permissionId"
                        )
                        or ""
                    ),
                    "photoLink": str(
                        account.get(
                            "photoLink"
                        )
                        or ""
                    ),
                    "connectedAt": (
                        utc_iso_now()
                    ),
                }
                if account
                else None
            ),
        }
        changed = True
        break

    if not changed:
        raise ValueError(
            "Usuário local não encontrado."
        )

    save_auth_registry(
        registry
    )

    return find_auth_user_by_profile(
        profile_id
    )


def current_active_profile():
    with ACTIVE_PROFILE_LOCK:
        if not ACTIVE_PROFILE_ID:
            return None

        return {
            "profileId": (
                ACTIVE_PROFILE_ID
            ),
            "username": (
                ACTIVE_PROFILE_USERNAME
            ),
        }


def rebuild_google_drive_manager():
    global GOOGLE_DRIVE

    GOOGLE_DRIVE = GoogleDriveManager(
        client_config_file=(
            GOOGLE_OAUTH_CLIENT_FILE
        ),
        token_file=(
            GOOGLE_DRIVE_TOKEN_FILE
        ),
        protect_secret=(
            protect_local_secret
        ),
        unprotect_secret=(
            unprotect_local_secret
        ),
        atomic_write_json=(
            atomic_write_json
        ),
    )


def rebuild_cloud_sync_manager():
    global CLOUD_SYNC

    if (
        "CloudSyncManager"
        not in globals()
        or "load_settings"
        not in globals()
    ):
        return

    CLOUD_SYNC = CloudSyncManager(
        google_drive=GOOGLE_DRIVE,
        db_file=DB_FILE,
        settings_file=SETTINGS_FILE,
        pluggy_config_file=(
            PLUGGY_CONFIG_FILE
        ),
        key_file=(
            CLOUD_SYNC_KEY_FILE
        ),
        state_file=(
            CLOUD_SYNC_STATE_FILE
        ),
        protect_secret=(
            protect_local_secret
        ),
        unprotect_secret=(
            unprotect_local_secret
        ),
        atomic_write_json=(
            atomic_write_json
        ),
        load_settings=(
            load_settings
        ),
        normalize_settings=(
            normalize_settings
        ),
        save_settings=(
            save_settings
        ),
        load_pluggy_config=(
            load_pluggy_config
        ),
        save_pluggy_config=(
            save_pluggy_config
        ),
    )


def activate_profile(
    profile_id,
    username,
):
    global ACTIVE_PROFILE_ID
    global ACTIVE_PROFILE_USERNAME
    global DATA_DIR
    global USER_DATA_DIR
    global CONFIG_DIR
    global SETTINGS_FILE
    global PLUGGY_CONFIG_FILE
    global GOOGLE_DRIVE_TOKEN_FILE
    global CLOUD_SYNC_KEY_FILE
    global CLOUD_SYNC_STATE_FILE
    global DB_FILE
    global CLOUD_BACKGROUND_TIMER

    profile_id = validate_profile_id(
        profile_id
    )
    username = normalize_auth_username(
        username
    )

    with ACTIVE_PROFILE_LOCK:
        if (
            ACTIVE_PROFILE_ID
            == profile_id
        ):
            return

        sync_lock_acquired = (
            SYNC_LOCK.acquire(
                timeout=15
            )
        )

        if not sync_lock_acquired:
            raise RuntimeError(
                "Aguarde a sincronização atual terminar antes de trocar de usuário."
            )

        try:
            if (
                "CLOUD_BACKGROUND_TIMER"
                in globals()
                and CLOUD_BACKGROUND_TIMER
            ):
                try:
                    CLOUD_BACKGROUND_TIMER.cancel()
                except Exception:
                    pass
                CLOUD_BACKGROUND_TIMER = None

            paths = (
                ensure_profile_directories(
                    profile_id
                )
            )

            DATA_DIR = paths[
                "data"
            ]
            USER_DATA_DIR = paths[
                "user_data"
            ]
            CONFIG_DIR = paths[
                "config"
            ]

            SETTINGS_FILE = (
                USER_DATA_DIR
                / "ajustes.json"
            )
            PLUGGY_CONFIG_FILE = (
                CONFIG_DIR
                / "pluggy.json"
            )
            GOOGLE_DRIVE_TOKEN_FILE = (
                CONFIG_DIR
                / "google_drive_token.json"
            )
            CLOUD_SYNC_KEY_FILE = (
                CONFIG_DIR
                / "cloud_sync_key.json"
            )
            CLOUD_SYNC_STATE_FILE = (
                CONFIG_DIR
                / "cloud_sync_state.json"
            )
            DB_FILE = (
                DATA_DIR
                / SQLITE_DATABASE_NAME
            )

            ACTIVE_PROFILE_ID = (
                profile_id
            )
            ACTIVE_PROFILE_USERNAME = (
                username
            )

            rebuild_google_drive_manager()
            rebuild_cloud_sync_manager()

        finally:
            SYNC_LOCK.release()


def deactivate_profile():
    global ACTIVE_PROFILE_ID
    global ACTIVE_PROFILE_USERNAME
    global DATA_DIR
    global USER_DATA_DIR
    global CONFIG_DIR
    global SETTINGS_FILE
    global PLUGGY_CONFIG_FILE
    global GOOGLE_DRIVE_TOKEN_FILE
    global CLOUD_SYNC_KEY_FILE
    global CLOUD_SYNC_STATE_FILE
    global DB_FILE
    global CLOUD_BACKGROUND_TIMER

    with ACTIVE_PROFILE_LOCK:
        if (
            "CLOUD_BACKGROUND_TIMER"
            in globals()
            and CLOUD_BACKGROUND_TIMER
        ):
            try:
                CLOUD_BACKGROUND_TIMER.cancel()
            except Exception:
                pass
            CLOUD_BACKGROUND_TIMER = None

        DATA_DIR = (
            ROOT_DATA_DIR
        )
        USER_DATA_DIR = (
            ROOT_USER_DATA_DIR
        )
        CONFIG_DIR = (
            ROOT_CONFIG_DIR
        )
        SETTINGS_FILE = (
            USER_DATA_DIR
            / "ajustes.json"
        )
        PLUGGY_CONFIG_FILE = (
            CONFIG_DIR
            / "pluggy.json"
        )
        GOOGLE_DRIVE_TOKEN_FILE = (
            CONFIG_DIR
            / "google_drive_token.json"
        )
        CLOUD_SYNC_KEY_FILE = (
            CONFIG_DIR
            / "cloud_sync_key.json"
        )
        CLOUD_SYNC_STATE_FILE = (
            CONFIG_DIR
            / "cloud_sync_state.json"
        )
        DB_FILE = (
            DATA_DIR
            / SQLITE_DATABASE_NAME
        )

        ACTIVE_PROFILE_ID = None
        ACTIVE_PROFILE_USERNAME = None

        rebuild_google_drive_manager()
        rebuild_cloud_sync_manager()


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


def revoke_other_auth_sessions(
    profile_id,
):
    profile_id = validate_profile_id(
        profile_id
    )

    with AUTH_SESSIONS_LOCK:
        tokens = [
            token
            for token, session
            in AUTH_SESSIONS.items()
            if str(
                session.get(
                    "profileId"
                )
                or ""
            )
            != profile_id
        ]

        for token in tokens:
            AUTH_SESSIONS.pop(
                token,
                None,
            )


def create_auth_session(
    username,
    profile_id=None,
):
    purge_expired_auth_sessions()

    user = (
        find_auth_user(
            username
        )
        if profile_id is None
        else find_auth_user_by_profile(
            profile_id
        )
    )

    if not user:
        raise ValueError(
            "Usuário local não encontrado."
        )

    token = secrets.token_urlsafe(32)
    now = time.time()

    with AUTH_SESSIONS_LOCK:
        AUTH_SESSIONS[token] = {
            "username": (
                user[
                    "username"
                ]
            ),
            "profileId": (
                user[
                    "profileId"
                ]
            ),
            "createdAt": now,
            "expiresAt": (
                now
                + AUTH_SESSION_TTL_SECONDS
            ),
        }

    return token


def auth_session_identity(
    token,
):
    if not token:
        return None

    purge_expired_auth_sessions()

    with AUTH_SESSIONS_LOCK:
        session = AUTH_SESSIONS.get(
            token
        )

        if not session:
            return None

        username = str(
            session.get(
                "username"
            )
            or ""
        )

        profile_id = str(
            session.get(
                "profileId"
            )
            or ""
        )

        if (
            not username
            or not profile_id
        ):
            return None

        return {
            "username": username,
            "profileId": profile_id,
        }


def auth_session_username(token):
    identity = auth_session_identity(
        token
    )

    return (
        identity[
            "username"
        ]
        if identity
        else None
    )


def auth_request_identity(request):
    return auth_session_identity(
        request.cookies.get(
            AUTH_COOKIE_NAME
        )
    )


def auth_request_username(request):
    identity = auth_request_identity(
        request
    )

    return (
        identity[
            "username"
        ]
        if identity
        else None
    )


def revoke_auth_session(token):
    if not token:
        return

    with AUTH_SESSIONS_LOCK:
        AUTH_SESSIONS.pop(
            token,
            None,
        )


def purge_expired_google_pending_auth():
    now = time.time()

    with GOOGLE_PENDING_AUTH_LOCK:
        expired = [
            token
            for token, record
            in GOOGLE_PENDING_AUTH.items()
            if float(
                record.get(
                    "expiresAt"
                )
                or 0
            )
            <= now
        ]

        for token in expired:
            GOOGLE_PENDING_AUTH.pop(
                token,
                None,
            )


def create_google_pending_auth(
    user,
    *,
    new_profile=False,
):
    purge_expired_google_pending_auth()

    token = secrets.token_urlsafe(
        32
    )

    now = time.time()

    with GOOGLE_PENDING_AUTH_LOCK:
        GOOGLE_PENDING_AUTH[
            token
        ] = {
            "profileId": (
                user[
                    "profileId"
                ]
            ),
            "username": (
                user[
                    "username"
                ]
            ),
            "newProfile": bool(
                new_profile
            ),
            "createdAt": now,
            "expiresAt": (
                now
                + AUTH_GOOGLE_PENDING_TTL_SECONDS
            ),
        }

    return token


def google_pending_identity(
    token,
):
    if not token:
        return None

    purge_expired_google_pending_auth()

    with GOOGLE_PENDING_AUTH_LOCK:
        record = (
            GOOGLE_PENDING_AUTH.get(
                token
            )
        )

        if not record:
            return None

        return dict(
            record
        )


def consume_google_pending_auth(
    token,
):
    if not token:
        return None

    purge_expired_google_pending_auth()

    with GOOGLE_PENDING_AUTH_LOCK:
        record = (
            GOOGLE_PENDING_AUTH.pop(
                token,
                None,
            )
        )

    return (
        dict(record)
        if record
        else None
    )


def set_google_pending_cookie(
    response,
    token,
):
    response.set_cookie(
        key=(
            AUTH_GOOGLE_PENDING_COOKIE_NAME
        ),
        value=token,
        max_age=(
            AUTH_GOOGLE_PENDING_TTL_SECONDS
        ),
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
    )


def clear_google_pending_cookie(
    response,
):
    response.delete_cookie(
        key=(
            AUTH_GOOGLE_PENDING_COOKIE_NAME
        ),
        path="/",
        httponly=True,
        samesite="strict",
    )


def require_auth_api_identity(
    request,
):
    identity = auth_request_identity(
        request
    )

    if not identity:
        raise HTTPException(
            status_code=401,
            detail=(
                "Autenticação Google necessária."
            ),
        )

    active = current_active_profile()

    if (
        not active
        or active[
            "profileId"
        ]
        != identity[
            "profileId"
        ]
    ):
        activate_profile(
            identity[
                "profileId"
            ],
            identity[
                "username"
            ],
        )

    return identity


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


def merge_google_token_json(
    existing_json,
    incoming_json,
):
    """
    Preserva refresh_token já existente quando uma nova autorização Google
    retorna somente access_token. Isso evita perder sincronização persistente
    em logins subsequentes.
    """
    try:
        incoming = json.loads(
            incoming_json
            or "{}"
        )
    except Exception as exc:
        raise ValueError(
            "Token Google recebido é inválido."
        ) from exc

    if not isinstance(
        incoming,
        dict,
    ):
        raise ValueError(
            "Token Google recebido é inválido."
        )

    existing = {}

    if existing_json:
        try:
            parsed = json.loads(
                existing_json
            )

            if isinstance(
                parsed,
                dict,
            ):
                existing = parsed
        except Exception:
            existing = {}

    if (
        not incoming.get(
            "refresh_token"
        )
        and existing.get(
            "refresh_token"
        )
    ):
        incoming[
            "refresh_token"
        ] = existing[
            "refresh_token"
        ]

    return json.dumps(
        incoming,
        ensure_ascii=False,
    )


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
        identity = auth_request_identity(
            request
        )

        if not identity:
            return JSONResponse(
                status_code=401,
                content={
                    "detail": (
                        "Autenticação necessária."
                    )
                },
            )

        active = current_active_profile()

        if (
            not active
            or active[
                "profileId"
            ]
            != identity[
                "profileId"
            ]
        ):
            try:
                activate_profile(
                    identity[
                        "profileId"
                    ],
                    identity[
                        "username"
                    ],
                )
            except Exception as exc:
                return JSONResponse(
                    status_code=409,
                    content={
                        "detail": str(
                            exc
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
    registry = load_auth_registry()

    identity = auth_request_identity(
        request
    )

    if identity:
        try:
            activate_profile(
                identity[
                    "profileId"
                ],
                identity[
                    "username"
                ],
            )
        except Exception:
            identity = None

    pending = None

    if not identity:
        pending = google_pending_identity(
            request.cookies.get(
                AUTH_GOOGLE_PENDING_COOKIE_NAME
            )
        )

    google_login_status = (
        GOOGLE_LOGIN.status()
    )

    user = (
        find_auth_user_by_profile(
            identity[
                "profileId"
            ]
        )
        if identity
        else None
    )

    google_account = (
        user.get(
            "googleAccount"
        )
        if user
        and isinstance(
            user.get(
                "googleAccount"
            ),
            dict,
        )
        else None
    )

    return {
        "configured": bool(
            google_login_status.get(
                "clientConfigured"
            )
        ),
        "googleClientConfigured": bool(
            google_login_status.get(
                "clientConfigured"
            )
        ),
        "userCount": len(
            registry[
                "users"
            ]
        ),
        "authenticated": bool(
            identity
        ),
        "username": (
            identity[
                "username"
            ]
            if identity
            else None
        ),
        "profileId": (
            identity[
                "profileId"
            ]
            if identity
            else None
        ),
        "googleAccount": (
            google_account
        ),
        "passwordEnabled": bool(
            user_local_password_enabled(
                user
            )
            if user
            else False
        ),
        "passwordRequired": bool(
            pending
        ),
        "pendingUsername": (
            pending.get(
                "username"
            )
            if pending
            else None
        ),
        "pendingProfileId": (
            pending.get(
                "profileId"
            )
            if pending
            else None
        ),
        "passwordMinLength": (
            AUTH_PASSWORD_MIN_LENGTH
        ),
        "sessionTtlSeconds": (
            AUTH_SESSION_TTL_SECONDS
        ),
        "authMode": "google",
    }


@app.put("/api/auth/google/client-config")
async def auth_google_client_config(
    request: Request,
):
    """
    Fallback administrativo para desenvolvimento quando o build não traz
    google_oauth_client.bundled.json.
    """
    if GOOGLE_LOGIN.has_client_config():
        raise HTTPException(
            status_code=409,
            detail=(
                "O OAuth Client já está configurado. Alterações posteriores "
                "exigem uma sessão autenticada no ControleFin."
            ),
        )

    payload = await auth_json_payload(
        request
    )

    try:
        result = (
            GOOGLE_LOGIN
            .save_client_config(
                payload,
                clear_token=True,
            )
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    return {
        "ok": True,
        **result,
    }


@app.post("/api/auth/google/start")
def auth_google_start():
    if not GOOGLE_LOGIN.has_client_config():
        raise HTTPException(
            status_code=409,
            detail=(
                "OAuth Client do Google não está configurado neste build."
            ),
        )

    try:
        GOOGLE_LOGIN.cancel_connect()
        GOOGLE_LOGIN.clear_token()

        result = (
            GOOGLE_LOGIN
            .begin_connect(
                timeout_seconds=180,
                return_query=(
                    "google_auth=return"
                ),
                # Em computador compartilhado queremos sempre mostrar o seletor
                # de conta. consent garante refresh_token para um novo PC.
                prompt=(
                    "select_account consent"
                ),
            )
        )
    except Exception as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc

    return result


@app.post("/api/auth/google/complete")
def auth_google_complete(
    request: Request,
):
    login_status = (
        GOOGLE_LOGIN.status()
    )

    if login_status.get(
        "oauthPending"
    ):
        return JSONResponse(
            status_code=202,
            content={
                "ok": False,
                "pending": True,
                "message": (
                    "A autorização Google ainda está sendo concluída."
                ),
            },
        )

    if not login_status.get(
        "connected"
    ):
        detail = (
            login_status.get(
                "oauthError"
            )
            or "A autorização Google não foi concluída."
        )

        raise HTTPException(
            status_code=409,
            detail=str(
                detail
            ),
        )

    try:
        account = (
            GOOGLE_LOGIN
            .account_identity()
        )

        incoming_token = (
            GOOGLE_LOGIN
            .load_token_json()
        )

        if not incoming_token:
            raise RuntimeError(
                "O Google não retornou um token utilizável."
            )

        user = (
            find_auth_user_by_google_account(
                account
            )
        )

        new_profile = False

        if not user:
            user, new_profile = (
                create_google_user(
                    account
                )
            )

        # Uma instância local opera um perfil por vez.
        with AUTH_SESSIONS_LOCK:
            AUTH_SESSIONS.clear()

        with GOOGLE_PENDING_AUTH_LOCK:
            GOOGLE_PENDING_AUTH.clear()

        activate_profile(
            user[
                "profileId"
            ],
            user[
                "username"
            ],
        )

        existing_token = (
            GOOGLE_DRIVE
            .load_token_json()
        )

        GOOGLE_DRIVE.save_token_json(
            merge_google_token_json(
                existing_token,
                incoming_token,
            )
        )

        user = (
            update_user_google_account(
                user[
                    "profileId"
                ],
                account,
            )
        )

        GOOGLE_LOGIN.clear_token()

        account_payload = (
            user.get(
                "googleAccount"
            )
            or {}
        )

        if user_local_password_enabled(
            user
        ):
            pending_token = (
                create_google_pending_auth(
                    user,
                    new_profile=(
                        new_profile
                    ),
                )
            )

            response = JSONResponse(
                {
                    "ok": True,
                    "authenticated": False,
                    "passwordRequired": True,
                    "newProfile": bool(
                        new_profile
                    ),
                    "username": (
                        user[
                            "username"
                        ]
                    ),
                    "profileId": (
                        user[
                            "profileId"
                        ]
                    ),
                    "googleAccount": (
                        account_payload
                    ),
                }
            )

            clear_auth_cookie(
                response
            )

            set_google_pending_cookie(
                response,
                pending_token,
            )

            return response

        token = create_auth_session(
            user[
                "username"
            ],
            user[
                "profileId"
            ],
        )

        response = JSONResponse(
            {
                "ok": True,
                "authenticated": True,
                "passwordRequired": False,
                "newProfile": bool(
                    new_profile
                ),
                "username": (
                    user[
                        "username"
                    ]
                ),
                "profileId": (
                    user[
                        "profileId"
                    ]
                ),
                "googleAccount": (
                    account_payload
                ),
            }
        )

        set_auth_cookie(
            response,
            token,
        )

        clear_google_pending_cookie(
            response
        )

        return response

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "Não foi possível concluir o login Google: "
                f"{exc}"
            ),
        ) from exc


@app.post("/api/auth/password/verify")
async def auth_password_verify(
    request: Request,
):
    pending_token = request.cookies.get(
        AUTH_GOOGLE_PENDING_COOKIE_NAME
    )

    pending = google_pending_identity(
        pending_token
    )

    if not pending:
        raise HTTPException(
            status_code=401,
            detail=(
                "A autenticação Google expirou. Entre com o Google novamente."
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
                "Muitas tentativas de senha. "
                f"Tente novamente em {retry_after}s."
            ),
            headers={
                "Retry-After": str(
                    retry_after
                )
            },
        )

    payload = await auth_json_payload(
        request
    )

    user = find_auth_user_by_profile(
        pending[
            "profileId"
        ]
    )

    if (
        not user
        or not user_local_password_enabled(
            user
        )
        or not verify_auth_password(
            user,
            payload.get(
                "password"
            ),
        )
    ):
        register_auth_login_failure(
            client_key
        )

        raise HTTPException(
            status_code=401,
            detail=(
                "Senha adicional inválida."
            ),
        )

    clear_auth_login_failures(
        client_key
    )

    consume_google_pending_auth(
        pending_token
    )

    activate_profile(
        user[
            "profileId"
        ],
        user[
            "username"
        ],
    )

    token = create_auth_session(
        user[
            "username"
        ],
        user[
            "profileId"
        ],
    )

    response = JSONResponse(
        {
            "ok": True,
            "authenticated": True,
            "newProfile": bool(
                pending.get(
                    "newProfile"
                )
            ),
            "username": (
                user[
                    "username"
                ]
            ),
            "profileId": (
                user[
                    "profileId"
                ]
            ),
            "googleAccount": (
                user.get(
                    "googleAccount"
                )
            ),
        }
    )

    set_auth_cookie(
        response,
        token,
    )

    clear_google_pending_cookie(
        response
    )

    return response


@app.get("/api/auth/security")
def auth_security_status(
    request: Request,
):
    identity = (
        require_auth_api_identity(
            request
        )
    )

    user = find_auth_user_by_profile(
        identity[
            "profileId"
        ]
    )

    if not user:
        raise HTTPException(
            status_code=404,
            detail=(
                "Perfil local não encontrado."
            ),
        )

    return {
        "ok": True,
        "authMode": "google",
        "passwordEnabled": (
            user_local_password_enabled(
                user
            )
        ),
        "passwordMinLength": (
            AUTH_PASSWORD_MIN_LENGTH
        ),
        "googleAccount": (
            user.get(
                "googleAccount"
            )
        ),
    }


@app.put("/api/auth/security/password")
async def auth_security_set_password(
    request: Request,
):
    identity = (
        require_auth_api_identity(
            request
        )
    )

    payload = await auth_json_payload(
        request
    )

    new_password = str(
        payload.get(
            "newPassword"
        )
        or ""
    )

    confirm_password = str(
        payload.get(
            "confirmPassword"
        )
        or ""
    )

    if new_password != confirm_password:
        raise HTTPException(
            status_code=400,
            detail=(
                "As novas senhas não coincidem."
            ),
        )

    user = find_auth_user_by_profile(
        identity[
            "profileId"
        ]
    )

    if not user:
        raise HTTPException(
            status_code=404,
            detail=(
                "Perfil local não encontrado."
            ),
        )

    if user_local_password_enabled(
        user
    ):
        current_password = str(
            payload.get(
                "currentPassword"
            )
            or ""
        )

        if not verify_auth_password(
            user,
            current_password,
        ):
            raise HTTPException(
                status_code=401,
                detail=(
                    "Senha adicional atual inválida."
                ),
            )

    try:
        updated = set_user_local_password(
            user[
                "profileId"
            ],
            new_password,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    return {
        "ok": True,
        "passwordEnabled": True,
        "googleAccount": (
            updated.get(
                "googleAccount"
            )
        ),
    }


@app.delete("/api/auth/security/password")
async def auth_security_disable_password(
    request: Request,
):
    identity = (
        require_auth_api_identity(
            request
        )
    )

    payload = await auth_json_payload(
        request
    )

    user = find_auth_user_by_profile(
        identity[
            "profileId"
        ]
    )

    if not user:
        raise HTTPException(
            status_code=404,
            detail=(
                "Perfil local não encontrado."
            ),
        )

    if user_local_password_enabled(
        user
    ):
        if not verify_auth_password(
            user,
            payload.get(
                "currentPassword"
            ),
        ):
            raise HTTPException(
                status_code=401,
                detail=(
                    "Senha adicional atual inválida."
                ),
            )

    disable_user_local_password(
        user[
            "profileId"
        ]
    )

    return {
        "ok": True,
        "passwordEnabled": False,
    }


# Endpoints antigos preservados apenas para indicar claramente a mudança de
# arquitetura. Login local não concede mais acesso ao dashboard.
@app.post("/api/auth/setup")
async def auth_setup(request: Request):
    raise HTTPException(
        status_code=410,
        detail=(
            "Cadastro local desativado. Use 'Continuar com Google'."
        ),
    )


@app.post("/api/auth/login")
async def auth_login(request: Request):
    raise HTTPException(
        status_code=410,
        detail=(
            "Login local desativado. Use 'Continuar com Google'."
        ),
    )


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    revoke_auth_session(
        request.cookies.get(
            AUTH_COOKIE_NAME
        )
    )

    pending_token = request.cookies.get(
        AUTH_GOOGLE_PENDING_COOKIE_NAME
    )

    consume_google_pending_auth(
        pending_token
    )

    try:
        GOOGLE_LOGIN.cancel_connect()
        GOOGLE_LOGIN.clear_token()
    except Exception:
        pass

    with AUTH_SESSIONS_LOCK:
        has_sessions = bool(
            AUTH_SESSIONS
        )

    if not has_sessions:
        deactivate_profile()

    response = JSONResponse(
        {
            "ok": True,
        }
    )

    clear_auth_cookie(
        response
    )

    clear_google_pending_cookie(
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

        schedule_cloud_sync(
            "pluggy_config"
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

    schedule_cloud_sync(
        "pluggy_sync"
    )

    return {
        "ok": True,
        "errorCount": len(
            data.errors
        ),
        "database": database_status(),
    }


def active_user_record():
    active = current_active_profile()

    if not active:
        return None

    return find_auth_user_by_profile(
        active[
            "profileId"
        ]
    )


def google_status_for_active_user():
    status = GOOGLE_DRIVE.status()
    user = active_user_record()

    associated = (
        user.get(
            "googleAccount"
        )
        if user
        and isinstance(
            user.get(
                "googleAccount"
            ),
            dict,
        )
        else None
    )

    if status.get(
        "connected"
    ):
        try:
            account = (
                GOOGLE_DRIVE
                .account_identity()
            )

            if user:
                try:
                    current_account = (
                        user.get(
                            "googleAccount"
                        )
                        if isinstance(
                            user.get(
                                "googleAccount"
                            ),
                            dict,
                        )
                        else None
                    )

                    same_permission = bool(
                        current_account
                        and account.get(
                            "permissionId"
                        )
                        and current_account.get(
                            "permissionId"
                        )
                        == account.get(
                            "permissionId"
                        )
                    )

                    same_email = bool(
                        current_account
                        and account.get(
                            "emailAddress"
                        )
                        and str(
                            current_account.get(
                                "emailAddress"
                            )
                            or ""
                        ).casefold()
                        == str(
                            account.get(
                                "emailAddress"
                            )
                            or ""
                        ).casefold()
                    )

                    if not (
                        same_permission
                        or same_email
                    ):
                        updated = (
                            update_user_google_account(
                                user[
                                    "profileId"
                                ],
                                account,
                            )
                        )

                        associated = (
                            updated.get(
                                "googleAccount"
                            )
                        )

                except ValueError as exc:
                    # Não revogamos o OAuth inteiro, pois isso poderia afetar
                    # outro perfil que usa a mesma conta. Removemos só o token
                    # deste perfil.
                    GOOGLE_DRIVE.clear_token()
                    status = (
                        GOOGLE_DRIVE.status()
                    )
                    status[
                        "associationError"
                    ] = str(
                        exc
                    )

        except Exception as exc:
            status[
                "accountLookupError"
            ] = str(
                exc
            )

    status[
        "account"
    ] = associated

    if status.get("connected"):
        try:
            status["storage"] = (
                GOOGLE_DRIVE.cloud_storage_info(
                    create=True
                )
            )
        except Exception as exc:
            status["storageError"] = str(exc)

    return status


@app.get("/api/google/status")
def api_google_status():
    return google_status_for_active_user()


@app.put("/api/google/client-config")
async def api_google_client_config(
    request: Request,
):
    payload = await auth_json_payload(
        request
    )

    try:
        saved = GOOGLE_DRIVE.save_client_config(
            payload.get(
                "clientConfig"
            ),
            clear_token=True,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    return {
        "ok": True,
        **saved,
        "status": google_status_for_active_user(),
    }


@app.delete("/api/google/client-config")
def api_google_remove_client_config():
    GOOGLE_DRIVE.clear_client_config()

    return {
        "ok": True,
        "status": google_status_for_active_user(),
    }


@app.post("/api/google/connect/start")
def api_google_connect_start():
    if not GOOGLE_DRIVE.has_client_config():
        raise HTTPException(
            status_code=409,
            detail=(
                "A configuração OAuth do aplicativo não está disponível. "
                "Inclua o OAuth Client compartilhado do ControleFin no build."
            ),
        )

    try:
        return GOOGLE_DRIVE.begin_connect()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "Não foi possível iniciar o OAuth do Google: "
                f"{exc}"
            ),
        ) from exc


@app.post("/api/google/connect")
def api_google_connect_compat():
    # Compatibilidade com versões anteriores do frontend.
    return api_google_connect_start()


@app.post("/api/google/connect/cancel")
def api_google_connect_cancel():
    GOOGLE_DRIVE.cancel_connect()

    return {
        "ok": True,
        "status": google_status_for_active_user(),
    }


@app.post("/api/google/test")
def api_google_test():
    if not GOOGLE_DRIVE.status().get(
        "connected"
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Google Drive ainda não conectado."
            ),
        )

    try:
        result = GOOGLE_DRIVE.test_access()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Não foi possível acessar a pasta ControleFin "
                f"no Google Drive: {exc}"
            ),
        ) from exc

    return {
        "ok": True,
        **result,
    }


@app.get("/api/google/drive/files")
def api_google_drive_files():
    if not GOOGLE_DRIVE.status().get(
        "connected"
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Google Drive ainda não conectado."
            ),
        )

    try:
        files = GOOGLE_DRIVE.list_files()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Falha ao listar a pasta ControleFin/Snapshots: "
                f"{exc}"
            ),
        ) from exc

    return {
        "ok": True,
        "space": "drive",
        "folder": GOOGLE_DRIVE.cloud_storage_info(create=True),
        "files": files,
        "count": len(files),
    }


@app.post("/api/google/disconnect")
def api_google_disconnect():
    user = active_user_record()

    result = GOOGLE_DRIVE.disconnect(
        revoke=True
    )

    if user:
        update_user_google_account(
            user[
                "profileId"
            ],
            None,
        )

    return {
        **result,
        "status": google_status_for_active_user(),
    }


@app.get("/api/google/cloud/status")
def api_google_cloud_status():
    google_status = google_status_for_active_user()
    include_remote = bool(
        google_status.get(
            "connected"
        )
    )

    return {
        "google": google_status,
        "cloud": CLOUD_SYNC.status(
            include_remote=include_remote
        ),
    }


@app.put("/api/google/cloud/passphrase")
async def api_google_cloud_passphrase(
    request: Request,
):
    payload = await auth_json_payload(request)
    passphrase = payload.get("passphrase")

    if not GOOGLE_DRIVE.status().get("connected"):
        raise HTTPException(
            status_code=409,
            detail=(
                "Conecte o Google Drive antes de configurar a senha da nuvem."
            ),
        )

    try:
        saved = CLOUD_SYNC.save_passphrase(
            passphrase,
            verify_remote=True,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc

    return {
        "ok": True,
        **saved,
        "cloud": CLOUD_SYNC.status(
            include_remote=True
        ),
    }


@app.post("/api/google/cloud/restore")
async def api_google_cloud_restore(
    request: Request,
):
    """
    Fluxo seguro de primeiro acesso / novo computador.

    Este endpoint é somente de RESTAURAÇÃO:
    - exige Google conectado;
    - exige que já exista snapshot remoto;
    - valida a senha da nuvem;
    - baixa e aplica o snapshot mais recente;
    - nunca cria/upload um snapshot vazio quando não há backup remoto.
    """
    payload = await auth_json_payload(
        request
    )

    passphrase = payload.get(
        "passphrase"
    )

    if not GOOGLE_DRIVE.status().get(
        "connected"
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Conecte sua conta Google antes de restaurar os dados."
            ),
        )

    try:
        remote = (
            CLOUD_SYNC.remote_status()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Não foi possível verificar os backups no Google Drive: "
                f"{exc}"
            ),
        ) from exc

    if not remote.get(
        "available"
    ):
        raise HTTPException(
            status_code=404,
            detail=(
                "Nenhum backup do ControleFin foi encontrado nesta conta Google."
            ),
        )

    try:
        # Verifica a senha contra o snapshot remoto ANTES de persistir.
        CLOUD_SYNC.save_passphrase(
            passphrase,
            verify_remote=True,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc

    result = run_cloud_operation(
        lambda: CLOUD_SYNC.download(
            force=True
        )
    )

    return {
        "ok": True,
        **result,
        "remote": remote,
        "pluggy": pluggy_public_status(),
        "database": database_status(),
    }


@app.delete("/api/google/cloud/passphrase")
def api_google_cloud_clear_passphrase():
    CLOUD_SYNC.clear_passphrase()
    return {
        "ok": True,
        "cloud": CLOUD_SYNC.status(
            include_remote=False
        ),
    }


def run_cloud_operation(operation):
    if not GOOGLE_DRIVE.status().get("connected"):
        raise HTTPException(
            status_code=409,
            detail="Google Drive não conectado.",
        )

    if not CLOUD_SYNC.key_configured():
        raise HTTPException(
            status_code=409,
            detail=(
                "Configure a senha da nuvem antes de sincronizar."
            ),
        )

    if not SYNC_LOCK.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail=(
                "Aguarde a sincronização Pluggy em andamento terminar."
            ),
        )

    try:
        return operation()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "Falha na sincronização com o Google Drive: "
                f"{exc}"
            ),
        ) from exc
    finally:
        SYNC_LOCK.release()


@app.post("/api/google/cloud/sync")
def api_google_cloud_sync():
    return run_cloud_operation(
        CLOUD_SYNC.smart_sync
    )


@app.post("/api/google/cloud/upload")
async def api_google_cloud_upload(
    request: Request,
):
    payload = await auth_json_payload(request)
    force = bool(payload.get("force"))
    return run_cloud_operation(
        lambda: CLOUD_SYNC.upload(
            force=force
        )
    )


@app.post("/api/google/cloud/download")
async def api_google_cloud_download(
    request: Request,
):
    payload = await auth_json_payload(request)
    force = bool(payload.get("force"))
    return run_cloud_operation(
        lambda: CLOUD_SYNC.download(
            force=force
        )
    )


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

    schedule_cloud_sync(
        "settings"
    )

    return {
        "ok": True,
        "settingsFile": str(SETTINGS_FILE),
    }


# Bootstrap manager. Após o login ele é reconstruído apontando para o perfil
# autenticado.
CLOUD_SYNC = CloudSyncManager(
    google_drive=GOOGLE_DRIVE,
    db_file=DB_FILE,
    settings_file=SETTINGS_FILE,
    pluggy_config_file=PLUGGY_CONFIG_FILE,
    key_file=CLOUD_SYNC_KEY_FILE,
    state_file=CLOUD_SYNC_STATE_FILE,
    protect_secret=protect_local_secret,
    unprotect_secret=unprotect_local_secret,
    atomic_write_json=atomic_write_json,
    load_settings=load_settings,
    normalize_settings=normalize_settings,
    save_settings=save_settings,
    load_pluggy_config=load_pluggy_config,
    save_pluggy_config=save_pluggy_config,
)

CLOUD_BACKGROUND_LOCK = threading.RLock()
CLOUD_BACKGROUND_TIMER = None


def _cloud_operation_available():
    google_status = GOOGLE_DRIVE.status()
    return bool(
        google_status.get("connected")
        and CLOUD_SYNC.key_configured()
    )


def _run_cloud_smart_sync_background():
    global CLOUD_BACKGROUND_TIMER

    with CLOUD_BACKGROUND_LOCK:
        CLOUD_BACKGROUND_TIMER = None

    if not _cloud_operation_available():
        return

    if not SYNC_LOCK.acquire(blocking=False):
        schedule_cloud_sync(
            "retry_after_pluggy",
            mark_dirty=False,
            delay_seconds=5.0,
        )
        return

    try:
        CLOUD_SYNC.smart_sync()
    except Exception:
        # CloudSyncManager already records the failure in local sync state.
        return
    finally:
        SYNC_LOCK.release()


def schedule_cloud_sync(
    reason,
    *,
    mark_dirty=True,
    delay_seconds=2.0,
):
    global CLOUD_BACKGROUND_TIMER

    if mark_dirty:
        CLOUD_SYNC.mark_dirty(str(reason))

    if not _cloud_operation_available():
        return

    with CLOUD_BACKGROUND_LOCK:
        if CLOUD_BACKGROUND_TIMER:
            try:
                CLOUD_BACKGROUND_TIMER.cancel()
            except Exception:
                pass

        timer = threading.Timer(
            float(delay_seconds),
            _run_cloud_smart_sync_background,
        )
        timer.daemon = True
        CLOUD_BACKGROUND_TIMER = timer
        timer.start()




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

    # Em instalações antigas, o .env pertencia ao único usuário existente.
    # Migre-o para o layout legado ANTES de criar o primeiro perfil; a etapa
    # seguinte copiará pluggy.json para o perfil correto.
    legacy_env_migrated = False

    if os.name == "nt":
        try:
            legacy_env_migrated = bool(
                migrate_legacy_env()
            )
        except Exception as exc:
            print(
                "Aviso: não foi possível migrar automaticamente "
                f"o .env legado: {exc}",
                file=sys.stderr,
                flush=True,
            )

    migrated_profile = False

    try:
        migrated_profile = (
            migrate_single_user_auth_to_registry()
        )
    except Exception as exc:
        print(
            "Aviso: não foi possível migrar o usuário legado para perfis: "
            f"{exc}",
            file=sys.stderr,
            flush=True,
        )

    print(
        f"Pasta do aplicativo: {BASE_DIR}",
        flush=True,
    )
    print(
        f"Dados pessoais: {STORAGE_DIR}",
        flush=True,
    )
    print(
        "Bancos SQLite: isolados por usuário em "
        f"{PROFILES_DIR}",
        flush=True,
    )

    if migrated_files:
        print(
            "Arquivos locais antigos copiados para "
            f"{STORAGE_DIR}.",
            flush=True,
        )

    if migrated_profile:
        print(
            "Instalação single-user migrada para perfil isolado.",
            flush=True,
        )

    if legacy_env_migrated:
        print(
            "Credenciais Pluggy do .env foram migradas para o perfil legado "
            "com proteção Windows DPAPI; o .env foi removido.",
            flush=True,
        )

    if ACTIVE_PROFILE_ID and DB_FILE.exists():
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
