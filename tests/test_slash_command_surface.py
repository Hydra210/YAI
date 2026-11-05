import ast
from pathlib import Path

from moderation import Mod
from slang import Slang

ROOT = Path(__file__).resolve().parents[1]


class EmptyMemory:
    def fast_get(self, scope, key):
        return None


def _decorated_group_commands(source: str, group_name: str) -> set[str]:
    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            target = decorator.func.value
            if not isinstance(target, ast.Name) or target.id != group_name or decorator.func.attr != "command":
                continue
            name = next((kw.value.value for kw in decorator.keywords
                         if kw.arg == "name" and isinstance(kw.value, ast.Constant)), node.name)
            names.add(name)
    return names


def test_mod_group_keeps_only_diagnostics():
    group = Mod(None, EmptyMemory(), None, None).command_group()
    assert {command.name for command in group.commands} == {"status", "refresh", "test"}


def test_slang_group_keeps_only_read_only_status_and_list():
    mod = Mod(None, EmptyMemory(), None, None)
    group = Slang(None, EmptyMemory(), None, None, mod).command_group()
    assert {command.name for command in group.commands} == {"status", "list"}


def test_bot_no_longer_registers_redundant_configuration_commands():
    source = (ROOT / "bot.py").read_text()
    tree = ast.parse(source)
    tree_names = set()
    yukiban_names = _decorated_group_commands(source, "yukiban_group")
    mod_names = _decorated_group_commands(source, "mod_group")

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if decorator.func.attr != "command":
                continue
            target = decorator.func.value
            if (isinstance(target, ast.Attribute) and target.attr == "tree"
                    and isinstance(target.value, ast.Name) and target.value.id == "client"):
                name = next((kw.value.value for kw in decorator.keywords
                             if kw.arg == "name" and isinstance(kw.value, ast.Constant)), node.name)
                tree_names.add(name)

    assert "pingstatus" in tree_names
    assert "pingcontrol" not in tree_names
    assert mod_names == set()
    assert yukiban_names == {"global", "global_unban", "list"}
