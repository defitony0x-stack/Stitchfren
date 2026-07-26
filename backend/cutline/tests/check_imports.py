"""
Static import-resolution checker for the app/ package tree.

Doesn't execute anything - just parses every .py file with ast and checks
that every "from .foo import bar" / "from app.foo import bar" statement
resolves to a name that actually exists in the target module (function,
class, module-level assignment, or a re-exported import), OR to a real
submodule of a namespace package (this project mostly omits __init__.py,
which is valid PEP 420 namespace packaging, not a bug by itself).
Third-party imports (fastapi, shapely, sqlalchemy, etc.) are skipped
entirely, since those aren't installed in every environment this runs in.

Also explicitly flags relative imports whose level goes beyond the
top-level package (e.g. ".." used from a module that isn't nested deep
enough) - a real ImportError at runtime, and exactly the class of bug
found in app/svg_export.py during this review.

This exists because every bug found in this codebase so far (Measurements,
app.schemas, flip_points, the missing main.py imports, the svg_export.py
".." bug) was exactly this class of error - and it's catchable with zero
dependencies, so there's no excuse for it to reach a VPS/Railway deploy
again.

Usage: python3 tests/check_imports.py   (run from project root)
"""

import ast
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_ROOT = os.path.join(PROJECT_ROOT, "app")

FAILURES = []
CHECKED = 0


def file_to_module_info(filepath: str):
    """Returns (dotted_module_path, is_package). is_package is True if this
    file is an __init__.py (its dotted path already refers to the package
    itself, not a module inside it)."""
    rel = os.path.relpath(filepath, PROJECT_ROOT)
    rel = rel[:-3] if rel.endswith(".py") else rel
    parts = rel.split(os.sep)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return ".".join(parts), is_package


def resolve_relative(current_module: str, is_pkg_init: bool, node: ast.ImportFrom):
    """Returns (target_module_dotted_path, error_or_None)."""
    base = current_module.split(".") if is_pkg_init else current_module.split(".")[:-1]
    strip = node.level - 1  # level=1 ('.') means "base" itself, no stripping
    if strip > 0 and strip >= len(base):
        return None, (
            f"relative import level {node.level} ('{'.' * node.level}') goes "
            f"beyond the top-level package from module '{current_module}' "
            f"(this raises ImportError: attempted relative import beyond "
            f"top-level package at runtime)"
        )
    anchor = base[: len(base) - strip] if strip > 0 else base
    if node.module:
        anchor = anchor + node.module.split(".")
    return ".".join(anchor), None


def resolve_module_location(module_name: str):
    """
    Returns one of:
      ("file", filepath)         - a real X.py module
      ("namespace_pkg", dirpath) - a directory with no __init__.py (PEP 420)
      ("pkg", filepath)          - a directory WITH __init__.py
      (None, None)               - doesn't exist
    """
    rel_parts = module_name.split(".")
    as_file = os.path.join(PROJECT_ROOT, *rel_parts) + ".py"
    if os.path.isfile(as_file):
        return "file", as_file
    as_dir = os.path.join(PROJECT_ROOT, *rel_parts)
    if os.path.isdir(as_dir):
        init_path = os.path.join(as_dir, "__init__.py")
        if os.path.isfile(init_path):
            return "pkg", init_path
        return "namespace_pkg", as_dir
    return None, None


_module_symbol_cache = {}


def get_module_symbols(module_name: str):
    """Returns set of top-level-importable names for module_name, or None
    if it doesn't exist at all."""
    if module_name in _module_symbol_cache:
        return _module_symbol_cache[module_name]

    kind, location = resolve_module_location(module_name)

    if kind is None:
        _module_symbol_cache[module_name] = None
        return None

    if kind == "namespace_pkg":
        # a bare directory: "from pkg import submodule" is always valid if
        # submodule.py or submodule/ exists inside it, regardless of any
        # __init__.py (there isn't one) exporting it explicitly.
        names = set()
        for entry in os.listdir(location):
            full = os.path.join(location, entry)
            if entry.endswith(".py") and entry != "__init__.py":
                names.add(entry[:-3])
            elif os.path.isdir(full) and not entry.startswith("__"):
                names.add(entry)
        _module_symbol_cache[module_name] = names
        return names

    # "file" or "pkg" -> location is an actual .py file to parse
    with open(location) as f:
        source = f.read()
    tree = ast.parse(source, location)

    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, (ast.Tuple, ast.List)):
                    for elt in target.elts:
                        if isinstance(elt, ast.Name):
                            names.add(elt.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name)

    # if this is a package (__init__.py), submodule files are also validly
    # importable via "from pkg import submodule" even without being named
    # explicitly in __init__.py
    if kind == "pkg":
        pkg_dir = os.path.dirname(location)
        for entry in os.listdir(pkg_dir):
            full = os.path.join(pkg_dir, entry)
            if entry.endswith(".py") and entry != "__init__.py":
                names.add(entry[:-3])
            elif os.path.isdir(full) and not entry.startswith("__"):
                names.add(entry)

    _module_symbol_cache[module_name] = names
    return names


def check_file(filepath: str):
    global CHECKED
    own_module, is_pkg_init = file_to_module_info(filepath)
    with open(filepath) as f:
        source = f.read()
    tree = ast.parse(source, filepath)
    relpath = os.path.relpath(filepath, PROJECT_ROOT)

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue

        if node.level and node.level > 0:
            target_module, err = resolve_relative(own_module, is_pkg_init, node)
            if err:
                FAILURES.append(f"{relpath}: {err}")
                continue
        elif node.module and node.module.split(".")[0] == "app":
            target_module = node.module
        else:
            continue  # third-party import, e.g. "from shapely.geometry import Polygon"

        target_names = get_module_symbols(target_module)
        if target_names is None:
            FAILURES.append(
                f"{relpath}: imports from '{target_module}' but that module doesn't exist on disk"
            )
            continue

        for alias in node.names:
            CHECKED += 1
            if alias.name == "*":
                continue
            if alias.name not in target_names:
                FAILURES.append(
                    f"{relpath}: imports '{alias.name}' from '{target_module}', but "
                    f"'{alias.name}' is not defined there "
                    f"(defined names: {sorted(target_names) or 'none'})"
                )


def main():
    py_files = []
    for root, dirs, files in os.walk(APP_ROOT):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fname in files:
            if fname.endswith(".py"):
                py_files.append(os.path.join(root, fname))

    for filepath in sorted(py_files):
        check_file(filepath)

    print(f"Checked {CHECKED} internal import statements across {len(py_files)} files.\n")
    if FAILURES:
        print(f"{len(FAILURES)} BROKEN IMPORT(S):\n")
        for f in FAILURES:
            print(" -", f)
        sys.exit(1)
    else:
        print("ALL INTERNAL IMPORTS RESOLVE CLEANLY")


if __name__ == "__main__":
    main()
