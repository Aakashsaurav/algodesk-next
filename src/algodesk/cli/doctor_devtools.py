"""Doctor health checks, living documentation, and devtools CLI commands."""

from __future__ import annotations

import json
from dataclasses import asdict

import typer

from algodesk.cli.common import _get_cli_state, _open_rt, handle_cli_errors
from algodesk.ops.doctor import DoctorRegistry, format_report, report_to_json

docs_app = typer.Typer(help="Inspect, check, and generate living documentation.")
api_app = typer.Typer(help="Inspect and generate API index.")


@docs_app.command("check")
@handle_cli_errors
def docs_check_cmd(
    ctx: typer.Context,
    json_opt: bool = typer.Option(False, "--json", help="Output machine-readable JSON"),
) -> None:
    """Validate documentation consistency, summary sections, and relative links."""
    from algodesk.devtools.docs import format_docs_text, run_docs_check

    state = _get_cli_state(ctx)
    is_json = json_opt or state.json
    with _open_rt(ctx, ensure=False) as rt:
        rep = run_docs_check(rt.root, params=rt.params)

    if is_json:
        data = {
            "summary": {
                "checked": rep.checked,
                "errors": len(rep.errors),
                "warnings": len(rep.warnings),
                "exit_code": rep.exit_code,
            },
            "findings": [asdict(f) for f in rep.findings],
        }
        typer.echo(json.dumps(data, indent=2))
    else:
        typer.echo(format_docs_text(rep))

    if rep.exit_code != 0:
        raise typer.Exit(code=rep.exit_code)


@docs_app.command("index")
@handle_cli_errors
def docs_index_cmd(ctx: typer.Context) -> None:
    """Regenerate deterministic docs/INDEX.md living documentation index."""
    from algodesk.devtools.docs import write_index

    with _open_rt(ctx, ensure=False) as rt:
        p = write_index(rt.root)
    typer.echo(f"OK: regenerated {p.relative_to(rt.root).as_posix()}")


@api_app.command("index")
@handle_cli_errors
def api_index_cmd(ctx: typer.Context) -> None:
    """Regenerate _workbench/API_INDEX.md AST-based symbol index."""
    from algodesk.devtools.api_index import write_api_index

    with _open_rt(ctx, ensure=False) as rt:
        out_p = write_api_index(rt.root)
    typer.echo(f"OK: generated {out_p.relative_to(rt.root).as_posix()}")


@handle_cli_errors
def doctor_cmd(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose diagnostic output"),
    only: str | None = typer.Option(None, "--only", help="Run only check with this id"),
    run: str | None = typer.Option(None, "--run", help="Run configuration name"),
    json_opt: bool = typer.Option(False, "--json", help="Output machine-readable JSON"),
) -> None:
    """Run platform and component diagnostic health checks."""
    state = _get_cli_state(ctx)
    is_json = json_opt or state.json
    run_name = run or state.run
    with _open_rt(ctx, ensure=False) as rt:
        import sys

        from algodesk.ops.checks import build_core_checks as default_build_checks

        app_mod = sys.modules.get("algodesk.cli.app")
        build_checks_fn = getattr(app_mod, "build_core_checks", default_build_checks)
        checks = build_checks_fn(rt)
        from algodesk.devtools import build_tool_checks
        from algodesk.ops.checks import build_run_checks

        checks.extend(build_tool_checks(rt))
        if run_name:
            checks.extend(build_run_checks(rt, run_name))
        registry = DoctorRegistry()
        registry.register_many(checks)
        report = registry.run(only=only)

    if is_json:
        typer.echo(json.dumps(report_to_json(report), indent=2))
    else:
        typer.echo(format_report(report, verbose=verbose))

    if report.exit_code != 0:
        raise typer.Exit(code=report.exit_code)


@handle_cli_errors
def budget_cmd(
    ctx: typer.Context,
    json_opt: bool = typer.Option(False, "--json", help="Output machine-readable JSON"),
) -> None:
    """Evaluate codebase size and dependency complexity against budget."""
    from algodesk.devtools.budget import (
        format_budget_json,
        format_budget_text,
        run_budget,
    )

    state = _get_cli_state(ctx)
    is_json = json_opt or state.json
    with _open_rt(ctx, ensure=False) as rt:
        rep = run_budget(rt.root, params=rt.params)

    if is_json:
        typer.echo(format_budget_json(rep))
    else:
        typer.echo(format_budget_text(rep))

    if rep.exit_code != 0:
        raise typer.Exit(code=rep.exit_code)


@handle_cli_errors
def lint_cmd(
    ctx: typer.Context,
    json_opt: bool = typer.Option(False, "--json", help="Output machine-readable JSON"),
) -> None:
    """Run AST architecture rules over core code and plugins."""
    from algodesk.devtools.archlint import (
        format_archlint_json,
        format_archlint_text,
        run_archlint,
    )

    state = _get_cli_state(ctx)
    is_json = json_opt or state.json
    with _open_rt(ctx, ensure=False) as rt:
        rep = run_archlint(rt.root)

    if is_json:
        typer.echo(format_archlint_json(rep))
    else:
        typer.echo(format_archlint_text(rep))

    if rep.exit_code != 0:
        raise typer.Exit(code=rep.exit_code)
