from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
import threading
import uuid
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


CLOUD_FORMAT = "ControleFinEncryptedCloudState"
CLOUD_FORMAT_VERSION = 1
CLOUD_PAYLOAD_FORMAT = "ControleFinCloudPayload"
CLOUD_PAYLOAD_VERSION = 1
CLOUD_KIND = "controlefin_state"
CLOUD_MIME_TYPE = "application/octet-stream"
CLOUD_SNAPSHOT_PREFIX = "controlefin-state-v1-r"
CLOUD_KDF_ITERATIONS = 600_000
CLOUD_MIN_PASSPHRASE = 12
CLOUD_MAX_PASSPHRASE = 256
CLOUD_HISTORY_LIMIT = 10

REQUIRED_DATABASE_TABLES = {
    "accounts",
    "transactions",
    "dataset_catalog",
    "manifest",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def parse_utc_timestamp(value: Any) -> Optional[float]:
    raw = str(value or "").strip()
    if not raw:
        return None

    try:
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError):
        return None


def epoch_to_utc_iso(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None

    return (
        datetime.fromtimestamp(float(value), tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def encode_b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def decode_b64(value: str) -> bytes:
    return base64.urlsafe_b64decode(str(value).encode("ascii"))


def validate_cloud_passphrase(value: Any) -> str:
    passphrase = str(value or "")
    if not CLOUD_MIN_PASSPHRASE <= len(passphrase) <= CLOUD_MAX_PASSPHRASE:
        raise ValueError(
            "A senha da nuvem deve ter entre "
            f"{CLOUD_MIN_PASSPHRASE} e {CLOUD_MAX_PASSPHRASE} caracteres."
        )
    return passphrase


def derive_cloud_key(
    passphrase: str,
    *,
    salt: bytes,
    iterations: int = CLOUD_KDF_ITERATIONS,
) -> bytes:
    passphrase = validate_cloud_passphrase(passphrase)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=bytes(salt),
        iterations=int(iterations),
    )
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt_cloud_payload(
    *,
    payload: bytes,
    passphrase: str,
    revision: int,
    device_id: str,
    created_at: Optional[str] = None,
) -> bytes:
    passphrase = validate_cloud_passphrase(passphrase)
    created_at = created_at or utc_now_iso()
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)

    metadata = {
        "format": CLOUD_FORMAT,
        "formatVersion": CLOUD_FORMAT_VERSION,
        "revision": int(revision),
        "deviceId": str(device_id),
        "createdAtUtc": created_at,
        "kdf": {
            "name": "PBKDF2-HMAC-SHA256",
            "iterations": CLOUD_KDF_ITERATIONS,
            "salt": encode_b64(salt),
        },
        "cipher": {
            "name": "AES-256-GCM",
            "nonce": encode_b64(nonce),
        },
    }

    aad = canonical_json_bytes(metadata)
    key = derive_cloud_key(
        passphrase,
        salt=salt,
        iterations=CLOUD_KDF_ITERATIONS,
    )
    ciphertext = AESGCM(key).encrypt(nonce, bytes(payload), aad)

    envelope = {
        **metadata,
        "ciphertext": encode_b64(ciphertext),
    }
    return canonical_json_bytes(envelope)


def decrypt_cloud_payload(
    *,
    encrypted: bytes,
    passphrase: str,
) -> tuple[dict[str, Any], bytes]:
    passphrase = validate_cloud_passphrase(passphrase)
    try:
        envelope = json.loads(bytes(encrypted).decode("utf-8"))
    except Exception as exc:
        raise ValueError("Snapshot da nuvem inválido.") from exc

    if not isinstance(envelope, dict):
        raise ValueError("Snapshot da nuvem inválido.")

    if (
        envelope.get("format") != CLOUD_FORMAT
        or int(envelope.get("formatVersion") or 0) != CLOUD_FORMAT_VERSION
    ):
        raise ValueError("Formato de snapshot da nuvem não suportado.")

    kdf_data = envelope.get("kdf")
    cipher_data = envelope.get("cipher")
    if not isinstance(kdf_data, dict) or not isinstance(cipher_data, dict):
        raise ValueError("Metadados criptográficos inválidos.")

    try:
        salt = decode_b64(kdf_data["salt"])
        iterations = int(kdf_data["iterations"])
        nonce = decode_b64(cipher_data["nonce"])
        ciphertext = decode_b64(envelope["ciphertext"])
    except Exception as exc:
        raise ValueError("Snapshot criptografado inválido.") from exc

    metadata = {key: value for key, value in envelope.items() if key != "ciphertext"}
    key = derive_cloud_key(passphrase, salt=salt, iterations=iterations)

    try:
        plaintext = AESGCM(key).decrypt(
            nonce,
            ciphertext,
            canonical_json_bytes(metadata),
        )
    except Exception as exc:
        raise ValueError(
            "Não foi possível descriptografar o snapshot. "
            "Verifique a senha da nuvem."
        ) from exc

    return metadata, plaintext


def safe_zip_member_name(value: str) -> str:
    raw = str(value or "").replace("\\", "/")
    if (
        not raw
        or raw.startswith("/")
        or raw.startswith("../")
        or "/../" in raw
        or raw.endswith("/..")
    ):
        raise ValueError("Snapshot contém caminho inválido.")
    return raw


def validate_sqlite_file(path: Path) -> None:
    path = Path(path)
    if not path.exists():
        raise ValueError("Banco SQLite ausente no snapshot.")

    try:
        with closing(sqlite3.connect(path, timeout=5)) as connection:
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
            if not quick_check or str(quick_check[0]).lower() != "ok":
                raise ValueError("PRAGMA quick_check falhou.")

            rows = connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                """
            ).fetchall()
            tables = {str(row[0]) for row in rows}
            missing = REQUIRED_DATABASE_TABLES - tables
            if missing:
                raise ValueError(
                    "Banco SQLite incompleto. Tabela(s) ausente(s): "
                    + ", ".join(sorted(missing))
                )
    except sqlite3.DatabaseError as exc:
        raise ValueError("Banco SQLite inválido no snapshot.") from exc


class CloudSyncManager:
    def __init__(
        self,
        *,
        google_drive: Any,
        db_file: Path,
        settings_file: Path,
        pluggy_config_file: Path,
        key_file: Path,
        state_file: Path,
        protect_secret: Callable[[str], dict[str, Any]],
        unprotect_secret: Callable[[dict[str, Any]], str],
        atomic_write_json: Callable[[Path, Any], None],
        load_settings: Callable[[], dict[str, Any]],
        normalize_settings: Callable[[Any], dict[str, Any]],
        save_settings: Callable[[dict[str, Any]], None],
        load_pluggy_config: Callable[..., Optional[dict[str, Any]]],
        save_pluggy_config: Callable[..., dict[str, Any]],
    ) -> None:
        self.google_drive = google_drive
        self.db_file = Path(db_file)
        self.settings_file = Path(settings_file)
        self.pluggy_config_file = Path(pluggy_config_file)
        self.key_file = Path(key_file)
        self.state_file = Path(state_file)
        self.protect_secret = protect_secret
        self.unprotect_secret = unprotect_secret
        self.atomic_write_json = atomic_write_json
        self.load_settings = load_settings
        self.normalize_settings = normalize_settings
        self.save_settings = save_settings
        self.load_pluggy_config = load_pluggy_config
        self.save_pluggy_config = save_pluggy_config
        self._lock = threading.RLock()

    def _default_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "deviceId": str(uuid.uuid4()),
            "lastAppliedRevision": 0,
            "lastRemoteFileId": None,
            "dirty": False,
            "dirtyReason": None,
            "lastSyncAt": None,
            "lastAction": None,
            "lastError": None,
            "lastLocalSignature": None,
            "lastLocalModifiedAt": None,
            "lastRemoteModifiedTime": None,
            "localChangeAt": None,
            "syncPolicy": "NEWEST_WINS",
            "legacyAppDataMigratedAt": None,
            "legacyAppDataMigratedCount": 0,
        }

    def load_state(self) -> dict[str, Any]:
        with self._lock:
            if not self.state_file.exists():
                state = self._default_state()
                self.atomic_write_json(self.state_file, state)
                return state

            try:
                state = json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RuntimeError(
                    "Estado local da sincronização Google Drive está corrompido."
                ) from exc

            if not isinstance(state, dict):
                raise RuntimeError(
                    "Estado local da sincronização Google Drive está inválido."
                )

            if not state.get("deviceId"):
                state["deviceId"] = str(uuid.uuid4())

            defaults = self._default_state()
            defaults["deviceId"] = state["deviceId"]
            defaults.update(state)
            return defaults

    def save_state(self, state: dict[str, Any]) -> None:
        with self._lock:
            self.atomic_write_json(self.state_file, state)

    def _update_state(self, **changes: Any) -> dict[str, Any]:
        state = self.load_state()
        state.update(changes)
        self.save_state(state)
        return state

    def mark_dirty(self, reason: str) -> dict[str, Any]:
        return self._update_state(
            dirty=True,
            dirtyReason=str(reason) or "local_change",
            localChangeAt=utc_now_iso(),
            lastError=None,
        )

    def local_state_info(self) -> dict[str, Any]:
        """Return the newest local timestamp for DB/settings/Pluggy."""
        tracked = (
            ("database", self.db_file),
            ("settings", self.settings_file),
            ("pluggy", self.pluggy_config_file),
        )

        rows = []
        newest_epoch = None
        newest_name = None

        for name, path in tracked:
            path = Path(path)
            if not path.exists():
                continue

            try:
                stat = path.stat()
            except OSError:
                continue

            modified = float(stat.st_mtime)
            rows.append(
                {
                    "name": name,
                    "size": int(stat.st_size),
                    "mtimeNs": int(stat.st_mtime_ns),
                }
            )

            if newest_epoch is None or modified > newest_epoch:
                newest_epoch = modified
                newest_name = name

        return {
            "signature": sha256_hex(canonical_json_bytes(rows)),
            "modifiedEpoch": newest_epoch,
            "modifiedTime": epoch_to_utc_iso(newest_epoch),
            "newestSource": newest_name,
            "fileCount": len(rows),
        }

    def _remote_modified_epoch(
        self,
        item: dict[str, Any],
    ) -> Optional[float]:
        return parse_utc_timestamp(
            item.get("modifiedTime")
            or item.get("snapshotCreatedAt")
        )

    def _latest_remote_by_time(
        self,
        snapshots: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not snapshots:
            raise FileNotFoundError(
                "Nenhum snapshot do ControleFin existe no Google Drive."
            )

        def key(item: dict[str, Any]):
            remote_time = self._remote_modified_epoch(item)
            return (
                float(remote_time if remote_time is not None else -1),
                int(item.get("revision") or 0),
                str(item.get("id") or ""),
            )

        return max(snapshots, key=key)

    def key_configured(self) -> bool:
        if not self.key_file.exists():
            return False
        try:
            self.load_passphrase()
            return True
        except Exception:
            return False

    def save_passphrase(
        self,
        passphrase: str,
        *,
        verify_remote: bool = True,
    ) -> dict[str, Any]:
        passphrase = validate_cloud_passphrase(passphrase)

        if verify_remote:
            snapshots = self.remote_snapshots()
            if snapshots:
                tip = self._latest_remote_tip_allowing_branch(snapshots)
                encrypted = self.google_drive.download_bytes(file_id=tip["id"])
                decrypt_cloud_payload(
                    encrypted=encrypted,
                    passphrase=passphrase,
                )

        payload = {
            "version": 1,
            "protected": self.protect_secret(passphrase),
            "updatedAt": utc_now_iso(),
        }
        self.atomic_write_json(self.key_file, payload)
        return {
            "configured": True,
            "updatedAt": payload["updatedAt"],
        }

    def clear_passphrase(self) -> None:
        try:
            self.key_file.unlink()
        except FileNotFoundError:
            pass

    def load_passphrase(self) -> str:
        if not self.key_file.exists():
            raise RuntimeError("Senha da nuvem ainda não configurada.")

        try:
            payload = json.loads(self.key_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(
                "Configuração da senha da nuvem está corrompida."
            ) from exc

        if not isinstance(payload, dict) or not isinstance(
            payload.get("protected"), dict
        ):
            raise RuntimeError("Configuração da senha da nuvem está inválida.")

        return validate_cloud_passphrase(
            self.unprotect_secret(payload["protected"])
        )

    def _remote_snapshot_from_file(
        self,
        item: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        props = item.get("appProperties")
        if not isinstance(props, dict):
            return None
        if props.get("kind") != CLOUD_KIND:
            return None
        if str(props.get("formatVersion") or "") != str(CLOUD_FORMAT_VERSION):
            return None

        try:
            revision = int(props.get("revision"))
        except (TypeError, ValueError):
            return None

        if revision <= 0:
            return None

        return {
            **item,
            "revision": revision,
            "snapshotDeviceId": str(props.get("deviceId") or ""),
            "snapshotCreatedAt": str(props.get("createdAtUtc") or ""),
        }

    def _write_remote_index(
        self,
        snapshot: dict[str, Any],
    ) -> None:
        writer = getattr(
            self.google_drive,
            "write_state_index",
            None,
        )
        if not callable(writer):
            return

        try:
            writer(
                {
                    "format": "ControleFinVisibleDriveIndex",
                    "formatVersion": 1,
                    "latestRevision": int(
                        snapshot.get("revision") or 0
                    ),
                    "latestFileId": snapshot.get("id"),
                    "latestFileName": snapshot.get("name"),
                    "latestModifiedTime": (
                        snapshot.get("modifiedTime")
                        or snapshot.get("snapshotCreatedAt")
                    ),
                    "updatedAtUtc": utc_now_iso(),
                    "note": (
                        "Os arquivos .bin em Snapshots são criptografados "
                        "pelo ControleFin."
                    ),
                }
            )
        except Exception:
            # O índice é somente auxiliar/visível; nunca invalida um snapshot.
            return

    def migrate_legacy_appdata_if_needed(
        self,
        current_items: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        lister = getattr(
            self.google_drive,
            "list_legacy_appdata_files",
            None,
        )
        if not callable(lister):
            return {
                "migrated": False,
                "count": 0,
            }

        current_items = (
            list(current_items)
            if current_items is not None
            else self.google_drive.list_files(page_size=1000)
        )
        current_snapshots = [
            snapshot
            for item in current_items
            if isinstance(item, dict)
            for snapshot in [self._remote_snapshot_from_file(item)]
            if snapshot
        ]

        if current_snapshots:
            return {
                "migrated": False,
                "count": 0,
            }

        try:
            legacy_items = lister(page_size=1000)
        except Exception:
            return {
                "migrated": False,
                "count": 0,
            }

        legacy_snapshots = [
            snapshot
            for item in legacy_items
            if isinstance(item, dict)
            for snapshot in [self._remote_snapshot_from_file(item)]
            if snapshot
        ]

        if not legacy_snapshots:
            return {
                "migrated": False,
                "count": 0,
            }

        legacy_snapshots.sort(
            key=lambda item: (
                int(item.get("revision") or 0),
                str(item.get("modifiedTime") or ""),
            )
        )
        selected = legacy_snapshots[-CLOUD_HISTORY_LIMIT:]
        copied = []

        for item in selected:
            encrypted = self.google_drive.download_bytes(
                file_id=str(item["id"])
            )
            created = self.google_drive.create_bytes(
                name=str(item.get("name") or self._snapshot_filename(
                    revision=int(item["revision"]),
                    device_id=str(item.get("snapshotDeviceId") or "legacy"),
                )),
                data=encrypted,
                mime_type=str(
                    item.get("mimeType")
                    or CLOUD_MIME_TYPE
                ),
                app_properties=dict(
                    item.get("appProperties")
                    or {}
                ),
            )
            migrated = self._remote_snapshot_from_file(created)
            if migrated:
                copied.append(migrated)

        if copied:
            latest = self._latest_remote_by_time(copied)
            self._write_remote_index(latest)
            self._update_state(
                legacyAppDataMigratedAt=utc_now_iso(),
                legacyAppDataMigratedCount=len(copied),
            )

        return {
            "migrated": bool(copied),
            "count": len(copied),
        }

    def remote_snapshots(self) -> list[dict[str, Any]]:
        items = self.google_drive.list_files(page_size=1000)
        snapshots: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            snapshot = self._remote_snapshot_from_file(item)
            if snapshot:
                snapshots.append(snapshot)

        if not snapshots:
            migration = self.migrate_legacy_appdata_if_needed(items)
            if migration.get("migrated"):
                items = self.google_drive.list_files(page_size=1000)
                snapshots = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    snapshot = self._remote_snapshot_from_file(item)
                    if snapshot:
                        snapshots.append(snapshot)

        snapshots.sort(
            key=lambda item: (
                int(item.get("revision") or 0),
                str(item.get("modifiedTime") or ""),
            ),
            reverse=True,
        )
        return snapshots

    def _unique_remote_tip(
        self,
        snapshots: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not snapshots:
            raise FileNotFoundError(
                "Nenhum snapshot do ControleFin existe no Google Drive."
            )

        max_revision = max(int(item["revision"]) for item in snapshots)
        tips = [
            item
            for item in snapshots
            if int(item["revision"]) == max_revision
        ]
        if len(tips) != 1:
            raise RuntimeError(
                "Foram encontrados múltiplos snapshots na mesma revisão "
                f"{max_revision}. Isso indica um conflito simultâneo entre dispositivos."
            )
        return tips[0]

    def _latest_remote_tip_allowing_branch(
        self,
        snapshots: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self._latest_remote_by_time(snapshots)

    def remote_status(self) -> dict[str, Any]:
        snapshots = self.remote_snapshots()

        if not snapshots:
            return {
                "available": False,
                "revision": 0,
                "snapshotCount": 0,
                "conflict": False,
                "modifiedTime": None,
            }

        latest = self._latest_remote_by_time(snapshots)

        return {
            "available": True,
            "revision": int(latest.get("revision") or 0),
            "maxRevision": max(
                int(item.get("revision") or 0)
                for item in snapshots
            ),
            "snapshotCount": len(snapshots),
            "conflict": False,
            "modifiedTime": (
                latest.get("modifiedTime")
                or latest.get("snapshotCreatedAt")
            ),
            "deviceId": latest.get("snapshotDeviceId"),
            "fileId": latest.get("id"),
            "fileName": latest.get("name"),
        }

    def status(self, *, include_remote: bool = False) -> dict[str, Any]:
        state = self.load_state()
        local_info = self.local_state_info()

        result = {
            "keyConfigured": self.key_configured(),
            "deviceId": state["deviceId"],
            "lastAppliedRevision": int(state.get("lastAppliedRevision") or 0),
            "dirty": bool(state.get("dirty")),
            "dirtyReason": state.get("dirtyReason"),
            "lastSyncAt": state.get("lastSyncAt"),
            "lastAction": state.get("lastAction"),
            "lastError": state.get("lastError"),
            "syncPolicy": "NEWEST_WINS",
            "localModifiedTime": local_info.get("modifiedTime"),
            "localNewestSource": local_info.get("newestSource"),
            "localSignatureChanged": bool(
                state.get("lastLocalSignature")
                and state.get("lastLocalSignature")
                != local_info.get("signature")
            ),
            "remote": None,
        }

        if include_remote:
            try:
                result["remote"] = self.remote_status()
            except Exception as exc:
                result["remoteError"] = str(exc)
        return result

    def _consistent_database_bytes(self) -> Optional[bytes]:
        if not self.db_file.exists():
            return None

        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        temp_fd, temp_name = tempfile.mkstemp(
            prefix=".cloud-db-",
            suffix=".sqlite",
            dir=str(self.db_file.parent),
        )
        os.close(temp_fd)
        temp_path = Path(temp_name)

        try:
            with closing(sqlite3.connect(self.db_file, timeout=10)) as source:
                with closing(sqlite3.connect(temp_path, timeout=10)) as destination:
                    source.backup(destination)
            validate_sqlite_file(temp_path)
            return temp_path.read_bytes()
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def _portable_pluggy_config(self) -> Optional[dict[str, Any]]:
        if not self.pluggy_config_file.exists():
            return None

        config = self.load_pluggy_config(include_secret=True)
        if not config:
            return None

        return {
            "version": 1,
            "clientId": str(config.get("clientId") or ""),
            "clientSecret": str(config.get("clientSecret") or ""),
            "itemIds": list(config.get("itemIds") or []),
        }

    def build_plain_payload(
        self,
        *,
        revision: int,
        device_id: str,
    ) -> bytes:
        files: dict[str, bytes] = {}

        database_bytes = self._consistent_database_bytes()
        if database_bytes is not None:
            files["data/controlefin.db"] = database_bytes

        settings = self.normalize_settings(self.load_settings())
        files["user_data/ajustes.json"] = (
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")

        pluggy = self._portable_pluggy_config()
        if pluggy is not None:
            files["config/pluggy_portable.json"] = (
                json.dumps(pluggy, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8")

        file_manifest = [
            {
                "path": path,
                "size": len(content),
                "sha256": sha256_hex(content),
            }
            for path, content in sorted(files.items())
        ]

        manifest = {
            "format": CLOUD_PAYLOAD_FORMAT,
            "formatVersion": CLOUD_PAYLOAD_VERSION,
            "revision": int(revision),
            "deviceId": str(device_id),
            "createdAtUtc": utc_now_iso(),
            "databasePresent": database_bytes is not None,
            "settingsPresent": True,
            "pluggyConfigured": pluggy is not None,
            "files": file_manifest,
        }

        buffer = io.BytesIO()
        with zipfile.ZipFile(
            buffer,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            archive.writestr("manifest.json", canonical_json_bytes(manifest))
            for path, content in files.items():
                archive.writestr(path, content)
        return buffer.getvalue()

    def _decode_plain_payload(
        self,
        *,
        payload: bytes,
        expected_revision: int,
        expected_device_id: str,
    ) -> dict[str, Any]:
        try:
            archive = zipfile.ZipFile(io.BytesIO(payload), mode="r")
        except Exception as exc:
            raise ValueError("Conteúdo interno do snapshot é inválido.") from exc

        with archive:
            names = {safe_zip_member_name(name) for name in archive.namelist()}
            if "manifest.json" not in names:
                raise ValueError("manifest.json ausente no snapshot.")

            try:
                manifest = json.loads(
                    archive.read("manifest.json").decode("utf-8")
                )
            except Exception as exc:
                raise ValueError("Manifesto do snapshot inválido.") from exc

            if (
                not isinstance(manifest, dict)
                or manifest.get("format") != CLOUD_PAYLOAD_FORMAT
                or int(manifest.get("formatVersion") or 0)
                != CLOUD_PAYLOAD_VERSION
            ):
                raise ValueError("Versão do payload da nuvem não suportada.")

            if int(manifest.get("revision") or 0) != int(expected_revision):
                raise ValueError(
                    "Revisão do payload não corresponde ao envelope."
                )
            if str(manifest.get("deviceId") or "") != str(expected_device_id):
                raise ValueError(
                    "deviceId do payload não corresponde ao envelope."
                )

            entries = manifest.get("files")
            if not isinstance(entries, list):
                raise ValueError("Lista de arquivos do snapshot inválida.")

            extracted: dict[str, bytes] = {}
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError("Entrada de arquivo inválida no snapshot.")
                path = safe_zip_member_name(entry.get("path"))
                if path not in names:
                    raise ValueError(f"Arquivo {path} ausente no snapshot.")
                content = archive.read(path)
                if int(entry.get("size") or -1) != len(content):
                    raise ValueError(f"Tamanho inválido para {path}.")
                if str(entry.get("sha256") or "") != sha256_hex(content):
                    raise ValueError(f"Checksum inválido para {path}.")
                extracted[path] = content

            return {
                "manifest": manifest,
                "files": extracted,
            }

    def _validate_restore_contents(
        self,
        decoded: dict[str, Any],
        *,
        temp_dir: Path,
    ) -> dict[str, Any]:
        files = decoded["files"]
        manifest = decoded["manifest"]
        staged_db = None

        if manifest.get("databasePresent"):
            db_bytes = files.get("data/controlefin.db")
            if db_bytes is None:
                raise ValueError(
                    "Manifesto indica banco, mas o arquivo está ausente."
                )
            staged_db = temp_dir / "controlefin.db"
            staged_db.write_bytes(db_bytes)
            validate_sqlite_file(staged_db)

        settings_bytes = files.get("user_data/ajustes.json")
        if settings_bytes is None:
            raise ValueError("ajustes.json ausente no snapshot.")
        try:
            raw_settings = json.loads(settings_bytes.decode("utf-8"))
        except Exception as exc:
            raise ValueError("ajustes.json inválido no snapshot.") from exc
        normalized_settings = self.normalize_settings(raw_settings)

        pluggy = None
        pluggy_bytes = files.get("config/pluggy_portable.json")
        if pluggy_bytes is not None:
            try:
                pluggy = json.loads(pluggy_bytes.decode("utf-8"))
            except Exception as exc:
                raise ValueError("Configuração Pluggy portátil inválida.") from exc

            if not isinstance(pluggy, dict):
                raise ValueError("Configuração Pluggy portátil inválida.")

            client_id = str(pluggy.get("clientId") or "").strip()
            client_secret = str(pluggy.get("clientSecret") or "").strip()
            item_ids = pluggy.get("itemIds")
            if (
                not client_id
                or not client_secret
                or not isinstance(item_ids, list)
                or not item_ids
            ):
                raise ValueError("Configuração Pluggy portátil incompleta.")

        return {
            "stagedDb": staged_db,
            "settings": normalized_settings,
            "pluggy": pluggy,
            "manifest": manifest,
        }

    def _restore_file_exact(
        self,
        *,
        path: Path,
        backup_path: Path,
        existed: bool,
    ) -> None:
        if existed:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_path, path)
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def apply_plain_payload(
        self,
        *,
        payload: bytes,
        revision: int,
        device_id: str,
    ) -> dict[str, Any]:
        storage_root = self.db_file.parent.parent
        storage_root.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(
            prefix=".cloud-restore-",
            dir=str(storage_root),
        ) as tmp:
            temp_dir = Path(tmp)
            decoded = self._decode_plain_payload(
                payload=payload,
                expected_revision=revision,
                expected_device_id=device_id,
            )
            validated = self._validate_restore_contents(
                decoded,
                temp_dir=temp_dir,
            )

            backup_dir = temp_dir / "rollback"
            backup_dir.mkdir(parents=True, exist_ok=True)
            tracked = [
                (self.db_file, backup_dir / "controlefin.db"),
                (self.settings_file, backup_dir / "ajustes.json"),
                (self.pluggy_config_file, backup_dir / "pluggy.json"),
            ]

            existence: dict[str, bool] = {}
            for path, backup in tracked:
                existed = path.exists()
                existence[str(path)] = existed
                if existed:
                    shutil.copy2(path, backup)

            try:
                staged_db = validated["stagedDb"]
                if staged_db is not None:
                    self.db_file.parent.mkdir(parents=True, exist_ok=True)
                    publish_temp = self.db_file.with_name(
                        ".controlefin.cloud."
                        + secrets.token_hex(6)
                        + ".db"
                    )
                    shutil.copy2(staged_db, publish_temp)
                    os.replace(publish_temp, self.db_file)
                else:
                    try:
                        self.db_file.unlink()
                    except FileNotFoundError:
                        pass

                self.save_settings(validated["settings"])

                pluggy = validated["pluggy"]
                if pluggy is None:
                    try:
                        self.pluggy_config_file.unlink()
                    except FileNotFoundError:
                        pass
                else:
                    self.save_pluggy_config(
                        client_id=pluggy["clientId"],
                        client_secret=pluggy["clientSecret"],
                        item_ids=pluggy["itemIds"],
                    )

            except Exception:
                for path, backup in tracked:
                    self._restore_file_exact(
                        path=path,
                        backup_path=backup,
                        existed=existence[str(path)],
                    )
                raise

            return {
                "databaseRestored": validated["stagedDb"] is not None,
                "settingsRestored": True,
                "pluggyRestored": validated["pluggy"] is not None,
                "manifest": validated["manifest"],
            }

    def _snapshot_filename(self, *, revision: int, device_id: str) -> str:
        safe_device = str(device_id).replace("-", "")[:12]
        return (
            f"{CLOUD_SNAPSHOT_PREFIX}"
            f"{int(revision):012d}-"
            f"{safe_device}.bin"
        )

    def _prune_history(self) -> None:
        try:
            snapshots = self.remote_snapshots()
            if len(snapshots) <= CLOUD_HISTORY_LIMIT:
                return

            max_revision = max(int(item["revision"]) for item in snapshots)
            protected_tip_ids = {
                str(item["id"])
                for item in snapshots
                if int(item["revision"]) == max_revision
            }

            non_tip = [
                item
                for item in snapshots
                if str(item.get("id") or "") not in protected_tip_ids
            ]
            keep_non_tip = max(0, CLOUD_HISTORY_LIMIT - len(protected_tip_ids))

            for item in non_tip[keep_non_tip:]:
                try:
                    self.google_drive.delete_file(str(item["id"]))
                except Exception:
                    pass
        except Exception:
            return

    def upload(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            passphrase = self.load_passphrase()
            state = self.load_state()

            snapshots_before = self.remote_snapshots()
            remote_revision = 0
            if snapshots_before:
                if force:
                    remote_revision = max(
                        int(item["revision"])
                        for item in snapshots_before
                    )
                else:
                    tip_before = self._unique_remote_tip(snapshots_before)
                    remote_revision = int(tip_before["revision"])

            local_revision = int(state.get("lastAppliedRevision") or 0)
            if not force and remote_revision != local_revision:
                raise RuntimeError(
                    "Conflito: a nuvem mudou desde a última revisão local. "
                    "Sincronize/baixe primeiro antes de enviar."
                )

            next_revision = remote_revision + 1
            device_id = str(state["deviceId"])
            plain = self.build_plain_payload(
                revision=next_revision,
                device_id=device_id,
            )

            snapshots_after_build = self.remote_snapshots()
            current_revision = 0
            if snapshots_after_build:
                if force:
                    current_revision = max(
                        int(item["revision"])
                        for item in snapshots_after_build
                    )
                else:
                    current_tip = self._unique_remote_tip(snapshots_after_build)
                    current_revision = int(current_tip["revision"])

            if not force and current_revision != remote_revision:
                raise RuntimeError(
                    "Conflito: outro dispositivo publicou uma nova revisão "
                    "enquanto este computador preparava o snapshot."
                )

            if force and current_revision != remote_revision:
                remote_revision = current_revision
                next_revision = remote_revision + 1
                plain = self.build_plain_payload(
                    revision=next_revision,
                    device_id=device_id,
                )

            created_at = utc_now_iso()
            encrypted = encrypt_cloud_payload(
                payload=plain,
                passphrase=passphrase,
                revision=next_revision,
                device_id=device_id,
                created_at=created_at,
            )

            created = self.google_drive.create_bytes(
                name=self._snapshot_filename(
                    revision=next_revision,
                    device_id=device_id,
                ),
                data=encrypted,
                mime_type=CLOUD_MIME_TYPE,
                app_properties={
                    "kind": CLOUD_KIND,
                    "formatVersion": CLOUD_FORMAT_VERSION,
                    "revision": next_revision,
                    "deviceId": device_id,
                    "createdAtUtc": created_at,
                },
            )

            indexed_snapshot = self._remote_snapshot_from_file(created)
            if indexed_snapshot:
                self._write_remote_index(indexed_snapshot)

            local_info = self.local_state_info()

            state.update(
                {
                    "lastAppliedRevision": next_revision,
                    "lastRemoteFileId": created.get("id"),
                    "dirty": False,
                    "dirtyReason": None,
                    "localChangeAt": None,
                    "lastLocalSignature": local_info.get("signature"),
                    "lastLocalModifiedAt": local_info.get("modifiedTime"),
                    "lastRemoteModifiedTime": (
                        created.get("modifiedTime") or created_at
                    ),
                    "lastSyncAt": utc_now_iso(),
                    "lastAction": "UPLOADED",
                    "lastError": None,
                }
            )
            self.save_state(state)
            self._prune_history()

            return {
                "ok": True,
                "action": "UPLOADED",
                "revision": next_revision,
                "fileId": created.get("id"),
                "fileName": created.get("name"),
                "sizeBytes": len(encrypted),
                "forced": bool(force),
                "decision": "LOCAL_NEWER",
                "localModifiedTime": local_info.get("modifiedTime"),
                "remoteModifiedTime": (
                    created.get("modifiedTime") or created_at
                ),
            }

    def download(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            passphrase = self.load_passphrase()
            state = self.load_state()

            if state.get("dirty") and not force:
                raise RuntimeError(
                    "Conflito: existem alterações locais ainda não enviadas. "
                    "Escolha explicitamente usar a nuvem para descartá-las."
                )

            snapshots = self.remote_snapshots()
            tip = self._latest_remote_by_time(snapshots)
            encrypted = self.google_drive.download_bytes(file_id=tip["id"])
            metadata, plain = decrypt_cloud_payload(
                encrypted=encrypted,
                passphrase=passphrase,
            )

            revision = int(metadata.get("revision") or 0)
            device_id = str(metadata.get("deviceId") or "")
            if revision != int(tip["revision"]):
                raise ValueError(
                    "A revisão criptografada não corresponde ao arquivo remoto."
                )

            result = self.apply_plain_payload(
                payload=plain,
                revision=revision,
                device_id=device_id,
            )

            local_info = self.local_state_info()

            state.update(
                {
                    "lastAppliedRevision": revision,
                    "lastRemoteFileId": tip.get("id"),
                    "dirty": False,
                    "dirtyReason": None,
                    "localChangeAt": None,
                    "lastLocalSignature": local_info.get("signature"),
                    "lastLocalModifiedAt": local_info.get("modifiedTime"),
                    "lastRemoteModifiedTime": (
                        tip.get("modifiedTime")
                        or tip.get("snapshotCreatedAt")
                    ),
                    "lastSyncAt": utc_now_iso(),
                    "lastAction": "DOWNLOADED",
                    "lastError": None,
                }
            )
            self.save_state(state)

            return {
                "ok": True,
                "action": "DOWNLOADED",
                "revision": revision,
                "fileId": tip.get("id"),
                "fileName": tip.get("name"),
                "forced": bool(force),
                "decision": "REMOTE_NEWER",
                "localModifiedTime": local_info.get("modifiedTime"),
                "remoteModifiedTime": (
                    tip.get("modifiedTime")
                    or tip.get("snapshotCreatedAt")
                ),
                **result,
            }

    def smart_sync(self) -> dict[str, Any]:
        """
        NEWEST_WINS:
        Drive mais recente -> baixa/substitui local.
        Caso contrário -> envia o estado local.
        """
        with self._lock:
            if not self.key_configured():
                return {
                    "ok": False,
                    "action": "KEY_REQUIRED",
                    "message": "Configure a senha da nuvem.",
                }

            state = self.load_state()

            try:
                snapshots = self.remote_snapshots()
                local_info = self.local_state_info()

                if not snapshots:
                    result = self.upload(force=True)
                    result["decision"] = "NO_REMOTE_UPLOAD_LOCAL"
                    return result

                remote_tip = self._latest_remote_by_time(snapshots)
                remote_revision = int(remote_tip.get("revision") or 0)
                remote_modified = self._remote_modified_epoch(remote_tip)
                remote_modified_iso = (
                    remote_tip.get("modifiedTime")
                    or remote_tip.get("snapshotCreatedAt")
                )

                local_modified = local_info.get("modifiedEpoch")
                local_modified_iso = local_info.get("modifiedTime")
                local_revision = int(
                    state.get("lastAppliedRevision") or 0
                )

                last_signature = state.get("lastLocalSignature")
                current_signature = local_info.get("signature")
                signature_changed = bool(
                    last_signature
                    and last_signature != current_signature
                )
                local_changed = bool(
                    state.get("dirty") or signature_changed
                )

                same_remote = (
                    str(state.get("lastRemoteFileId") or "")
                    == str(remote_tip.get("id") or "")
                )

                # Um PC novo não deve sobrescrever o Drive só porque
                # settings locais foram criados agora.
                if local_revision == 0 and not state.get("dirty"):
                    result = self.download(force=True)
                    result["decision"] = "INITIAL_REMOTE_RESTORE"
                    return result

                if same_remote and not local_changed:
                    self._update_state(
                        lastSyncAt=utc_now_iso(),
                        lastAction="UP_TO_DATE",
                        lastError=None,
                        lastRemoteModifiedTime=remote_modified_iso,
                        lastLocalModifiedAt=local_modified_iso,
                        lastLocalSignature=current_signature,
                    )
                    return {
                        "ok": True,
                        "action": "UP_TO_DATE",
                        "decision": "SAME_VERSION",
                        "revision": remote_revision,
                        "localModifiedTime": local_modified_iso,
                        "remoteModifiedTime": remote_modified_iso,
                    }

                remote_is_newer = False

                if remote_modified is not None and local_modified is None:
                    remote_is_newer = True
                elif (
                    remote_modified is not None
                    and local_modified is not None
                ):
                    remote_is_newer = remote_modified > local_modified

                if remote_is_newer:
                    result = self.download(force=True)
                    result["decision"] = "REMOTE_NEWER"
                    return result

                result = self.upload(force=True)
                result["decision"] = "LOCAL_NEWER_OR_EQUAL"
                return result

            except Exception as exc:
                self._update_state(
                    lastAction="ERROR",
                    lastError=str(exc),
                )
                raise

