from __future__ import annotations

import io
import json
import html as html_lib
import secrets
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


GOOGLE_DRIVE_FILE_SCOPE = (
    "https://www.googleapis.com/auth/drive.file"
)
# Mantido apenas para importar snapshots criados pelas versões antigas.
# Novos arquivos são gravados exclusivamente no espaço visível "drive".
GOOGLE_DRIVE_LEGACY_APPDATA_SCOPE = (
    "https://www.googleapis.com/auth/drive.appdata"
)
GOOGLE_DRIVE_SCOPE = GOOGLE_DRIVE_FILE_SCOPE
GOOGLE_DRIVE_SCOPES = [
    GOOGLE_DRIVE_FILE_SCOPE,
    GOOGLE_DRIVE_LEGACY_APPDATA_SCOPE,
]
GOOGLE_DRIVE_TOKEN_VERSION = 1

GOOGLE_DRIVE_FOLDER_MIME = (
    "application/vnd.google-apps.folder"
)
CONTROLEFIN_DRIVE_FOLDER_NAME = "ControleFin"
CONTROLEFIN_SNAPSHOTS_FOLDER_NAME = "Snapshots"
CONTROLEFIN_INDEX_FILE_NAME = "controlefin-state.json"
CONTROLEFIN_ROOT_ROLE = "controlefin_root"
CONTROLEFIN_SNAPSHOTS_ROLE = "controlefin_snapshots"


def utc_now_iso() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat(
        timespec="seconds"
    ).replace(
        "+00:00",
        "Z",
    )


def validate_google_oauth_client_config(
    payload: Any,
) -> dict[str, Any]:
    """Accept only a Google OAuth Client of type Desktop app."""
    if not isinstance(
        payload,
        dict,
    ):
        raise ValueError(
            "credentials.json inválido."
        )

    installed = payload.get(
        "installed"
    )

    if not isinstance(
        installed,
        dict,
    ):
        if "web" in payload:
            raise ValueError(
                "Use um OAuth Client do tipo Desktop app, não Web application."
            )

        raise ValueError(
            "O arquivo precisa conter uma credencial OAuth do tipo Desktop app."
        )

    client_id = str(
        installed.get(
            "client_id"
        )
        or ""
    ).strip()

    auth_uri = str(
        installed.get(
            "auth_uri"
        )
        or ""
    ).strip()

    token_uri = str(
        installed.get(
            "token_uri"
        )
        or ""
    ).strip()

    redirect_uris = (
        installed.get(
            "redirect_uris"
        )
        or []
    )

    if not client_id:
        raise ValueError(
            "OAuth Client ID ausente."
        )

    if not client_id.endswith(
        ".apps.googleusercontent.com"
    ):
        raise ValueError(
            "OAuth Client ID do Google inválido."
        )

    if not auth_uri.startswith(
        "https://accounts.google.com/"
    ):
        raise ValueError(
            "auth_uri do Google inválido."
        )

    if not token_uri.startswith(
        "https://oauth2.googleapis.com/"
    ):
        raise ValueError(
            "token_uri do Google inválido."
        )

    if (
        not isinstance(
            redirect_uris,
            list,
        )
        or not any(
            str(uri).startswith(
                (
                    "http://localhost",
                    "http://127.0.0.1",
                )
            )
            for uri in redirect_uris
        )
    ):
        raise ValueError(
            "A credencial Desktop precisa permitir redirect de loopback localhost."
        )

    return payload


def escape_drive_query_literal(
    value: str,
) -> str:
    return (
        str(value)
        .replace(
            "\\",
            "\\\\",
        )
        .replace(
            "'",
            "\\'",
        )
    )


class GoogleDriveManager:
    def __init__(
        self,
        *,
        client_config_file: Path,
        token_file: Path,
        protect_secret: Callable[
            [str],
            dict[str, Any],
        ],
        unprotect_secret: Callable[
            [dict[str, Any]],
            str,
        ],
        atomic_write_json: Callable[
            [Path, Any],
            None,
        ],
    ) -> None:
        self.client_config_file = Path(
            client_config_file
        )
        self.token_file = Path(
            token_file
        )
        self.protect_secret = (
            protect_secret
        )
        self.unprotect_secret = (
            unprotect_secret
        )
        self.atomic_write_json = (
            atomic_write_json
        )

        self._oauth_lock = threading.RLock()
        self._oauth_pending = None
        self._oauth_last_error = None
        self._storage_lock = threading.RLock()
        self._storage_cache = None

    @staticmethod
    def _google_imports():
        try:
            from google.auth.transport.requests import (
                Request as GoogleAuthRequest,
            )
            from google.oauth2.credentials import (
                Credentials,
            )
            from google_auth_oauthlib.flow import (
                InstalledAppFlow,
            )
            from googleapiclient.discovery import (
                build,
            )
            from googleapiclient.http import (
                MediaIoBaseDownload,
                MediaIoBaseUpload,
            )
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Bibliotecas do Google Drive não instaladas. "
                "Execute: python -m pip install -r requirements.txt"
            ) from exc

        return {
            "GoogleAuthRequest": (
                GoogleAuthRequest
            ),
            "Credentials": Credentials,
            "InstalledAppFlow": (
                InstalledAppFlow
            ),
            "build": build,
            "MediaIoBaseDownload": (
                MediaIoBaseDownload
            ),
            "MediaIoBaseUpload": (
                MediaIoBaseUpload
            ),
        }

    def has_client_config(
        self,
    ) -> bool:
        return (
            self.client_config_file.exists()
        )

    def load_client_config(
        self,
    ) -> dict[str, Any]:
        if not self.client_config_file.exists():
            raise RuntimeError(
                "OAuth Client do Google ainda não configurado."
            )

        try:
            payload = json.loads(
                self.client_config_file.read_text(
                    encoding="utf-8"
                )
            )
        except Exception as exc:
            raise RuntimeError(
                "O arquivo OAuth do Google está inválido."
            ) from exc

        return (
            validate_google_oauth_client_config(
                payload
            )
        )

    def save_client_config(
        self,
        payload: Any,
        *,
        clear_token: bool = True,
    ) -> dict[str, Any]:
        validated = (
            validate_google_oauth_client_config(
                payload
            )
        )

        self.client_config_file.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.atomic_write_json(
            self.client_config_file,
            validated,
        )

        if clear_token:
            self.clear_token()

        return {
            "configured": True,
            "clientId": (
                validated[
                    "installed"
                ]["client_id"]
            ),
        }

    def clear_client_config(
        self,
    ) -> None:
        self.clear_token()

        try:
            self.client_config_file.unlink()
        except FileNotFoundError:
            pass

    def _load_token_envelope(
        self,
    ) -> Optional[dict[str, Any]]:
        if not self.token_file.exists():
            return None

        try:
            payload = json.loads(
                self.token_file.read_text(
                    encoding="utf-8"
                )
            )
        except Exception as exc:
            raise RuntimeError(
                "O token local do Google Drive está corrompido."
            ) from exc

        if (
            not isinstance(
                payload,
                dict,
            )
            or int(
                payload.get("version")
                or 0
            )
            != GOOGLE_DRIVE_TOKEN_VERSION
            or not isinstance(
                payload.get("protected"),
                dict,
            )
        ):
            raise RuntimeError(
                "Formato do token Google não suportado."
            )

        return payload

    def save_token_json(
        self,
        token_json: str,
    ) -> None:
        token_json = str(
            token_json
            or ""
        ).strip()

        if not token_json:
            raise ValueError(
                "Token Google vazio."
            )

        try:
            parsed = json.loads(
                token_json
            )
        except Exception as exc:
            raise ValueError(
                "Token Google inválido."
            ) from exc

        if not isinstance(
            parsed,
            dict,
        ):
            raise ValueError(
                "Token Google inválido."
            )

        envelope = {
            "version": (
                GOOGLE_DRIVE_TOKEN_VERSION
            ),
            "protected": (
                self.protect_secret(
                    token_json
                )
            ),
            "updatedAt": utc_now_iso(),
        }

        self.token_file.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.atomic_write_json(
            self.token_file,
            envelope,
        )

    def load_token_json(
        self,
    ) -> Optional[str]:
        envelope = (
            self._load_token_envelope()
        )

        if not envelope:
            return None

        return self.unprotect_secret(
            envelope["protected"]
        )

    def clear_token(
        self,
    ) -> None:
        try:
            self.token_file.unlink()
        except FileNotFoundError:
            pass

        with self._storage_lock:
            self._storage_cache = None

    @staticmethod
    def _token_scopes_from_info(
        token_info: Any,
    ) -> list[str]:
        if not isinstance(token_info, dict):
            return []

        raw = token_info.get("scopes")
        if isinstance(raw, str):
            return [
                item
                for item in raw.split()
                if item
            ]

        if isinstance(raw, list):
            return [
                str(item)
                for item in raw
                if str(item).strip()
            ]

        return []

    def status(
        self,
    ) -> dict[str, Any]:
        client_id = ""
        client_configured = False

        if self.has_client_config():
            try:
                config = self.load_client_config()
                client_id = str(
                    config["installed"]["client_id"]
                )
                client_configured = True
            except Exception:
                client_configured = False

        token_exists = self.token_file.exists()
        token_readable = False
        token_updated_at = None
        token_scopes: list[str] = []

        if token_exists:
            try:
                envelope = self._load_token_envelope()
                token_json = self.load_token_json()
                token_info = json.loads(token_json or "{}")
                token_scopes = self._token_scopes_from_info(token_info)
                token_readable = True
                token_updated_at = (
                    envelope.get("updatedAt")
                    if envelope
                    else None
                )
            except Exception:
                token_readable = False

        drive_file_granted = (
            GOOGLE_DRIVE_FILE_SCOPE
            in token_scopes
        )
        legacy_scope_granted = (
            GOOGLE_DRIVE_LEGACY_APPDATA_SCOPE
            in token_scopes
        )

        with self._oauth_lock:
            pending = self._oauth_pending
            oauth_pending = bool(
                pending
                and pending.get("status") == "PENDING"
            )
            oauth_started_at = (
                pending.get("startedAt")
                if pending
                else None
            )
            oauth_error = self._oauth_last_error

        return {
            "clientConfigured": client_configured,
            "clientId": client_id,
            "connected": bool(
                client_configured
                and token_readable
                and drive_file_granted
            ),
            "tokenStored": token_exists,
            "tokenReadable": token_readable,
            "tokenUpdatedAt": token_updated_at,
            "tokenScopes": token_scopes,
            "requiresReauthorization": bool(
                token_readable
                and not drive_file_granted
            ),
            "scope": GOOGLE_DRIVE_FILE_SCOPE,
            "scopes": list(GOOGLE_DRIVE_SCOPES),
            "legacyAppDataScopeGranted": legacy_scope_granted,
            "space": "drive",
            "folderName": CONTROLEFIN_DRIVE_FOLDER_NAME,
            "oauthPending": oauth_pending,
            "oauthStartedAt": oauth_started_at,
            "oauthError": oauth_error,
        }

    def _clear_pending_oauth(
        self,
        *,
        error: Optional[str] = None,
    ) -> None:
        with self._oauth_lock:
            pending = self._oauth_pending

            if pending:
                server = pending.get(
                    "server"
                )
                if server is not None:
                    try:
                        server.server_close()
                    except Exception:
                        pass

            self._oauth_pending = None
            self._oauth_last_error = (
                str(error)
                if error
                else None
            )

    def cancel_connect(
        self,
    ) -> None:
        self._clear_pending_oauth(
            error=None
        )

    def begin_connect(
        self,
        *,
        timeout_seconds: int = 180,
        return_query: str = "google_oauth=return",
        prompt: str = "consent",
    ) -> dict[str, Any]:
        """
        Inicia OAuth sem bloquear a request do dashboard.

        O dashboard recebe a authorizationUrl imediatamente e abre a janela do
        Google por conta própria. Um HTTPServer temporário recebe o callback de
        loopback em 127.0.0.1:<porta aleatória>.
        """
        imports = (
            self._google_imports()
        )

        allowed_return_queries = {
            "google_oauth=return",
            "google_auth=return",
        }

        if return_query not in allowed_return_queries:
            raise ValueError(
                "Destino de retorno OAuth inválido."
            )

        prompt = str(
            prompt
            or "consent"
        ).strip()

        if prompt not in {
            "consent",
            "select_account",
            "select_account consent",
        }:
            raise ValueError(
                "Prompt OAuth inválido."
            )

        client_config = (
            self.load_client_config()
        )

        with self._oauth_lock:
            if (
                self._oauth_pending
                and self._oauth_pending.get(
                    "status"
                )
                == "PENDING"
            ):
                raise RuntimeError(
                    "Já existe uma autorização Google em andamento."
                )

            self._oauth_last_error = None

        flow = imports[
            "InstalledAppFlow"
        ].from_client_config(
            client_config,
            GOOGLE_DRIVE_SCOPES,
        )

        manager = self
        callback_result = {
            "query": None,
        }

        class OAuthCallbackHandler(
            BaseHTTPRequestHandler
        ):
            def log_message(
                self,
                format,
                *args,
            ):
                return

            def do_GET(self):
                parsed = urllib.parse.urlsplit(
                    self.path
                )

                callback_result[
                    "query"
                ] = urllib.parse.parse_qs(
                    parsed.query,
                    keep_blank_values=True,
                )

                success = (
                    "code"
                    in callback_result["query"]
                    and "error"
                    not in callback_result[
                        "query"
                    ]
                )

                title = (
                    "ControleFin conectado"
                    if success
                    else "ControleFin - autorização não concluída"
                )

                message = (
                    "Autorização recebida. Você será levado de volta ao ControleFin automaticamente."
                    if success
                    else "A autorização não foi concluída. Volte ao ControleFin para ver os detalhes."
                )

                body = (
                    "<!doctype html>"
                    "<html lang='pt-BR'>"
                    "<meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                    f"<title>{html_lib.escape(title)}</title>"
                    "<body style='margin:0;background:#0b0b0b;color:#fff;"
                    "font:16px system-ui;display:grid;place-items:center;"
                    "min-height:100vh;padding:24px;box-sizing:border-box'>"
                    "<main style='max-width:520px;text-align:center'>"
                    "<h1 style='font-size:24px'>ControleFin</h1>"
                    f"<p style='color:#b3b3b3;line-height:1.55'>{html_lib.escape(message)}</p>"
                    "</main>"
                    "<script>setTimeout(()=>{window.location.replace('http://127.0.0.1:8765/?"
                    + html_lib.escape(
                        return_query,
                        quote=True,
                    )
                    + "');},1800);</script>"
                    "</body></html>"
                ).encode(
                    "utf-8"
                )

                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "text/html; charset=utf-8",
                )
                self.send_header(
                    "Cache-Control",
                    "no-store",
                )
                self.send_header(
                    "Content-Length",
                    str(len(body)),
                )
                self.end_headers()
                self.wfile.write(
                    body
                )

        callback_server = HTTPServer(
            (
                "127.0.0.1",
                0,
            ),
            OAuthCallbackHandler,
        )

        callback_server.timeout = max(
            1,
            int(timeout_seconds),
        )

        callback_port = int(
            callback_server.server_port
        )

        redirect_uri = (
            f"http://127.0.0.1:"
            f"{callback_port}/"
        )

        flow.redirect_uri = (
            redirect_uri
        )

        authorization_url, state = (
            flow.authorization_url(
                access_type="offline",
                prompt=prompt,
                include_granted_scopes="true",
            )
        )

        pending = {
            "status": "PENDING",
            "state": state,
            "flow": flow,
            "server": callback_server,
            "startedAt": utc_now_iso(),
            "redirectUri": redirect_uri,
        }

        with self._oauth_lock:
            self._oauth_pending = (
                pending
            )

        def worker():
            error = None

            try:
                callback_server.handle_request()

                query = (
                    callback_result.get(
                        "query"
                    )
                    or {}
                )

                if not query:
                    raise TimeoutError(
                        "A autorização Google expirou antes do callback."
                    )

                returned_state = str(
                    (
                        query.get(
                            "state"
                        )
                        or [""]
                    )[0]
                )

                if not secrets.compare_digest(
                    str(state),
                    returned_state,
                ):
                    raise RuntimeError(
                        "State OAuth inválido."
                    )

                oauth_error = str(
                    (
                        query.get(
                            "error"
                        )
                        or [""]
                    )[0]
                ).strip()

                if oauth_error:
                    raise RuntimeError(
                        "Google recusou/cancelou a autorização: "
                        + oauth_error
                    )

                code = str(
                    (
                        query.get(
                            "code"
                        )
                        or [""]
                    )[0]
                ).strip()

                if not code:
                    raise RuntimeError(
                        "Callback OAuth não trouxe o código de autorização."
                    )

                flow.fetch_token(
                    code=code
                )

                credentials = (
                    flow.credentials
                )

                self.save_token_json(
                    credentials.to_json()
                )

                # Faz uma chamada real ao Drive antes de marcar como concluído.
                self.test_access()

            except Exception as exc:
                error = str(
                    exc
                )
            finally:
                try:
                    callback_server.server_close()
                except Exception:
                    pass

                self._clear_pending_oauth(
                    error=error
                )

        thread = threading.Thread(
            target=worker,
            name="controlefin-google-oauth",
            daemon=True,
        )
        thread.start()

        return {
            "ok": True,
            "authorizationUrl": (
                authorization_url
            ),
            "redirectUri": (
                redirect_uri
            ),
            "expiresInSeconds": (
                int(timeout_seconds)
            ),
        }

    # Mantido como alias para código antigo. Não abre navegador e não bloqueia.
    def connect_interactive(
        self,
    ) -> dict[str, Any]:
        return self.begin_connect()

    def _credentials(
        self,
    ):
        imports = self._google_imports()
        token_json = self.load_token_json()

        if not token_json:
            raise RuntimeError(
                "Google Drive ainda não conectado."
            )

        try:
            token_info = json.loads(token_json)
        except Exception as exc:
            raise RuntimeError(
                "Token Google inválido."
            ) from exc

        token_scopes = self._token_scopes_from_info(token_info)

        if GOOGLE_DRIVE_FILE_SCOPE not in token_scopes:
            raise RuntimeError(
                "A autorização Google precisa ser atualizada para permitir "
                "a pasta visível ControleFin. Conecte sua conta novamente."
            )

        credentials = imports["Credentials"].from_authorized_user_info(
            token_info,
            token_scopes,
        )

        if credentials.expired and credentials.refresh_token:
            credentials.refresh(
                imports["GoogleAuthRequest"]()
            )
            self.save_token_json(
                credentials.to_json()
            )

        if not credentials.valid:
            raise RuntimeError(
                "A autorização do Google Drive expirou. Conecte novamente."
            )

        return credentials

    def service(
        self,
    ):
        imports = (
            self._google_imports()
        )

        return imports[
            "build"
        ](
            "drive",
            "v3",
            credentials=self._credentials(),
            cache_discovery=False,
        )

    def account_identity(
        self,
    ) -> dict[str, Any]:
        """
        Retorna a identidade da conta Google ligada ao token atual.

        Drive API v3 expõe o usuário autenticado em about.user.
        permissionId é usado como identificador estável para evitar que dois
        perfis locais sejam ligados silenciosamente ao mesmo Drive.
        """
        response = (
            self.service()
            .about()
            .get(
                fields=(
                    "user("
                    "displayName,"
                    "emailAddress,"
                    "permissionId,"
                    "photoLink,"
                    "me"
                    ")"
                )
            )
            .execute()
        )

        user = (
            response.get("user")
            or {}
        )

        if not isinstance(
            user,
            dict,
        ):
            user = {}

        return {
            "displayName": str(
                user.get(
                    "displayName"
                )
                or ""
            ),
            "emailAddress": str(
                user.get(
                    "emailAddress"
                )
                or ""
            ),
            "permissionId": str(
                user.get(
                    "permissionId"
                )
                or ""
            ),
            "photoLink": str(
                user.get(
                    "photoLink"
                )
                or ""
            ),
            "me": bool(
                user.get(
                    "me"
                )
            ),
        }

    def _drive_file_fields(self) -> str:
        return (
            "id,name,mimeType,parents,"
            "modifiedTime,size,version,md5Checksum,"
            "appProperties,webViewLink"
        )

    def _list_drive_query(
        self,
        *,
        query: str,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        service = self.service()
        files: list[dict[str, Any]] = []
        page_token = None

        while True:
            response = (
                service.files()
                .list(
                    spaces="drive",
                    q=query,
                    pageSize=max(
                        1,
                        min(int(page_size), 1000),
                    ),
                    pageToken=page_token,
                    fields=(
                        "nextPageToken,files("
                        + self._drive_file_fields()
                        + ")"
                    ),
                )
                .execute()
            )

            files.extend(
                response.get("files", [])
            )
            page_token = response.get(
                "nextPageToken"
            )
            if not page_token:
                break

        return files

    def _find_child(
        self,
        *,
        parent_id: str,
        name: str,
        mime_type: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        safe_parent = escape_drive_query_literal(parent_id)
        safe_name = escape_drive_query_literal(name)
        parts = [
            f"'{safe_parent}' in parents",
            f"name = '{safe_name}'",
            "trashed = false",
        ]
        if mime_type:
            parts.append(
                "mimeType = '"
                + escape_drive_query_literal(mime_type)
                + "'"
            )

        files = self._list_drive_query(
            query=" and ".join(parts),
            page_size=20,
        )
        if not files:
            return None

        files.sort(
            key=lambda item: str(
                item.get("modifiedTime") or ""
            ),
            reverse=True,
        )
        return files[0]

    def _create_folder(
        self,
        *,
        name: str,
        parent_id: str,
        role: str,
    ) -> dict[str, Any]:
        return (
            self.service()
            .files()
            .create(
                body={
                    "name": str(name),
                    "mimeType": GOOGLE_DRIVE_FOLDER_MIME,
                    "parents": [str(parent_id)],
                    "appProperties": {
                        "controlefinRole": str(role),
                        "controlefinVersion": "1",
                    },
                },
                fields=self._drive_file_fields(),
            )
            .execute()
        )

    def ensure_cloud_storage(
        self,
    ) -> dict[str, Any]:
        with self._storage_lock:
            if self._storage_cache:
                return dict(self._storage_cache)

            root = self._find_child(
                parent_id="root",
                name=CONTROLEFIN_DRIVE_FOLDER_NAME,
                mime_type=GOOGLE_DRIVE_FOLDER_MIME,
            )

            if not root:
                root = self._create_folder(
                    name=CONTROLEFIN_DRIVE_FOLDER_NAME,
                    parent_id="root",
                    role=CONTROLEFIN_ROOT_ROLE,
                )

            snapshots = self._find_child(
                parent_id=str(root["id"]),
                name=CONTROLEFIN_SNAPSHOTS_FOLDER_NAME,
                mime_type=GOOGLE_DRIVE_FOLDER_MIME,
            )

            if not snapshots:
                snapshots = self._create_folder(
                    name=CONTROLEFIN_SNAPSHOTS_FOLDER_NAME,
                    parent_id=str(root["id"]),
                    role=CONTROLEFIN_SNAPSHOTS_ROLE,
                )

            root_id = str(root["id"])
            snapshots_id = str(snapshots["id"])

            result = {
                "rootFolderId": root_id,
                "rootFolderName": CONTROLEFIN_DRIVE_FOLDER_NAME,
                "snapshotsFolderId": snapshots_id,
                "snapshotsFolderName": CONTROLEFIN_SNAPSHOTS_FOLDER_NAME,
                "webViewLink": (
                    root.get("webViewLink")
                    or f"https://drive.google.com/drive/folders/{root_id}"
                ),
                "space": "drive",
            }
            self._storage_cache = dict(result)
            return result

    def cloud_storage_info(
        self,
        *,
        create: bool = True,
    ) -> Optional[dict[str, Any]]:
        if create:
            return self.ensure_cloud_storage()

        with self._storage_lock:
            return (
                dict(self._storage_cache)
                if self._storage_cache
                else None
            )

    def test_access(
        self,
    ) -> dict[str, Any]:
        storage = self.ensure_cloud_storage()
        safe_parent = escape_drive_query_literal(
            storage["snapshotsFolderId"]
        )
        response = (
            self.service()
            .files()
            .list(
                spaces="drive",
                q=(
                    f"'{safe_parent}' in parents "
                    "and trashed = false"
                ),
                pageSize=1,
                fields="files(id,name),nextPageToken",
            )
            .execute()
        )

        return {
            "ok": True,
            "space": "drive",
            "folder": storage,
            "visibleFilePreviewCount": len(
                response.get("files", [])
            ),
        }

    def list_files(
        self,
        *,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        storage = self.ensure_cloud_storage()
        safe_parent = escape_drive_query_literal(
            storage["snapshotsFolderId"]
        )
        return self._list_drive_query(
            query=(
                f"'{safe_parent}' in parents "
                "and trashed = false"
            ),
            page_size=page_size,
        )

    def find_file(
        self,
        name: str,
    ) -> Optional[dict[str, Any]]:
        storage = self.ensure_cloud_storage()
        return self._find_child(
            parent_id=storage["snapshotsFolderId"],
            name=name,
        )

    def create_bytes(
        self,
        *,
        name: str,
        data: bytes,
        mime_type: str = "application/octet-stream",
        app_properties: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        imports = self._google_imports()
        storage = self.ensure_cloud_storage()
        body: dict[str, Any] = {
            "name": str(name),
            "parents": [storage["snapshotsFolderId"]],
        }

        if app_properties:
            body["appProperties"] = {
                str(key): str(value)
                for key, value in app_properties.items()
                if value is not None
            }

        media = imports["MediaIoBaseUpload"](
            io.BytesIO(bytes(data)),
            mimetype=mime_type,
            resumable=False,
        )

        return (
            self.service()
            .files()
            .create(
                body=body,
                media_body=media,
                fields=self._drive_file_fields(),
            )
            .execute()
        )

    def upsert_bytes(
        self,
        *,
        name: str,
        data: bytes,
        mime_type: str = "application/octet-stream",
        app_properties: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        imports = self._google_imports()
        service = self.service()
        existing = self.find_file(name)
        media = imports["MediaIoBaseUpload"](
            io.BytesIO(bytes(data)),
            mimetype=mime_type,
            resumable=False,
        )

        body = None
        if app_properties:
            body = {
                "appProperties": {
                    str(key): str(value)
                    for key, value in app_properties.items()
                    if value is not None
                }
            }

        if existing:
            return (
                service.files()
                .update(
                    fileId=existing["id"],
                    body=body,
                    media_body=media,
                    fields=self._drive_file_fields(),
                )
                .execute()
            )

        return self.create_bytes(
            name=name,
            data=data,
            mime_type=mime_type,
            app_properties=app_properties,
        )

    def write_state_index(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        imports = self._google_imports()
        service = self.service()
        storage = self.ensure_cloud_storage()
        existing = self._find_child(
            parent_id=storage["rootFolderId"],
            name=CONTROLEFIN_INDEX_FILE_NAME,
            mime_type="application/json",
        )
        data = (
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        media = imports["MediaIoBaseUpload"](
            io.BytesIO(data),
            mimetype="application/json",
            resumable=False,
        )
        body = {
            "appProperties": {
                "kind": "controlefin_index",
                "formatVersion": "1",
            }
        }

        if existing:
            return (
                service.files()
                .update(
                    fileId=existing["id"],
                    body=body,
                    media_body=media,
                    fields=self._drive_file_fields(),
                )
                .execute()
            )

        return (
            service.files()
            .create(
                body={
                    "name": CONTROLEFIN_INDEX_FILE_NAME,
                    "parents": [storage["rootFolderId"]],
                    **body,
                },
                media_body=media,
                fields=self._drive_file_fields(),
            )
            .execute()
        )

    def list_legacy_appdata_files(
        self,
        *,
        page_size: int = 1000,
    ) -> list[dict[str, Any]]:
        status = self.status()
        if not status.get("legacyAppDataScopeGranted"):
            return []

        service = self.service()
        files: list[dict[str, Any]] = []
        page_token = None

        while True:
            response = (
                service.files()
                .list(
                    spaces="appDataFolder",
                    q="trashed = false",
                    pageSize=max(1, min(int(page_size), 1000)),
                    pageToken=page_token,
                    fields=(
                        "nextPageToken,files("
                        "id,name,mimeType,modifiedTime,size,"
                        "version,md5Checksum,appProperties"
                        ")"
                    ),
                )
                .execute()
            )
            files.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return files


    def download_bytes(
        self,
        *,
        file_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> bytes:
        if not file_id:
            if not name:
                raise ValueError(
                    "Informe file_id ou name."
                )

            file_info = (
                self.find_file(
                    name
                )
            )

            if not file_info:
                raise FileNotFoundError(
                    name
                )

            file_id = str(
                file_info["id"]
            )

        imports = (
            self._google_imports()
        )

        request = (
            self.service()
            .files()
            .get_media(
                fileId=file_id
            )
        )

        buffer = io.BytesIO()

        downloader = imports[
            "MediaIoBaseDownload"
        ](
            buffer,
            request,
        )

        done = False

        while not done:
            _, done = (
                downloader.next_chunk()
            )

        return buffer.getvalue()

    def delete_file(
        self,
        file_id: str,
    ) -> None:
        (
            self.service()
            .files()
            .delete(
                fileId=file_id
            )
            .execute()
        )

    def disconnect(
        self,
        *,
        revoke: bool = True,
    ) -> dict[str, Any]:
        self.cancel_connect()

        revoked = False
        revoke_error = None

        if (
            revoke
            and self.token_file.exists()
        ):
            try:
                token_json = (
                    self.load_token_json()
                )

                token_info = json.loads(
                    token_json
                    or "{}"
                )

                token = (
                    token_info.get(
                        "refresh_token"
                    )
                    or token_info.get(
                        "token"
                    )
                )

                if token:
                    body = (
                        urllib.parse.urlencode(
                            {
                                "token": token,
                            }
                        ).encode(
                            "utf-8"
                        )
                    )

                    request = (
                        urllib.request.Request(
                            "https://oauth2.googleapis.com/revoke",
                            data=body,
                            headers={
                                "Content-Type": (
                                    "application/"
                                    "x-www-form-urlencoded"
                                )
                            },
                            method="POST",
                        )
                    )

                    with urllib.request.urlopen(
                        request,
                        timeout=10,
                    ) as response:
                        revoked = (
                            200
                            <= int(
                                response.status
                            )
                            < 300
                        )
            except Exception as exc:
                revoke_error = str(
                    exc
                )

        self.clear_token()

        return {
            "ok": True,
            "connected": False,
            "revoked": revoked,
            "revokeError": (
                revoke_error
            ),
        }
