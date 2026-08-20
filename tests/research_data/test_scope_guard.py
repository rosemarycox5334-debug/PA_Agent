import ast
import inspect
from pathlib import Path

from pa_agent.research_data.binance_public import PUBLIC_PATHS, BinancePublicClient

PACKAGE = Path("pa_agent/research_data")


def test_package_has_no_second_batch_module_names():
    forbidden = {"strategy.py", "positions.py", "backtest.py", "ledger.py", "matching.py", "llm.py"}
    assert forbidden.isdisjoint(path.name for path in PACKAGE.glob("*.py"))


def test_package_does_not_import_gui_ai_strategy_position_or_ledger_modules():
    forbidden_prefixes = (
        "pa_agent.ai",
        "pa_agent.gui",
        "pa_agent.indicators",
        "pa_agent.orchestrator",
        "pa_agent.records",
    )
    violations = []
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name.startswith(forbidden_prefixes) for name in names):
                violations.append((str(path), names))
    assert violations == []


def test_public_client_exposes_no_write_or_generic_request_method():
    public_methods = {
        name
        for name, member in inspect.getmembers(BinancePublicClient, inspect.isfunction)
        if not name.startswith("_")
    }
    assert public_methods == {"get_json"}
    assert all("account" not in path.lower() and "order" not in path.lower() for path in PUBLIC_PATHS)


def test_source_contains_no_create_order_call():
    compact_source = "\n".join(path.read_text(encoding="utf-8") for path in PACKAGE.glob("*.py"))
    assert "create_order(" not in compact_source
