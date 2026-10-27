"""Shared CLI state, utilities, and execution wrappers."""

from __future__ import annotations

import functools
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import typer

from algodesk.api.errors import AlgoDeskError, ConfigError
from algodesk.ops.runtime import Runtime, open_runtime


@dataclass
class CliState:
    """Global CLI configuration state passed from main callback."""

    root: Path | None = None
    profile: str | None = None
    run: str | None = None
    json: bool = False
    debug: bool = False


def _get_cli_state(ctx: Any = None) -> CliState:
    cur = ctx if ctx is not None else click.get_current_context(silent=True)
    while cur is not None:
        if getattr(cur, "obj", None) is not None and isinstance(cur.obj, CliState):
            return cur.obj
        cur = getattr(cur, "parent", None)
    return CliState()


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render plain aligned text table without rich dependencies."""
    if not headers and not rows:
        return ""
    col_count = max(len(headers), max((len(r) for r in rows), default=0))
    p_headers = headers + [""] * (col_count - len(headers))
    p_rows = [r + [""] * (col_count - len(r)) for r in rows]

    widths = [
        max(len(str(p_headers[i])), max((len(str(r[i])) for r in p_rows), default=0))
        for i in range(col_count)
    ]
    lines = ["  ".join(p_headers[i].ljust(widths[i]) for i in range(col_count))]
    for r in p_rows:
        lines.append("  ".join(str(r[i]).ljust(widths[i]) for i in range(col_count)))
    return "\n".join(lines)


def handle_cli_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Intercept errors and format three-line AlgoDeskError or bug message."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        ctx: Any = next(
            (a for a in args if hasattr(a, "obj") and hasattr(a, "parent")),
            next((v for v in kwargs.values() if hasattr(v, "obj") and hasattr(v, "parent")), None),
        )
        state = _get_cli_state(ctx)
        debug = state.debug or ("--debug" in sys.argv)
        try:
            return fn(*args, **kwargs)
        except typer.Exit:
            raise
        except (typer.BadParameter, click.UsageError, click.exceptions.ClickException):
            raise
        except AlgoDeskError as err:
            if debug:
                typer.echo(traceback.format_exc().rstrip())
            typer.echo(f"ERROR: {err.what}")
            typer.echo(f"WHERE: {err.where}")
            typer.echo(f"FIX: {err.fix}")
            raise typer.Exit(code=1) from err
        except Exception as exc:
            if debug:
                typer.echo(traceback.format_exc().rstrip())
            typer.echo(f"INTERNAL ERROR (bug): {type(exc).__name__}: {exc}")
            typer.echo("run with --debug for the traceback")
            raise typer.Exit(code=70) from exc

    return wrapper


def _open_rt(ctx: typer.Context, *, ensure: bool = False) -> Runtime:
    state = _get_cli_state(ctx)
    if state.root is not None:
        root_path = Path(state.root).resolve()
        pyproj = root_path / "pyproject.toml"
        is_algo = False
        if pyproj.is_file():
            try:
                import tomllib

                with open(pyproj, "rb") as f:
                    data = tomllib.load(f)
                is_algo = data.get("project", {}).get("name") == "algodesk"
            except Exception:
                pass
        if not is_algo:
            raise ConfigError(
                what="Project root not found: no pyproject.toml with [project].name = 'algodesk'",
                where=str(root_path),
                fix="Run from within an AlgoDesk project or specify --root",
            )
    return open_runtime(
        root=state.root,
        run=state.run,
        profile=state.profile,
        ensure=ensure,
    )
