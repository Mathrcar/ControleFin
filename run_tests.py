#!/usr/bin/env python3
"""
ControleFin - executor único da suíte permanente de regressão.

Uso:
    python run_tests.py

Requisitos:
    - mesmo ambiente Python usado pelo ControleFin;
    - Node.js disponível no PATH para os testes do dashboard.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TESTS = ROOT / "tests"


def run(command: list[str], label: str) -> None:
    print()
    print("=" * 88)
    print(label)
    print("=" * 88)
    print(">", " ".join(command))
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def main() -> int:
    required = [
        ROOT / "controlefin_server.py",
        ROOT / "pluggy_finance_export.py",
        ROOT / "controlefin_dashboard.html",
        TESTS / "python",
        TESTS / "js" / "test_dashboard_regressions.js",
    ]

    missing = [str(path) for path in required if not path.exists()]
    if missing:
        print("ERRO: estrutura incompleta da suíte:")
        for path in missing:
            print(" -", path)
        return 2

    node = shutil.which("node")
    if not node:
        print(
            "ERRO: Node.js não foi encontrado no PATH.\n"
            "Os testes Python podem rodar, mas a suíte completa exige Node.js "
            "para validar a lógica JavaScript real do dashboard."
        )
        return 2

    run(
        [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            str(TESTS / "python"),
            "-p",
            "test_*.py",
            "-v",
        ],
        "1/2 - TESTES PYTHON",
    )

    run(
        [
            node,
            str(TESTS / "js" / "test_dashboard_regressions.js"),
        ],
        "2/2 - TESTES JAVASCRIPT DO DASHBOARD",
    )

    print()
    print("=" * 88)
    print("SUÍTE COMPLETA: OK")
    print("=" * 88)
    print(
        "Settings, SQLite, API local, contratos estruturais, CardBankslip, "
        "parcelamentos, fluxo de caixa e projeções passaram."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
