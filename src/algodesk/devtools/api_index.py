"""AST-based API index generator for AlgoDesk packages and plugins."""

from __future__ import annotations

import ast
from pathlib import Path


def _format_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef, prefix: str = "", rel_path: str = ""
) -> str:
    """Format a function or method signature as a single line with line range."""
    args_str = ast.unparse(node.args).replace("\n", " ").strip()
    ret_str = f" -> {ast.unparse(node.returns).strip()}" if node.returns else ""
    async_prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    name = f"{prefix}{node.name}"
    start_line = getattr(node, "lineno", 1)
    end_line = getattr(node, "end_lineno", start_line)
    loc_prefix = f"{rel_path}:{start_line}-{end_line}: " if rel_path else ""
    return f"- {loc_prefix}`{async_prefix}{name}({args_str}){ret_str}`"


def _format_class(node: ast.ClassDef, rel_path: str = "") -> str:
    """Format a class with its bases, fields, or primary methods as a single line with line range."""
    bases_str = f"({', '.join(ast.unparse(b) for b in node.bases)})" if node.bases else ""
    fields: list[str] = []
    for item in node.body:
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            if not item.target.id.startswith("_"):
                ann = ast.unparse(item.annotation).replace("\n", " ")
                fields.append(f"{item.target.id}: {ann}")
    start_line = getattr(node, "lineno", 1)
    end_line = getattr(node, "end_lineno", start_line)
    loc_prefix = f"{rel_path}:{start_line}-{end_line}: " if rel_path else ""
    if fields:
        return f"- {loc_prefix}`class {node.name}{bases_str}`: {', '.join(fields)}"
    return f"- {loc_prefix}`class {node.name}{bases_str}`"


def extract_public_symbols(file_path: Path, rel_path: str = "") -> list[str]:
    """Parse a python file using AST and return one-line signatures of public symbols."""
    try:
        content = file_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(file_path))
    except Exception:
        return []

    lines: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                lines.append(_format_function(node, rel_path=rel_path))
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                lines.append(_format_class(node, rel_path=rel_path))
                # Also list public methods of the class
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if not member.name.startswith("_") or member.name == "__init__":
                            lines.append(
                                _format_function(member, prefix=f"{node.name}.", rel_path=rel_path)
                            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if not node.target.id.startswith("_"):
                ann = ast.unparse(node.annotation).replace("\n", " ")
                start_l = getattr(node, "lineno", 1)
                end_l = getattr(node, "end_lineno", start_l)
                loc_prefix = f"{rel_path}:{start_l}-{end_l}: " if rel_path else ""
                lines.append(f"- {loc_prefix}`{node.target.id}: {ann}`")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    val = ast.unparse(node.value).replace("\n", " ")
                    if len(val) > 60:
                        val = val[:57] + "..."
                    start_l = getattr(node, "lineno", 1)
                    end_l = getattr(node, "end_lineno", start_l)
                    loc_prefix = f"{rel_path}:{start_l}-{end_l}: " if rel_path else ""
                    lines.append(f"- {loc_prefix}`{target.id} = {val}`")

    return lines


def generate_api_index(root: Path) -> str:
    """Generate the full API index markdown document."""
    lines: list[str] = [
        "# AlgoDesk Next API Index",
        "",
        "## Summary",
        "Comprehensive AST-generated index of all public packages, classes, functions, "
        "and plugin interfaces in the AlgoDesk Next workbench codebase. "
        "Kept updated for subagents and developers.",
        "",
    ]

    src_dir = root / "src" / "algodesk"
    if src_dir.is_dir():
        packages = sorted(
            [p for p in src_dir.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))],
            key=lambda p: p.name,
        )

        for pkg in packages:
            lines.append(f"## algodesk.{pkg.name}")
            pkg_lines: list[str] = []
            py_files = sorted(pkg.rglob("*.py"), key=lambda f: str(f))
            for py_file in py_files:
                if any(
                    part.startswith((".", "_")) and part != "__init__.py" for part in py_file.parts
                ):
                    continue
                rel = py_file.relative_to(root).as_posix()
                symbols = extract_public_symbols(py_file, rel_path=rel)
                if symbols:
                    pkg_lines.append(f"### {rel}")
                    pkg_lines.extend(symbols)
            if pkg_lines:
                lines.extend(pkg_lines)
            else:
                lines.append("- *(no public symbols exported)*")
            lines.append("")

    plugins_dir = root / "plugins"
    if plugins_dir.is_dir():
        lines.append("## Plugins")
        lines.append("")
        plugin_dirs: list[Path] = []
        for p in sorted(plugins_dir.rglob("manifest.toml")):
            plugin_dirs.append(p.parent)

        for pdir in sorted(plugin_dirs, key=lambda p: str(p)):
            rel_plugin = pdir.relative_to(root)
            lines.append(f"### {rel_plugin}")
            p_lines: list[str] = []
            for py_file in sorted(pdir.rglob("*.py"), key=lambda f: str(f)):
                if "proto" in py_file.parts or any(part.startswith(".") for part in py_file.parts):
                    continue
                rel_f = py_file.relative_to(root).as_posix()
                symbols = extract_public_symbols(py_file, rel_path=rel_f)
                if symbols:
                    p_lines.extend(symbols)
            if p_lines:
                lines.extend(p_lines)
            else:
                lines.append("- *(no public symbols)*")
            lines.append("")

    return "\n".join(lines).strip() + "\n"


def write_api_index(root: Path) -> Path:
    """Generate and write API_INDEX.md to _workbench/."""
    wb_dir = root / "_workbench"
    wb_dir.mkdir(parents=True, exist_ok=True)
    out_file = wb_dir / "API_INDEX.md"
    content = generate_api_index(root)
    out_file.write_text(content, encoding="utf-8")
    return out_file
