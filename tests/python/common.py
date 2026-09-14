from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_module(module_name: str, filename: str) -> ModuleType:
    """
    Carrega um módulo de produção diretamente da raiz do ControleFin.

    Usa um nome exclusivo para não colidir com imports normais durante os testes.
    """
    path = PROJECT_ROOT / filename
    if not path.exists():
        raise FileNotFoundError(path)

    root_text = str(PROJECT_ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)

    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing

    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Não foi possível carregar {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_server() -> ModuleType:
    return load_module(
        "controlefin_server_under_test",
        "controlefin_server.py",
    )


def load_exporter() -> ModuleType:
    return load_module(
        "pluggy_finance_export_under_test",
        "pluggy_finance_export.py",
    )
