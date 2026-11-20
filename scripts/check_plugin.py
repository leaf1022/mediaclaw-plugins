#!/usr/bin/env python3
"""Validate a MediaClaw plugin without depending on the public MovieClaw fork."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

from pluginlib import PluginError, dependency_lock, load_store, permissions_of, plugin_table

FORBIDDEN = ("movieclaw", "MovieClaw")


def _source_files(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.rglob("*.py")
        if "__pycache__" not in path.parts and ".venv" not in path.parts
    )


def check(directory: Path) -> None:
    """Check manifest, branding, syntax, imports, web manifest, and catalog metadata."""
    table = plugin_table(directory)
    dependency_lock(directory)
    permissions_of(directory)
    load_store(directory)

    entry = str(table.get("entry") or "").strip()
    if not entry:
        raise PluginError("[plugin].entry 必填")
    entry_path = directory.joinpath(*entry.split("."))
    if not (entry_path.is_file() or (entry_path / "__init__.py").is_file()):
        raise PluginError(f"找不到入口模块：{entry}")

    for path in _source_files(directory):
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise PluginError(f"{path} 读取失败：{exc}") from exc
        leaked = [name for name in FORBIDDEN if name in source]
        if leaked:
            raise PluginError(f"{path} 仍含旧品牌名：{'、'.join(leaked)}")
        try:
            tree = ast.parse(source, filename=str(path))
            compile(source, str(path), "exec")
        except (SyntaxError, UnicodeError) as exc:
            raise PluginError(f"{path} 语法错误：{exc}") from exc
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(name.lower().startswith("movieclaw") for name in names):
                raise PluginError(f"{path}:{node.lineno} 导入了旧 MovieClaw 包")

    for path in directory.rglob("manifest.webmanifest"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise PluginError(f"{path} 不是合法的 web manifest：{exc}") from exc
        if not isinstance(data, dict):
            raise PluginError(f"{path} 须是 JSON 对象")

    print(f"✓ {table['id']} {table['version']}：MediaClaw 格式检查通过")


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python3 scripts/check_plugin.py <plugin-dir> [...]", file=sys.stderr)
        return 2
    try:
        for item in argv:
            check(Path(item))
    except PluginError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
