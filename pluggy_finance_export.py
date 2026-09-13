#!/usr/bin/env python3
"""
ControleFin - Exportador local de dados financeiros da Pluggy.

Objetivo
--------
Ler uma lista de Item IDs já conhecida pelo usuário, consultar os produtos
financeiros relacionados e gerar CSVs estáveis para consumo posterior por um
HTML/dashboard local.

Decisões importantes
---------------------
* Item ID = conexão com uma instituição/connector.
* accountId = conta bancária ou cartão pertencente ao Item.
* Pluggy Accounts possui type BANK ou CREDIT.
* Investments são um produto separado, consultado por itemId.
* Transações usam GET /v2/transactions, com paginação por cursor (`next`).
* O script NÃO cria, atualiza ou remove Items. Ele somente lê dados.
* Credenciais e API key nunca são gravadas nos CSVs.

Dependência externa: requests
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urljoin, urlparse
from dotenv import load_dotenv

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = "https://api.pluggy.ai"
USER_AGENT = "ControleFin-Local/1.0"
DEFAULT_OUTPUT_DIR = "data"


# ---------------------------------------------------------------------------
# Utilidades gerais
# ---------------------------------------------------------------------------


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_simple_dotenv(path: Path) -> None:
    """Carrega um .env simples sem adicionar dependência de python-dotenv."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return float(value)
    try:
        n = float(value)
        if math.isnan(n) or math.isinf(n):
            return None
        return n
    except (TypeError, ValueError):
        return None


def abs_or_none(value: Any) -> Optional[float]:
    n = safe_float(value)
    return abs(n) if n is not None else None


def month_from_iso(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    match = re.match(r"^(\d{4})-(\d{2})", value)
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)}"


def date_from_iso(value: Any) -> Optional[str]:
    if not isinstance(value, str) or len(value) < 10:
        return None
    candidate = value[:10]
    if re.match(r"^\d{4}-\d{2}-\d{2}$", candidate):
        return candidate
    return None


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def flatten_dict(data: dict[str, Any], prefix: str = "", sep: str = "__") -> dict[str, Any]:
    """
    Achata dicts recursivamente. Arrays são mantidos como JSON em uma célula.
    Isso preserva campos desconhecidos/adicionados pela API sem perder dados.
    """
    out: dict[str, Any] = {}
    for key, value in data.items():
        key_str = str(key)
        full_key = f"{prefix}{sep}{key_str}" if prefix else key_str
        if isinstance(value, dict):
            out.update(flatten_dict(value, full_key, sep=sep))
        elif isinstance(value, list):
            out[full_key] = json_text(value)
        else:
            out[full_key] = value
    return out


def response_list(payload: Any) -> list[dict[str, Any]]:
    """Normaliza formatos de resposta de endpoints que retornam coleções."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("results", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def read_item_ids_from_file(path: Optional[Path]) -> list[str]:
    if not path:
        return []
    if not path.exists():
        raise FileNotFoundError(f"Arquivo de Item IDs não encontrado: {path}")
    ids: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        ids.extend(part.strip() for part in line.split(",") if part.strip())
    return ids


def unique_preserve_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        value = value.strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


# ---------------------------------------------------------------------------
# Cliente HTTP Pluggy
# ---------------------------------------------------------------------------


class PluggyAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        method: str = "",
        endpoint: str = "",
        status_code: Optional[int] = None,
        response_text: str = "",
    ) -> None:
        super().__init__(message)
        self.method = method
        self.endpoint = endpoint
        self.status_code = status_code
        self.response_text = response_text


@dataclass
class PluggyClient:
    client_id: str
    client_secret: str
    base_url: str = BASE_URL
    timeout_seconds: int = 45
    api_key: Optional[str] = None
    request_log: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        retry = Retry(
            total=4,
            connect=4,
            read=4,
            status=4,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
        self.session = requests.Session()
        self.session.mount("https://", adapter)
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            }
        )

    def authenticate(self) -> str:
        url = f"{self.base_url}/auth"
        started = time.perf_counter()
        try:
            response = self.session.post(
                url,
                json={"clientId": self.client_id, "clientSecret": self.client_secret},
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            self._log_request("POST", "/auth", None, started, error=str(exc))
            raise PluggyAPIError(f"Falha de rede ao autenticar na Pluggy: {exc}") from exc

        self._log_request("POST", "/auth", response.status_code, started)
        if not response.ok:
            raise PluggyAPIError(
                "Falha ao autenticar na Pluggy.",
                method="POST",
                endpoint="/auth",
                status_code=response.status_code,
                response_text=response.text[:2000],
            )

        payload = response.json()
        api_key = (
            payload.get("apiKey")
            or payload.get("accessToken")
            or payload.get("access_token")
            or payload.get("token")
        )
        if not api_key:
            raise PluggyAPIError(
                "A autenticação retornou sucesso, mas nenhuma API key conhecida foi encontrada."
            )
        self.api_key = str(api_key)
        return self.api_key

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            self.authenticate()
        return {"X-API-KEY": str(self.api_key)}

    def _log_request(
        self,
        method: str,
        endpoint: str,
        status_code: Optional[int],
        started: float,
        *,
        error: Optional[str] = None,
    ) -> None:
        self.request_log.append(
            {
                "timestampUtc": utc_now_iso(),
                "method": method,
                "endpoint": endpoint,
                "statusCode": status_code,
                "durationMs": round((time.perf_counter() - started) * 1000, 2),
                "success": bool(status_code and 200 <= status_code < 300) if status_code else False,
                "error": error,
            }
        )

    def get_json(
        self,
        endpoint_or_url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        ignore_statuses: Iterable[int] = (),
        _retried_auth: bool = False,
    ) -> Any:
        if endpoint_or_url.startswith("http://") or endpoint_or_url.startswith("https://"):
            parsed = urlparse(endpoint_or_url)
            base_host = urlparse(self.base_url).netloc
            if parsed.netloc != base_host:
                raise PluggyAPIError(
                    f"A API retornou uma URL de paginação externa inesperada: {parsed.netloc}"
                )
            url = endpoint_or_url
            endpoint_for_log = parsed.path
        else:
            endpoint_for_log = endpoint_or_url if endpoint_or_url.startswith("/") else f"/{endpoint_or_url}"
            url = f"{self.base_url}{endpoint_for_log}"

        started = time.perf_counter()
        try:
            response = self.session.get(
                url,
                params=params,
                headers=self._headers(),
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            self._log_request("GET", endpoint_for_log, None, started, error=str(exc))
            raise PluggyAPIError(
                f"Falha de rede em GET {endpoint_for_log}: {exc}",
                method="GET",
                endpoint=endpoint_for_log,
            ) from exc

        self._log_request("GET", endpoint_for_log, response.status_code, started)

        if response.status_code == 401 and not _retried_auth:
            # API keys expiram. Renova uma vez e repete a requisição.
            self.authenticate()
            return self.get_json(
                endpoint_or_url,
                params=params,
                ignore_statuses=ignore_statuses,
                _retried_auth=True,
            )

        if response.status_code in set(ignore_statuses):
            return None

        if not response.ok:
            raise PluggyAPIError(
                f"Pluggy retornou HTTP {response.status_code} em GET {endpoint_for_log}",
                method="GET",
                endpoint=endpoint_for_log,
                status_code=response.status_code,
                response_text=response.text[:4000],
            )

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise PluggyAPIError(
                f"Resposta não-JSON em GET {endpoint_for_log}",
                method="GET",
                endpoint=endpoint_for_log,
                status_code=response.status_code,
                response_text=response.text[:4000],
            ) from exc

    def get_cursor_transactions(
        self,
        account_id: str,
        *,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"accountId": account_id}
        if date_from:
            params["dateFrom"] = date_from
        if date_to:
            params["dateTo"] = date_to

        payload = self.get_json("/v2/transactions", params=params)
        rows = response_list(payload)
        seen_next: set[str] = set()

        while isinstance(payload, dict) and payload.get("next"):
            next_value = str(payload["next"])
            if next_value in seen_next:
                raise PluggyAPIError(
                    f"Cursor repetido em /v2/transactions para accountId={account_id}."
                )
            seen_next.add(next_value)

            if next_value.startswith("http://") or next_value.startswith("https://"):
                next_url = next_value
            elif next_value.startswith("?"):
                next_url = f"{self.base_url}/v2/transactions{next_value}"
            elif next_value.startswith("/"):
                next_url = f"{self.base_url}{next_value}"
            else:
                next_url = urljoin(f"{self.base_url}/v2/transactions", next_value)

            payload = self.get_json(next_url)
            rows.extend(response_list(payload))

        return rows

    def get_page_list(
        self,
        endpoint: str,
        *,
        params: Optional[dict[str, Any]] = None,
        page_size: int = 500,
    ) -> list[dict[str, Any]]:
        """Paginação page/pageSize para endpoints que ainda a usam."""
        base_params = dict(params or {})
        page = 1
        all_rows: list[dict[str, Any]] = []

        while True:
            query = {**base_params, "pageSize": page_size, "page": page}
            payload = self.get_json(endpoint, params=query)
            rows = response_list(payload)
            all_rows.extend(rows)

            total_pages = payload.get("totalPages") if isinstance(payload, dict) else None
            if isinstance(total_pages, (int, float)) and page >= int(total_pages):
                break
            if not rows:
                break
            if len(rows) < page_size:
                break
            page += 1

            if page > 10000:
                raise PluggyAPIError(f"Proteção contra loop infinito acionada em {endpoint}")

        return all_rows


# ---------------------------------------------------------------------------
# Armazenamento em memória + erros de extração
# ---------------------------------------------------------------------------


@dataclass
class ExtractedData:
    tables: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    errors: list[dict[str, Any]] = field(default_factory=list)

    def add(self, table: str, record: dict[str, Any]) -> None:
        self.tables[table].append(record)

    def extend(self, table: str, records: Iterable[dict[str, Any]]) -> None:
        self.tables[table].extend(records)

    def error(
        self,
        scope: str,
        resource_id: Optional[str],
        exc: Exception,
        *,
        severity: str = "ERROR",
    ) -> None:
        status_code = getattr(exc, "status_code", None)
        response_text = getattr(exc, "response_text", "")
        endpoint = getattr(exc, "endpoint", "")
        self.errors.append(
            {
                "timestampUtc": utc_now_iso(),
                "severity": severity,
                "scope": scope,
                "resourceId": resource_id,
                "endpoint": endpoint,
                "httpStatus": status_code,
                "message": str(exc),
                "responseExcerpt": response_text[:1000] if response_text else None,
            }
        )


# ---------------------------------------------------------------------------
# Extração da Pluggy
# ---------------------------------------------------------------------------


@dataclass
class ExportOptions:
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    realtime_balances: bool = False
    include_identity: bool = True
    include_consents: bool = True
    include_loans: bool = True
    include_categories: bool = True


class PluggyFinanceExtractor:
    def __init__(
        self,
        client: PluggyClient,
        item_ids: list[str],
        options: ExportOptions,
    ) -> None:
        self.client = client
        self.item_ids = item_ids
        self.options = options
        self.data = ExtractedData()
        self.connector_cache: dict[str, dict[str, Any]] = {}

    def run(self) -> ExtractedData:
        run_started = utc_now_iso()

        if self.options.include_categories:
            self._collect_categories()

        for idx, item_id in enumerate(self.item_ids, start=1):
            print(f"[{idx}/{len(self.item_ids)}] Lendo Item {item_id}...", flush=True)
            self._collect_item(item_id)

        self._build_derived_tables(run_started)
        self.data.tables["api_request_log"] = list(self.client.request_log)
        self.data.tables["api_errors"] = list(self.data.errors)
        return self.data

    def _collect_categories(self) -> None:
        try:
            payload = self.client.get_json("/categories")
            rows = response_list(payload)
            if not rows and isinstance(payload, list):
                rows = payload
            self.data.extend("categories", rows)
        except Exception as exc:
            self.data.error("categories", None, exc, severity="WARNING")

    def _collect_item(self, item_id: str) -> None:
        try:
            item = self.client.get_json(f"/items/{item_id}")
        except Exception as exc:
            self.data.error("item", item_id, exc)
            return

        if not isinstance(item, dict):
            self.data.error("item", item_id, RuntimeError("Resposta do Item não é um objeto JSON."))
            return

        item = dict(item)
        item.setdefault("id", item_id)
        connector = self._resolve_connector(item)
        institution_name = self._institution_name(item, connector)
        connector_id = self._connector_id(item, connector)
        is_open_finance = self._connector_is_open_finance(item, connector)

        item["institutionName"] = institution_name
        item["resolvedConnectorId"] = connector_id
        item["resolvedIsOpenFinance"] = is_open_finance
        self.data.add("items", item)

        if self.options.include_consents:
            self._collect_consents(item_id, institution_name, connector_id)
        if self.options.include_identity:
            self._collect_identity(item_id, institution_name, connector_id)

        accounts = self._collect_accounts(
            item_id=item_id,
            institution_name=institution_name,
            connector_id=connector_id,
            is_open_finance=is_open_finance,
        )

        for account in accounts:
            self._collect_account_details(account, is_open_finance=is_open_finance)

        self._collect_investments(item_id, institution_name, connector_id)

        if self.options.include_loans:
            self._collect_loans(item_id, institution_name, connector_id)

    def _resolve_connector(self, item: dict[str, Any]) -> dict[str, Any]:
        embedded = item.get("connector")
        connector_id: Optional[str] = None
        if isinstance(embedded, dict) and embedded.get("id") is not None:
            connector_id = str(embedded["id"])
        elif item.get("connectorId") is not None:
            connector_id = str(item["connectorId"])

        if connector_id is None:
            return embedded if isinstance(embedded, dict) else {}

        if connector_id in self.connector_cache:
            return self.connector_cache[connector_id]

        try:
            resolved = self.client.get_json(f"/connectors/{connector_id}")
            if isinstance(resolved, dict):
                self.connector_cache[connector_id] = resolved
                self.data.add("connectors", resolved)
                return resolved
        except Exception as exc:
            self.data.error("connector", connector_id, exc, severity="WARNING")

        fallback = embedded if isinstance(embedded, dict) else {"id": connector_id}
        self.connector_cache[connector_id] = fallback
        self.data.add("connectors", fallback)
        return fallback

    @staticmethod
    def _institution_name(item: dict[str, Any], connector: dict[str, Any]) -> str:
        candidates = [
            connector.get("name"),
            connector.get("institutionName"),
            (item.get("connector") or {}).get("name") if isinstance(item.get("connector"), dict) else None,
            item.get("name"),
        ]
        return next((str(x) for x in candidates if x), "Instituição não identificada")

    @staticmethod
    def _connector_id(item: dict[str, Any], connector: dict[str, Any]) -> Optional[str]:
        value = connector.get("id")
        if value is None:
            value = item.get("connectorId")
        if value is None and isinstance(item.get("connector"), dict):
            value = item["connector"].get("id")
        return str(value) if value is not None else None

    @staticmethod
    def _connector_is_open_finance(
        item: dict[str, Any], connector: dict[str, Any]
    ) -> Optional[bool]:
        value = connector.get("isOpenFinance")
        if value is None and isinstance(item.get("connector"), dict):
            value = item["connector"].get("isOpenFinance")
        return bool(value) if value is not None else None

    def _collect_consents(
        self, item_id: str, institution_name: str, connector_id: Optional[str]
    ) -> None:
        try:
            payload = self.client.get_json(
                "/consents",
                params={"itemId": item_id},
                ignore_statuses=(404,),
            )
            for consent in response_list(payload):
                row = dict(consent)
                row.setdefault("itemId", item_id)
                row["institutionName"] = institution_name
                row["connectorId"] = connector_id
                self.data.add("consents", row)
        except Exception as exc:
            self.data.error("consents", item_id, exc, severity="WARNING")

    def _collect_identity(
        self, item_id: str, institution_name: str, connector_id: Optional[str]
    ) -> None:
        try:
            payload = self.client.get_json(
                "/identity",
                params={"itemId": item_id},
                ignore_statuses=(404,),
            )
            if isinstance(payload, dict):
                row = dict(payload)
                row.setdefault("itemId", item_id)
                row["institutionName"] = institution_name
                row["connectorId"] = connector_id
                self.data.add("identities", row)
        except Exception as exc:
            self.data.error("identity", item_id, exc, severity="WARNING")

    def _collect_accounts(
        self,
        *,
        item_id: str,
        institution_name: str,
        connector_id: Optional[str],
        is_open_finance: Optional[bool],
    ) -> list[dict[str, Any]]:
        try:
            payload = self.client.get_json("/accounts", params={"itemId": item_id})
            accounts = response_list(payload)
        except Exception as exc:
            self.data.error("accounts", item_id, exc)
            return []

        enriched: list[dict[str, Any]] = []
        for account in accounts:
            row = dict(account)
            row.setdefault("itemId", item_id)
            row["accountId"] = row.get("id")
            row["institutionName"] = institution_name
            row["connectorId"] = connector_id
            row["isOpenFinance"] = is_open_finance
            row["accountGroup"] = (
                "BANK_ACCOUNT"
                if row.get("type") == "BANK"
                else "CREDIT_CARD"
                if row.get("type") == "CREDIT"
                else "OTHER_ACCOUNT"
            )
            row["effectiveBalance"] = row.get("balance")
            row["balanceSource"] = "ITEM_SYNC"
            self.data.add("accounts", row)
            if row.get("type") == "BANK":
                self.data.add("bank_accounts", row)
                self._extract_reserved_balances(row)
            elif row.get("type") == "CREDIT":
                self.data.add("credit_cards", row)
                self._extract_disaggregated_credit_limits(row)
            enriched.append(row)
        return enriched

    def _collect_account_details(
        self, account: dict[str, Any], *, is_open_finance: Optional[bool]
    ) -> None:
        account_id = str(account.get("id") or account.get("accountId") or "")
        if not account_id:
            return

        if self.options.realtime_balances and is_open_finance is True:
            try:
                payload = self.client.get_json(
                    f"/accounts/{account_id}/balance",
                    ignore_statuses=(400, 403, 404, 429, 502),
                )
                if isinstance(payload, dict):
                    row = dict(payload)
                    row["accountId"] = account_id
                    row["itemId"] = account.get("itemId")
                    row["institutionName"] = account.get("institutionName")
                    row["accountName"] = account.get("name")
                    row["accountType"] = account.get("type")
                    self.data.add("realtime_balances", row)
                    realtime_value = safe_float(payload.get("balance"))
                    if realtime_value is not None:
                        # O endpoint de saldo em tempo real usa a mesma lógica de
                        # balance e atualiza o resource da conta na Pluggy. Mantemos
                        # o valor também no objeto local para que os KPIs reflitam
                        # a consulta mais recente desta execução.
                        account["effectiveBalance"] = realtime_value
                        account["balanceSource"] = "REALTIME"
                        account["realtimeBalanceUpdateDateTime"] = payload.get("updateDateTime")
            except Exception as exc:
                self.data.error("realtime_balance", account_id, exc, severity="WARNING")

        try:
            transactions = self.client.get_cursor_transactions(
                account_id,
                date_from=self.options.date_from,
                date_to=self.options.date_to,
            )
            for tx in transactions:
                self.data.add("transactions", self._enrich_transaction(tx, account))
        except Exception as exc:
            self.data.error("transactions", account_id, exc)

        if account.get("type") == "CREDIT":
            self._collect_bills(account)

    def _enrich_transaction(
        self, tx: dict[str, Any], account: dict[str, Any]
    ) -> dict[str, Any]:
        row = dict(tx)
        row.setdefault("accountId", account.get("id"))
        row["itemId"] = account.get("itemId")
        row["institutionName"] = account.get("institutionName")
        row["accountName"] = account.get("name")
        row["accountType"] = account.get("type")
        row["accountSubtype"] = account.get("subtype")
        row["accountGroup"] = account.get("accountGroup")
        row["month"] = month_from_iso(row.get("date"))

        tx_type = str(row.get("type") or "").upper()
        amount_abs = abs_or_none(row.get("amount"))

        row["normalizedInflow"] = None
        row["normalizedOutflow"] = None
        row["normalizedNetCashflow"] = None
        row["normalizedExpense"] = None
        row["normalizedCardCredit"] = None

        if amount_abs is not None and account.get("type") == "BANK":
            if tx_type == "CREDIT":
                row["normalizedInflow"] = amount_abs
                row["normalizedOutflow"] = 0.0
                row["normalizedNetCashflow"] = amount_abs
            elif tx_type == "DEBIT":
                row["normalizedInflow"] = 0.0
                row["normalizedOutflow"] = amount_abs
                row["normalizedNetCashflow"] = -amount_abs
                row["normalizedExpense"] = amount_abs
        elif amount_abs is not None and account.get("type") == "CREDIT":
            # Usa type em vez do sinal bruto, pois conectores podem divergir no sinal.
            if tx_type == "DEBIT":
                row["normalizedExpense"] = amount_abs
                row["normalizedCardCredit"] = 0.0
            elif tx_type == "CREDIT":
                row["normalizedExpense"] = 0.0
                row["normalizedCardCredit"] = amount_abs

        cc_meta = row.get("creditCardMetadata")
        if isinstance(cc_meta, dict) and safe_float(cc_meta.get("totalInstallments")):
            total = int(safe_float(cc_meta.get("totalInstallments")) or 0)
            if total > 1:
                installment = {
                    "transactionId": row.get("id"),
                    "accountId": row.get("accountId"),
                    "itemId": row.get("itemId"),
                    "institutionName": row.get("institutionName"),
                    "accountName": row.get("accountName"),
                    "date": row.get("date"),
                    "description": row.get("description"),
                    "category": row.get("category"),
                    "categoryId": row.get("categoryId"),
                    "amount": row.get("amount"),
                    "normalizedExpense": row.get("normalizedExpense"),
                    "currencyCode": row.get("currencyCode"),
                    **cc_meta,
                }
                self.data.add("card_installments", installment)

        return row

    def _collect_bills(self, account: dict[str, Any]) -> None:
        account_id = str(account.get("id") or "")
        if not account_id:
            return
        try:
            payload = self.client.get_json("/bills", params={"accountId": account_id})
            bills = response_list(payload)
        except Exception as exc:
            self.data.error("bills", account_id, exc, severity="WARNING")
            return

        for bill in bills:
            row = dict(bill)
            row["accountId"] = account_id
            row["itemId"] = account.get("itemId")
            row["institutionName"] = account.get("institutionName")
            row["accountName"] = account.get("name")
            self.data.add("credit_card_bills", row)

            bill_id = row.get("id")
            for payment in row.get("payments") or []:
                if isinstance(payment, dict):
                    child = dict(payment)
                    child["billId"] = bill_id
                    child["accountId"] = account_id
                    child["itemId"] = account.get("itemId")
                    child["institutionName"] = account.get("institutionName")
                    child["accountName"] = account.get("name")
                    self.data.add("bill_payments", child)

            for charge in row.get("financeCharges") or []:
                if isinstance(charge, dict):
                    child = dict(charge)
                    child["billId"] = bill_id
                    child["accountId"] = account_id
                    child["itemId"] = account.get("itemId")
                    child["institutionName"] = account.get("institutionName")
                    child["accountName"] = account.get("name")
                    self.data.add("bill_finance_charges", child)

    def _extract_disaggregated_credit_limits(self, account: dict[str, Any]) -> None:
        credit_data = account.get("creditData")
        if not isinstance(credit_data, dict):
            return
        for limit_row in credit_data.get("disaggregatedCreditLimits") or []:
            if isinstance(limit_row, dict):
                child = dict(limit_row)
                child["accountId"] = account.get("id")
                child["itemId"] = account.get("itemId")
                child["institutionName"] = account.get("institutionName")
                child["accountName"] = account.get("name")
                self.data.add("credit_limits", child)

    def _extract_reserved_balances(self, account: dict[str, Any]) -> None:
        bank_data = account.get("bankData")
        if not isinstance(bank_data, dict):
            return
        for index, reserved in enumerate(bank_data.get("reservedBalances") or [], start=1):
            if not isinstance(reserved, dict):
                continue
            parent = dict(reserved)
            parent["reservedBalanceIndex"] = index
            parent["accountId"] = account.get("id")
            parent["itemId"] = account.get("itemId")
            parent["institutionName"] = account.get("institutionName")
            parent["accountName"] = account.get("name")
            self.data.add("reserved_balances", parent)

            for amount_index, available in enumerate(reserved.get("availableAmounts") or [], start=1):
                if isinstance(available, dict):
                    child = dict(available)
                    child["reservedBalanceIndex"] = index
                    child["availableAmountIndex"] = amount_index
                    child["reservedBalanceName"] = reserved.get("name")
                    child["accountId"] = account.get("id")
                    child["itemId"] = account.get("itemId")
                    child["institutionName"] = account.get("institutionName")
                    child["accountName"] = account.get("name")
                    self.data.add("reserved_balance_amounts", child)

    def _collect_investments(
        self, item_id: str, institution_name: str, connector_id: Optional[str]
    ) -> None:
        try:
            investments = self.client.get_page_list(
                "/investments", params={"itemId": item_id}, page_size=500
            )
        except Exception as exc:
            self.data.error("investments", item_id, exc, severity="WARNING")
            return

        for inv in investments:
            row = dict(inv)
            row.setdefault("itemId", item_id)
            row["connectorId"] = connector_id
            row["connectionInstitutionName"] = institution_name
            investment_institution = row.get("institution")
            if isinstance(investment_institution, dict) and investment_institution.get("name"):
                row["institutionName"] = investment_institution.get("name")
            else:
                row["institutionName"] = institution_name

            original = safe_float(row.get("amountOriginal"))
            balance = safe_float(row.get("balance"))
            reported_profit = safe_float(row.get("amountProfit"))
            computed_profit = reported_profit
            if computed_profit is None and balance is not None and original is not None:
                computed_profit = balance - original
            row["computedProfit"] = computed_profit
            row["computedReturnPct"] = (
                (computed_profit / original) * 100
                if computed_profit is not None and original not in (None, 0)
                else None
            )
            self.data.add("investments", row)

            investment_id = row.get("id")
            if investment_id:
                try:
                    txs = self.client.get_page_list(
                        f"/investments/{investment_id}/transactions",
                        page_size=500,
                    )
                    for tx in txs:
                        child = dict(tx)
                        child["investmentId"] = investment_id
                        child["investmentName"] = row.get("name")
                        child["investmentType"] = row.get("type")
                        child["investmentSubtype"] = row.get("subtype")
                        child["itemId"] = item_id
                        child["institutionName"] = row.get("institutionName")
                        child["currencyCode"] = child.get("currencyCode") or row.get("currencyCode")
                        self.data.add("investment_transactions", child)
                except Exception as exc:
                    self.data.error(
                        "investment_transactions", str(investment_id), exc, severity="WARNING"
                    )

    def _collect_loans(
        self, item_id: str, institution_name: str, connector_id: Optional[str]
    ) -> None:
        try:
            payload = self.client.get_json(
                "/loans", params={"itemId": item_id}, ignore_statuses=(404,)
            )
            loans = response_list(payload)
        except Exception as exc:
            self.data.error("loans", item_id, exc, severity="WARNING")
            return

        for loan in loans:
            row = dict(loan)
            row.setdefault("itemId", item_id)
            row["institutionName"] = institution_name
            row["connectorId"] = connector_id
            self.data.add("loans", row)
            self._extract_loan_children(row)

    def _extract_loan_children(self, loan: dict[str, Any]) -> None:
        loan_id = loan.get("id")
        common = {
            "loanId": loan_id,
            "itemId": loan.get("itemId"),
            "institutionName": loan.get("institutionName"),
            "productName": loan.get("productName"),
            "currencyCode": loan.get("currencyCode"),
        }

        child_specs = [
            ("interestRates", "loan_interest_rates"),
            ("contractedFees", "loan_fees"),
            ("contractedFinanceCharges", "loan_finance_charges"),
            ("warranties", "loan_warranties"),
        ]
        for source_key, table in child_specs:
            for child in loan.get(source_key) or []:
                if isinstance(child, dict):
                    self.data.add(table, {**common, **child})

        installments = loan.get("installments")
        if isinstance(installments, dict):
            for balloon in installments.get("balloonPayments") or []:
                if isinstance(balloon, dict):
                    self.data.add("loan_balloon_payments", {**common, **balloon})

        payments = loan.get("payments")
        if isinstance(payments, dict):
            for release in payments.get("releases") or []:
                if not isinstance(release, dict):
                    continue
                release_row = {**common, **release}
                self.data.add("loan_payment_releases", release_row)

                over_parcel = release.get("overParcel")
                if isinstance(over_parcel, dict):
                    for fee in over_parcel.get("fees") or []:
                        if isinstance(fee, dict):
                            self.data.add(
                                "loan_overparcel_fees",
                                {
                                    **common,
                                    "paymentProviderId": release.get("providerId"),
                                    "instalmentId": release.get("instalmentId"),
                                    **fee,
                                },
                            )
                    for charge in over_parcel.get("charges") or []:
                        if isinstance(charge, dict):
                            self.data.add(
                                "loan_overparcel_charges",
                                {
                                    **common,
                                    "paymentProviderId": release.get("providerId"),
                                    "instalmentId": release.get("instalmentId"),
                                    **charge,
                                },
                            )

    # ------------------------------------------------------------------
    # Tabelas derivadas para o dashboard
    # ------------------------------------------------------------------

    def _build_derived_tables(self, run_started: str) -> None:
        self._build_connection_health()
        self._build_positions_and_kpis()
        self._build_credit_utilization()
        self._build_investment_allocation()
        self._build_cashflow_tables()
        self._build_spending_tables()
        self._build_extraction_summary(run_started)

    def _build_connection_health(self) -> None:
        consent_by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for consent in self.data.tables.get("consents", []):
            if consent.get("itemId"):
                consent_by_item[str(consent["itemId"])].append(consent)

        for item in self.data.tables.get("items", []):
            item_id = str(item.get("id") or "")
            consents = consent_by_item.get(item_id, [])
            self.data.add(
                "connection_health",
                {
                    "itemId": item_id,
                    "institutionName": item.get("institutionName"),
                    "connectorId": item.get("resolvedConnectorId"),
                    "isOpenFinance": item.get("resolvedIsOpenFinance"),
                    "status": item.get("status"),
                    "executionStatus": item.get("executionStatus"),
                    "lastUpdatedAt": item.get("lastUpdatedAt"),
                    "nextAutoSyncAt": item.get("nextAutoSyncAt"),
                    "updatedAt": item.get("updatedAt"),
                    "createdAt": item.get("createdAt"),
                    "clientUserId": item.get("clientUserId"),
                    "consentCount": len(consents),
                    "hasError": bool(item.get("error")),
                    "errorJson": json_text(item.get("error")) if item.get("error") else None,
                    "statusDetailJson": (
                        json_text(item.get("statusDetail")) if item.get("statusDetail") else None
                    ),
                },
            )

    def _build_positions_and_kpis(self) -> None:
        positions: list[dict[str, Any]] = []

        for account in self.data.tables.get("accounts", []):
            currency = account.get("currencyCode") or "UNKNOWN"
            amount = safe_float(account.get("effectiveBalance"))
            if amount is None:
                continue
            account_type = account.get("type")
            if account_type == "BANK":
                contribution = amount
                position_type = "BANK_ACCOUNT"
            elif account_type == "CREDIT":
                contribution = -amount
                position_type = "CREDIT_CARD"
            else:
                contribution = amount
                position_type = "OTHER_ACCOUNT"

            positions.append(
                {
                    "sourceId": account.get("id"),
                    "itemId": account.get("itemId"),
                    "institutionName": account.get("institutionName"),
                    "positionType": position_type,
                    "name": account.get("name") or account.get("marketingName"),
                    "subtype": account.get("subtype"),
                    "currencyCode": currency,
                    "currentAmount": amount,
                    "netWorthContribution": contribution,
                    "status": (
                        (account.get("creditData") or {}).get("status")
                        if isinstance(account.get("creditData"), dict)
                        else None
                    ),
                }
            )

        for inv in self.data.tables.get("investments", []):
            amount = safe_float(inv.get("balance"))
            if amount is None:
                continue
            positions.append(
                {
                    "sourceId": inv.get("id"),
                    "itemId": inv.get("itemId"),
                    "institutionName": inv.get("institutionName"),
                    "positionType": "INVESTMENT",
                    "name": inv.get("name"),
                    "subtype": inv.get("subtype") or inv.get("type"),
                    "currencyCode": inv.get("currencyCode") or "UNKNOWN",
                    "currentAmount": amount,
                    "netWorthContribution": amount,
                    "status": inv.get("status"),
                }
            )

        for loan in self.data.tables.get("loans", []):
            payments = loan.get("payments")
            outstanding = (
                safe_float(payments.get("contractOutstandingBalance"))
                if isinstance(payments, dict)
                else None
            )
            if outstanding is None:
                continue
            positions.append(
                {
                    "sourceId": loan.get("id"),
                    "itemId": loan.get("itemId"),
                    "institutionName": loan.get("institutionName"),
                    "positionType": "LOAN",
                    "name": loan.get("productName") or loan.get("type"),
                    "subtype": loan.get("type"),
                    "currencyCode": loan.get("currencyCode") or "UNKNOWN",
                    "currentAmount": outstanding,
                    "netWorthContribution": -outstanding,
                    "status": None,
                }
            )

        self.data.tables["financial_positions"] = positions

        # KPIs agregados por moeda — nunca mistura BRL, USD etc.
        by_currency: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        loan_missing_outstanding: dict[str, int] = defaultdict(int)

        for account in self.data.tables.get("accounts", []):
            currency = str(account.get("currencyCode") or "UNKNOWN")
            balance = safe_float(account.get("effectiveBalance"))
            if balance is None:
                continue
            if account.get("type") == "BANK":
                by_currency[currency]["bankBalance"] += balance
                bank_data = account.get("bankData")
                if isinstance(bank_data, dict):
                    auto = safe_float(bank_data.get("automaticallyInvestedBalance"))
                    if auto is not None:
                        by_currency[currency]["automaticallyInvestedBalance"] += auto
            elif account.get("type") == "CREDIT":
                by_currency[currency]["creditCardOpenBalance"] += balance
                credit_data = account.get("creditData")
                if isinstance(credit_data, dict):
                    credit_limit = safe_float(credit_data.get("creditLimit"))
                    available = safe_float(credit_data.get("availableCreditLimit"))
                    minimum = safe_float(credit_data.get("minimumPayment"))
                    if credit_limit is not None:
                        by_currency[currency]["creditLimit"] += credit_limit
                    if available is not None:
                        by_currency[currency]["availableCreditLimit"] += available
                    if minimum is not None:
                        by_currency[currency]["minimumCardPayment"] += minimum

        for inv in self.data.tables.get("investments", []):
            currency = str(inv.get("currencyCode") or "UNKNOWN")
            balance = safe_float(inv.get("balance"))
            amount = safe_float(inv.get("amount"))
            profit = safe_float(inv.get("computedProfit"))
            withdrawal = safe_float(inv.get("amountWithdrawal"))
            if balance is not None:
                by_currency[currency]["investmentNetBalance"] += balance
            if amount is not None:
                by_currency[currency]["investmentGrossAmount"] += amount
            if profit is not None:
                by_currency[currency]["investmentProfit"] += profit
            if withdrawal is not None:
                by_currency[currency]["investmentWithdrawable"] += withdrawal

        for loan in self.data.tables.get("loans", []):
            currency = str(loan.get("currencyCode") or "UNKNOWN")
            payments = loan.get("payments")
            outstanding = (
                safe_float(payments.get("contractOutstandingBalance"))
                if isinstance(payments, dict)
                else None
            )
            if outstanding is not None:
                by_currency[currency]["loanOutstandingBalance"] += outstanding
            else:
                loan_missing_outstanding[currency] += 1

        kpi_rows: list[dict[str, Any]] = []
        for currency in sorted(by_currency.keys() | loan_missing_outstanding.keys()):
            values = by_currency[currency]
            bank_balance = values.get("bankBalance", 0.0)
            investment_balance = values.get("investmentNetBalance", 0.0)
            card_balance = values.get("creditCardOpenBalance", 0.0)
            loan_balance = values.get("loanOutstandingBalance", 0.0)
            net_worth = bank_balance + investment_balance - card_balance - loan_balance
            credit_limit = values.get("creditLimit", 0.0)
            available_limit = values.get("availableCreditLimit", 0.0)
            used_limit = max(credit_limit - available_limit, 0.0) if credit_limit else None
            utilization = (
                (used_limit / credit_limit) * 100
                if used_limit is not None and credit_limit > 0
                else None
            )
            kpi_rows.append(
                {
                    "currencyCode": currency,
                    "bankBalance": round(bank_balance, 2),
                    "automaticallyInvestedBalance": round(
                        values.get("automaticallyInvestedBalance", 0.0), 2
                    ),
                    "investmentNetBalance": round(investment_balance, 2),
                    "investmentGrossAmount": round(values.get("investmentGrossAmount", 0.0), 2),
                    "investmentProfit": round(values.get("investmentProfit", 0.0), 2),
                    "investmentWithdrawable": round(
                        values.get("investmentWithdrawable", 0.0), 2
                    ),
                    "creditCardOpenBalance": round(card_balance, 2),
                    "creditLimit": round(credit_limit, 2),
                    "availableCreditLimit": round(available_limit, 2),
                    "estimatedUsedCreditLimit": round(used_limit, 2)
                    if used_limit is not None
                    else None,
                    "creditUtilizationPct": round(utilization, 2)
                    if utilization is not None
                    else None,
                    "minimumCardPayment": round(values.get("minimumCardPayment", 0.0), 2),
                    "loanOutstandingBalance": round(loan_balance, 2),
                    "loansMissingOutstandingBalance": loan_missing_outstanding.get(currency, 0),
                    "estimatedNetWorth": round(net_worth, 2),
                }
            )
        self.data.tables["dashboard_kpis"] = kpi_rows

        # Distribuição por instituição e tipo de posição.
        grouped: dict[tuple[str, str, str], dict[str, float]] = defaultdict(
            lambda: {"currentAmount": 0.0, "netWorthContribution": 0.0, "count": 0.0}
        )
        for pos in positions:
            key = (
                str(pos.get("institutionName") or "Não identificada"),
                str(pos.get("positionType") or "OTHER"),
                str(pos.get("currencyCode") or "UNKNOWN"),
            )
            grouped[key]["currentAmount"] += safe_float(pos.get("currentAmount")) or 0.0
            grouped[key]["netWorthContribution"] += (
                safe_float(pos.get("netWorthContribution")) or 0.0
            )
            grouped[key]["count"] += 1
        self.data.tables["positions_by_institution"] = [
            {
                "institutionName": key[0],
                "positionType": key[1],
                "currencyCode": key[2],
                "positionCount": int(values["count"]),
                "currentAmount": round(values["currentAmount"], 2),
                "netWorthContribution": round(values["netWorthContribution"], 2),
            }
            for key, values in sorted(grouped.items())
        ]

    def _build_credit_utilization(self) -> None:
        rows: list[dict[str, Any]] = []
        for card in self.data.tables.get("credit_cards", []):
            credit_data = card.get("creditData")
            if not isinstance(credit_data, dict):
                credit_data = {}
            limit_total = safe_float(credit_data.get("creditLimit"))
            available = safe_float(credit_data.get("availableCreditLimit"))
            open_balance = safe_float(card.get("effectiveBalance"))
            used_from_limit = (
                max(limit_total - available, 0.0)
                if limit_total is not None and available is not None
                else None
            )
            utilization = (
                (used_from_limit / limit_total) * 100
                if used_from_limit is not None and limit_total and limit_total > 0
                else None
            )
            rows.append(
                {
                    "accountId": card.get("id"),
                    "itemId": card.get("itemId"),
                    "institutionName": card.get("institutionName"),
                    "accountName": card.get("name"),
                    "brand": credit_data.get("brand"),
                    "level": credit_data.get("level"),
                    "currencyCode": card.get("currencyCode"),
                    "openBalance": open_balance,
                    "creditLimit": limit_total,
                    "availableCreditLimit": available,
                    "estimatedUsedLimit": used_from_limit,
                    "utilizationPct": round(utilization, 2) if utilization is not None else None,
                    "minimumPayment": credit_data.get("minimumPayment"),
                    "balanceCloseDate": credit_data.get("balanceCloseDate"),
                    "balanceDueDate": credit_data.get("balanceDueDate"),
                    "status": credit_data.get("status"),
                    "holderType": credit_data.get("holderType"),
                    "isLimitFlexible": credit_data.get("isLimitFlexible"),
                }
            )
        self.data.tables["credit_utilization"] = rows

    def _build_investment_allocation(self) -> None:
        grouped: dict[tuple[str, str, str, str], dict[str, float]] = defaultdict(
            lambda: {"balance": 0.0, "amount": 0.0, "profit": 0.0, "count": 0.0}
        )
        for inv in self.data.tables.get("investments", []):
            key = (
                str(inv.get("institutionName") or "Não identificada"),
                str(inv.get("type") or "UNKNOWN"),
                str(inv.get("subtype") or "UNKNOWN"),
                str(inv.get("currencyCode") or "UNKNOWN"),
            )
            values = grouped[key]
            values["balance"] += safe_float(inv.get("balance")) or 0.0
            values["amount"] += safe_float(inv.get("amount")) or 0.0
            values["profit"] += safe_float(inv.get("computedProfit")) or 0.0
            values["count"] += 1

        rows = []
        for key, values in sorted(grouped.items()):
            rows.append(
                {
                    "institutionName": key[0],
                    "investmentType": key[1],
                    "investmentSubtype": key[2],
                    "currencyCode": key[3],
                    "assetCount": int(values["count"]),
                    "netBalance": round(values["balance"], 2),
                    "grossAmount": round(values["amount"], 2),
                    "profit": round(values["profit"], 2),
                }
            )
        self.data.tables["investment_allocation"] = rows

    def _build_cashflow_tables(self) -> None:
        aggregate: dict[tuple[str, str, str], dict[str, float]] = defaultdict(
            lambda: {"inflow": 0.0, "outflow": 0.0, "net": 0.0, "count": 0.0}
        )
        by_account: dict[tuple[str, str, str, str, str, str, str], dict[str, float]] = defaultdict(
            lambda: {"inflow": 0.0, "outflow": 0.0, "net": 0.0, "count": 0.0}
        )

        for tx in self.data.tables.get("transactions", []):
            if tx.get("accountType") != "BANK":
                continue
            month = tx.get("month") or month_from_iso(tx.get("date"))
            if not month:
                continue
            currency = str(tx.get("currencyCode") or "UNKNOWN")
            status = str(tx.get("status") or "UNKNOWN")
            inflow = safe_float(tx.get("normalizedInflow")) or 0.0
            outflow = safe_float(tx.get("normalizedOutflow")) or 0.0
            net = safe_float(tx.get("normalizedNetCashflow")) or 0.0

            key = (str(month), currency, status)
            aggregate[key]["inflow"] += inflow
            aggregate[key]["outflow"] += outflow
            aggregate[key]["net"] += net
            aggregate[key]["count"] += 1

            account_key = (
                str(month),
                str(tx.get("itemId") or ""),
                str(tx.get("accountId") or ""),
                str(tx.get("institutionName") or ""),
                str(tx.get("accountName") or ""),
                currency,
                status,
            )
            by_account[account_key]["inflow"] += inflow
            by_account[account_key]["outflow"] += outflow
            by_account[account_key]["net"] += net
            by_account[account_key]["count"] += 1

        self.data.tables["monthly_cashflow"] = [
            {
                "month": key[0],
                "currencyCode": key[1],
                "status": key[2],
                "transactionCount": int(values["count"]),
                "inflow": round(values["inflow"], 2),
                "outflow": round(values["outflow"], 2),
                "netCashflow": round(values["net"], 2),
            }
            for key, values in sorted(aggregate.items())
        ]

        self.data.tables["monthly_cashflow_by_account"] = [
            {
                "month": key[0],
                "itemId": key[1],
                "accountId": key[2],
                "institutionName": key[3],
                "accountName": key[4],
                "currencyCode": key[5],
                "status": key[6],
                "transactionCount": int(values["count"]),
                "inflow": round(values["inflow"], 2),
                "outflow": round(values["outflow"], 2),
                "netCashflow": round(values["net"], 2),
            }
            for key, values in sorted(by_account.items())
        ]

    def _build_spending_tables(self) -> None:
        category_group: dict[
            tuple[str, str, str, str, str, str], dict[str, float]
        ] = defaultdict(lambda: {"expense": 0.0, "count": 0.0})
        merchant_group: dict[
            tuple[str, str, str, str, str], dict[str, float]
        ] = defaultdict(lambda: {"expense": 0.0, "count": 0.0})
        card_monthly: dict[
            tuple[str, str, str, str, str, str], dict[str, float]
        ] = defaultdict(lambda: {"expense": 0.0, "credits": 0.0, "count": 0.0})

        for tx in self.data.tables.get("transactions", []):
            expense = safe_float(tx.get("normalizedExpense"))
            card_credit = safe_float(tx.get("normalizedCardCredit")) or 0.0
            month = tx.get("month") or month_from_iso(tx.get("date"))
            if not month:
                continue
            currency = str(tx.get("currencyCode") or "UNKNOWN")
            status = str(tx.get("status") or "UNKNOWN")
            source = str(tx.get("accountGroup") or "UNKNOWN")

            if expense is not None and expense > 0:
                cat_key = (
                    str(month),
                    source,
                    str(tx.get("categoryId") or "UNCATEGORIZED"),
                    str(tx.get("category") or "Sem categoria"),
                    currency,
                    status,
                )
                category_group[cat_key]["expense"] += expense
                category_group[cat_key]["count"] += 1

                merchant_name = None
                merchant = tx.get("merchant")
                if isinstance(merchant, dict):
                    merchant_name = merchant.get("name") or merchant.get("businessName")
                merchant_name = merchant_name or tx.get("description") or "Não identificado"
                merch_key = (str(month), source, str(merchant_name), currency, status)
                merchant_group[merch_key]["expense"] += expense
                merchant_group[merch_key]["count"] += 1

            if tx.get("accountType") == "CREDIT":
                card_key = (
                    str(month),
                    str(tx.get("accountId") or ""),
                    str(tx.get("institutionName") or ""),
                    str(tx.get("accountName") or ""),
                    currency,
                    status,
                )
                if expense is not None:
                    card_monthly[card_key]["expense"] += expense
                card_monthly[card_key]["credits"] += card_credit
                card_monthly[card_key]["count"] += 1

        self.data.tables["monthly_spending_by_category"] = [
            {
                "month": key[0],
                "sourceGroup": key[1],
                "categoryId": key[2],
                "category": key[3],
                "currencyCode": key[4],
                "status": key[5],
                "transactionCount": int(values["count"]),
                "expense": round(values["expense"], 2),
            }
            for key, values in sorted(category_group.items())
        ]

        self.data.tables["monthly_spending_by_merchant"] = [
            {
                "month": key[0],
                "sourceGroup": key[1],
                "merchantOrDescription": key[2],
                "currencyCode": key[3],
                "status": key[4],
                "transactionCount": int(values["count"]),
                "expense": round(values["expense"], 2),
            }
            for key, values in sorted(merchant_group.items())
        ]

        self.data.tables["monthly_credit_card_spending"] = [
            {
                "month": key[0],
                "accountId": key[1],
                "institutionName": key[2],
                "accountName": key[3],
                "currencyCode": key[4],
                "status": key[5],
                "transactionCount": int(values["count"]),
                "cardCharges": round(values["expense"], 2),
                "cardCreditsOrPayments": round(values["credits"], 2),
                "netCardActivity": round(values["expense"] - values["credits"], 2),
            }
            for key, values in sorted(card_monthly.items())
        ]

    def _build_extraction_summary(self, run_started: str) -> None:
        rows = []
        for table_name, records in sorted(self.data.tables.items()):
            rows.append(
                {
                    "runStartedUtc": run_started,
                    "runFinishedUtc": utc_now_iso(),
                    "tableName": table_name,
                    "rowCount": len(records),
                }
            )
        rows.append(
            {
                "runStartedUtc": run_started,
                "runFinishedUtc": utc_now_iso(),
                "tableName": "api_errors",
                "rowCount": len(self.data.errors),
            }
        )
        self.data.tables["extraction_summary"] = rows


# ---------------------------------------------------------------------------
# Exportação CSV
# ---------------------------------------------------------------------------


DATASET_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    "dashboard_kpis": (
        "1 linha por moeda",
        "KPIs consolidados de patrimônio, saldos, cartões, investimentos e dívidas.",
    ),
    "financial_positions": (
        "1 linha por posição financeira",
        "Visão unificada de contas bancárias, cartões, investimentos e empréstimos.",
    ),
    "positions_by_institution": (
        "instituição x tipo x moeda",
        "Agregação de posições por instituição para composição patrimonial.",
    ),
    "connection_health": (
        "1 linha por ItemId",
        "Status, execução, sincronização e saúde de cada conexão Pluggy.",
    ),
    "items": ("1 linha por ItemId", "Resposta completa de cada conexão/item."),
    "connectors": ("1 linha por connector", "Metadados atuais das instituições/connectors."),
    "consents": ("1 linha por consentimento", "Consentimentos vinculados a cada Item."),
    "identities": ("1 linha por identidade", "Dados de identidade retornados por Item, quando disponíveis."),
    "accounts": ("1 linha por accountId", "Todas as contas BANK e CREDIT com campos específicos achatados."),
    "bank_accounts": ("1 linha por conta BANK", "Somente contas bancárias/correntes/poupança."),
    "credit_cards": ("1 linha por conta CREDIT", "Somente cartões de crédito."),
    "credit_limits": ("1 linha por linha de limite", "Limites desagregados dos cartões quando disponibilizados."),
    "reserved_balances": ("1 linha por reserva/caixinha", "Reservas internas de contas bancárias, quando disponíveis."),
    "reserved_balance_amounts": ("1 linha por valor reservado", "Valores/remuneração das reservas/caixinhas."),
    "realtime_balances": ("1 linha por saldo consultado", "Saldo em tempo real para contas Open Finance quando habilitado."),
    "transactions": ("1 linha por transação", "Todas as transações com contexto de Item/conta e campos normalizados."),
    "card_installments": ("1 linha por transação parcelada", "Compras parceladas identificadas por creditCardMetadata."),
    "credit_card_bills": ("1 linha por fatura", "Faturas de cartão retornadas pela Pluggy."),
    "bill_payments": ("1 linha por pagamento de fatura", "Pagamentos vinculados às faturas."),
    "bill_finance_charges": ("1 linha por encargo", "Juros, IOF, multas e outros encargos de faturas."),
    "investments": ("1 linha por ativo", "Posições de investimento por Item, com rentabilidade calculada quando possível."),
    "investment_transactions": ("1 linha por movimentação", "Aplicações/resgates e demais movimentações por investimento."),
    "investment_allocation": ("instituição x tipo x subtipo x moeda", "Alocação consolidada da carteira de investimentos."),
    "loans": ("1 linha por contrato", "Empréstimos e financiamentos encontrados por Item."),
    "loan_interest_rates": ("1 linha por taxa", "Taxas de juros associadas aos contratos de crédito."),
    "loan_fees": ("1 linha por tarifa", "Tarifas contratadas dos empréstimos."),
    "loan_finance_charges": ("1 linha por encargo", "Encargos financeiros contratados dos empréstimos."),
    "loan_warranties": ("1 linha por garantia", "Garantias vinculadas aos empréstimos."),
    "loan_balloon_payments": ("1 linha por parcela não regular", "Pagamentos balloon/não regulares de contratos."),
    "loan_payment_releases": ("1 linha por pagamento", "Pagamentos/liberações registrados em contratos de crédito."),
    "loan_overparcel_fees": ("1 linha por tarifa", "Tarifas de pagamentos fora da parcela."),
    "loan_overparcel_charges": ("1 linha por encargo", "Encargos de pagamentos fora da parcela."),
    "categories": ("1 linha por categoria", "Árvore de categorias de transações disponível na aplicação."),
    "monthly_cashflow": ("mês x moeda x status", "Fluxo de caixa real de contas BANK: entradas, saídas e líquido."),
    "monthly_cashflow_by_account": ("mês x conta x status", "Fluxo de caixa mensal detalhado por conta bancária."),
    "monthly_spending_by_category": ("mês x origem x categoria", "Gastos agregados sem misturar BANK e CREDIT, evitando suposições de dupla contagem."),
    "monthly_spending_by_merchant": ("mês x origem x estabelecimento", "Gastos agregados por merchant/descrição."),
    "monthly_credit_card_spending": ("mês x cartão x status", "Compras, créditos/pagamentos e atividade líquida de cada cartão."),
    "credit_utilization": ("1 linha por cartão", "Limite, limite disponível, uso estimado e datas de fechamento/vencimento."),
    "api_request_log": ("1 linha por requisição", "Log técnico sem credenciais para auditoria local da extração."),
    "api_errors": ("1 linha por erro", "Erros e avisos ocorridos sem interromper a coleta completa."),
    "extraction_summary": ("1 linha por tabela", "Contagem de linhas e metadados da execução."),
}


DEFAULT_COLUMNS: dict[str, list[str]] = {
    "dashboard_kpis": ["currencyCode", "bankBalance", "investmentNetBalance", "creditCardOpenBalance", "loanOutstandingBalance", "estimatedNetWorth"],
    "financial_positions": ["sourceId", "itemId", "institutionName", "positionType", "name", "currencyCode", "currentAmount", "netWorthContribution"],
    "items": ["id", "institutionName", "status", "executionStatus", "lastUpdatedAt"],
    "accounts": ["id", "itemId", "accountId", "institutionName", "type", "subtype", "name", "balance", "currencyCode"],
    "bank_accounts": ["id", "itemId", "institutionName", "name", "balance", "currencyCode"],
    "credit_cards": ["id", "itemId", "institutionName", "name", "balance", "currencyCode"],
    "transactions": ["id", "itemId", "accountId", "institutionName", "accountName", "accountType", "date", "description", "amount", "currencyCode", "type", "status", "category", "categoryId"],
    "credit_card_bills": ["id", "itemId", "accountId", "institutionName", "accountName", "dueDate", "totalAmount", "totalAmountCurrencyCode"],
    "investments": ["id", "itemId", "institutionName", "name", "type", "subtype", "balance", "currencyCode", "status"],
    "investment_transactions": ["id", "investmentId", "itemId", "institutionName", "date", "tradeDate", "type", "amount", "currencyCode"],
    "loans": ["id", "itemId", "institutionName", "productName", "type", "contractAmount", "currencyCode", "dueDate"],
    "monthly_cashflow": ["month", "currencyCode", "status", "transactionCount", "inflow", "outflow", "netCashflow"],
    "monthly_spending_by_category": ["month", "sourceGroup", "categoryId", "category", "currencyCode", "status", "transactionCount", "expense"],
    "credit_utilization": ["accountId", "itemId", "institutionName", "accountName", "currencyCode", "openBalance", "creditLimit", "availableCreditLimit", "estimatedUsedLimit", "utilizationPct"],
    "api_errors": ["timestampUtc", "severity", "scope", "resourceId", "endpoint", "httpStatus", "message"],
}


PRIORITY_COLUMNS = [
    "id",
    "sourceId",
    "transactionId",
    "investmentId",
    "loanId",
    "billId",
    "itemId",
    "accountId",
    "connectorId",
    "institutionName",
    "connectionInstitutionName",
    "accountName",
    "name",
    "productName",
    "positionType",
    "accountGroup",
    "accountType",
    "type",
    "subtype",
    "status",
    "date",
    "dueDate",
    "month",
    "currencyCode",
    "amount",
    "balance",
]


def csv_columns(records: list[dict[str, Any]], defaults: list[str]) -> list[str]:
    seen: set[str] = set()
    discovered: list[str] = []
    for record in records:
        flat = flatten_dict(record)
        for key in flat.keys():
            if key not in seen:
                seen.add(key)
                discovered.append(key)

    for key in defaults:
        if key not in seen:
            discovered.append(key)
            seen.add(key)

    priority = [col for col in PRIORITY_COLUMNS if col in seen]
    remaining = [col for col in discovered if col not in set(priority)]
    return priority + remaining


def write_csv(path: Path, records: list[dict[str, Any]], defaults: Optional[list[str]] = None) -> None:
    flat_records = [flatten_dict(record) for record in records]
    columns = csv_columns(records, defaults or [])
    if not columns:
        columns = ["_empty"]

    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for record in flat_records:
            clean: dict[str, Any] = {}
            for key in columns:
                value = record.get(key)
                if isinstance(value, (dict, list)):
                    value = json_text(value)
                elif value is None:
                    value = ""
                clean[key] = value
            writer.writerow(clean)


def sort_table(table_name: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records = list(records)
    try:
        if table_name == "transactions":
            return sorted(records, key=lambda r: str(r.get("date") or ""), reverse=True)
        if table_name == "credit_card_bills":
            return sorted(records, key=lambda r: str(r.get("dueDate") or ""), reverse=True)
        if table_name in {
            "monthly_cashflow",
            "monthly_cashflow_by_account",
            "monthly_spending_by_category",
            "monthly_spending_by_merchant",
            "monthly_credit_card_spending",
        }:
            return sorted(records, key=lambda r: str(r.get("month") or ""))
    except Exception:
        pass
    return records


def export_csv_bundle(data: ExtractedData, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    # Cria todos os datasets conhecidos para manter nomes estáveis para o HTML.
    known_tables = list(DATASET_DESCRIPTIONS.keys())
    extra_tables = [t for t in data.tables.keys() if t not in DATASET_DESCRIPTIONS]
    all_tables = known_tables + sorted(extra_tables)

    catalog_rows: list[dict[str, Any]] = []
    for table_name in all_tables:
        records = sort_table(table_name, data.tables.get(table_name, []))
        filename = f"{table_name}.csv"
        write_csv(output_dir / filename, records, DEFAULT_COLUMNS.get(table_name, []))
        grain, description = DATASET_DESCRIPTIONS.get(
            table_name, ("variável", "Tabela adicional gerada pelo exportador.")
        )
        catalog_rows.append(
            {
                "tableName": table_name,
                "fileName": filename,
                "rowCount": len(records),
                "grain": grain,
                "description": description,
            }
        )

    write_csv(
        output_dir / "dataset_catalog.csv",
        catalog_rows,
        ["tableName", "fileName", "rowCount", "grain", "description"],
    )

    # Manifest em CSV para o HTML descobrir a execução sem depender de JSON.
    manifest = [
        {"key": "generatedAtUtc", "value": utc_now_iso()},
        {"key": "baseUrl", "value": BASE_URL},
        {"key": "datasetCount", "value": len(catalog_rows)},
        {"key": "errorCount", "value": len(data.errors)},
        {"key": "transactionEndpoint", "value": "/v2/transactions"},
    ]
    write_csv(output_dir / "manifest.csv", manifest, ["key", "value"])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def validate_date_arg(value: Optional[str], flag_name: str) -> Optional[str]:
    if value is None:
        return None
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        raise ValueError(f"{flag_name} deve estar no formato YYYY-MM-DD")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exporta dados financeiros da Pluggy para CSVs consumíveis por um dashboard local."
    )
    parser.add_argument(
        "--item-id",
        action="append",
        default=[],
        help="Item ID da Pluggy. Pode ser repetido várias vezes.",
    )
    parser.add_argument(
        "--item-file",
        type=Path,
        help="Arquivo texto com um Item ID por linha (ou separados por vírgula).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(DEFAULT_OUTPUT_DIR),
        help=f"Diretório dos CSVs. Padrão: ./{DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--date-from",
        help="Data inicial das transações no formato YYYY-MM-DD. Se omitida, busca todo o histórico armazenado.",
    )
    parser.add_argument(
        "--date-to",
        help="Data final das transações no formato YYYY-MM-DD.",
    )
    parser.add_argument(
        "--realtime-balances",
        action="store_true",
        help="Consulta /accounts/{id}/balance para contas Open Finance. Conta para rate limit da instituição.",
    )
    parser.add_argument("--skip-identity", action="store_true", help="Não consulta /identity.")
    parser.add_argument("--skip-consents", action="store_true", help="Não consulta /consents.")
    parser.add_argument("--skip-loans", action="store_true", help="Não consulta /loans.")
    parser.add_argument("--skip-categories", action="store_true", help="Não consulta /categories.")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Arquivo .env local. Padrão: ./.env",
    )
    return parser.parse_args()


def run_export(
    *,
    output_dir: Path,
    env_file: Path = Path(".env"),
    item_ids: Optional[Iterable[str]] = None,
    item_file: Optional[Path] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    realtime_balances: bool = False,
    include_identity: bool = True,
    include_consents: bool = True,
    include_loans: bool = True,
    include_categories: bool = True,
) -> ExtractedData:
    """
    Executa a exportação de forma reutilizável pelo backend local.

    O backend informa explicitamente a pasta permanente `data` do ControleFin.
    Ela fica fora de `dist`, para que recompilar o executável não apague os dados.
    Os CSVs existentes são sobrescritos pelo export_csv_bundle(), pois
    write_csv() abre cada arquivo em modo "w".
    """
    output_dir = Path(output_dir)
    env_file = Path(env_file)

    load_simple_dotenv(env_file)

    client_id = os.getenv("PLUGGY_CLIENT_ID", "").strip()
    client_secret = os.getenv("PLUGGY_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        raise ValueError(
            "Defina PLUGGY_CLIENT_ID e PLUGGY_CLIENT_SECRET no ambiente ou no .env."
        )

    env_item_ids = [
        part.strip()
        for part in os.getenv("PLUGGY_ITEM_IDS", "").split(",")
        if part.strip()
    ]

    file_item_ids = read_item_ids_from_file(item_file)
    resolved_item_ids = unique_preserve_order(
        [*(item_ids or []), *file_item_ids, *env_item_ids]
    )

    if not resolved_item_ids:
        raise ValueError(
            "Informe ao menos um Item ID via argumento, arquivo ou PLUGGY_ITEM_IDS."
        )

    date_from = validate_date_arg(date_from, "--date-from")
    date_to = validate_date_arg(date_to, "--date-to")

    if date_from and date_to and date_from > date_to:
        raise ValueError("--date-from não pode ser posterior a --date-to.")

    options = ExportOptions(
        date_from=date_from,
        date_to=date_to,
        realtime_balances=realtime_balances,
        include_identity=include_identity,
        include_consents=include_consents,
        include_loans=include_loans,
        include_categories=include_categories,
    )

    print(
        f"Autenticando na Pluggy e exportando {len(resolved_item_ids)} Item(s)...",
        flush=True,
    )

    client = PluggyClient(client_id=client_id, client_secret=client_secret)
    client.authenticate()

    extractor = PluggyFinanceExtractor(client, resolved_item_ids, options)
    data = extractor.run()

    # write_csv() usa modo "w", então cada execução substitui os CSVs anteriores.
    export_csv_bundle(data, output_dir)

    print("\nExportação concluída.")
    print(f"Diretório: {output_dir.resolve()}")
    print(f"Tabelas: {len(DATASET_DESCRIPTIONS)} + catálogo/manifest")
    print(f"Erros/avisos registrados: {len(data.errors)}")
    print("Use dataset_catalog.csv para saber o propósito e a granularidade de cada arquivo.")

    return data


def main() -> int:
    args = parse_args()

    try:
        run_export(
            output_dir=args.output,
            env_file=args.env_file,
            item_ids=args.item_id,
            item_file=args.item_file,
            date_from=args.date_from,
            date_to=args.date_to,
            realtime_balances=args.realtime_balances,
            include_identity=not args.skip_identity,
            include_consents=not args.skip_consents,
            include_loans=not args.skip_loans,
            include_categories=not args.skip_categories,
        )
    except KeyboardInterrupt:
        print("Execução interrompida pelo usuário.", file=sys.stderr)
        return 130
    except (ValueError, FileNotFoundError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Falha fatal: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
