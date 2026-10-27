"""Plugin inspection, testing, and scaffolding CLI commands."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import typer

from algodesk.api.conformance import kind_problems
from algodesk.api.kinds import Mode
from algodesk.cli.common import _get_cli_state, _open_rt, format_table, handle_cli_errors
from algodesk.ops.context import build_context
from algodesk.registry.base_checks import base_contract_problems
from algodesk.registry.scaffold import scaffold_plugin

plugins_app = typer.Typer(help="Discover, check, and test plugins.")
new_app = typer.Typer(help="Scaffold new components.")


@plugins_app.command("list")
@handle_cli_errors
def plugins_list(
    ctx: typer.Context,
    json_opt: bool = typer.Option(False, "--json", help="Output machine-readable JSON"),
) -> None:
    """List discovered plugins and their conformance status."""
    state = _get_cli_state(ctx)
    is_json = json_opt or state.json
    with _open_rt(ctx, ensure=False) as rt:
        plugins = rt.plugins.list()
        items: list[dict[str, Any]] = []
        for p in plugins:
            conf = rt.plugins.conformance_status(p.kind, p.name)
            items.append(
                {
                    "kind": p.kind.value,
                    "name": p.name,
                    "version": p.manifest.version,
                    "order_capable": p.manifest.order_capable,
                    "conformance": conf,
                }
            )

    if is_json:
        typer.echo(json.dumps(items, indent=2))
    else:
        headers = ["KIND", "NAME", "VERSION", "ORDER_CAPABLE", "CONFORMANCE"]
        rows = [
            [
                item["kind"],
                item["name"],
                item["version"],
                str(item["order_capable"]).lower(),
                item["conformance"],
            ]
            for item in items
        ]
        typer.echo(format_table(headers, rows))


@plugins_app.command("check")
@handle_cli_errors
def plugins_check(ctx: typer.Context) -> None:
    """Validate discovered plugin manifests and structure."""
    with _open_rt(ctx, ensure=False) as rt:
        rt.plugins.refresh()
        issues = rt.plugins.issues

    if not issues:
        typer.echo("OK: all plugins passed discovery and manifest checks")
    else:
        typer.echo(f"{len(issues)} issue(s) detected across discovered plugins:")
        for issue in issues:
            typer.echo(f"  {issue.path}: {issue.what} (Fix: {issue.fix})")
        raise typer.Exit(code=1)


@plugins_app.command("test")
@handle_cli_errors
def plugins_test(
    ctx: typer.Context,
    kind: str = typer.Argument(..., help="Plugin kind (e.g. notifier, venue)"),
    name: str = typer.Argument(..., help="Plugin name"),
) -> None:
    """Run tests for plugin and verify contract adherence."""
    with _open_rt(ctx, ensure=True) as rt:
        p_info = rt.plugins.info(kind, name)
        tests_dir = p_info.directory / "tests"
        if tests_dir.is_dir() and any(tests_dir.glob("test_*.py")):
            cmd = [sys.executable, "-m", "pytest", str(tests_dir), "-q"]
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                typer.echo(f"Plugin tests failed (exit code {proc.returncode}):")
                if proc.stdout:
                    typer.echo(proc.stdout.strip())
                if proc.stderr:
                    typer.echo(proc.stderr.strip())
                raise typer.Exit(code=1)

        if not p_info.manifest.order_capable:
            p_ctx = build_context(Mode.RESEARCH, run_id=f"test_{kind}_{name}")
            instance = rt.plugins.create(p_info.kind, p_info.name, ctx=p_ctx)
            problems = base_contract_problems(instance)
            if not problems:
                problems = kind_problems(instance)
            if problems:
                typer.echo(f"Conformance problems found in {kind}/{name}:")
                for prob in problems:
                    typer.echo(f"  - {prob}")
                raise typer.Exit(code=1)

        rt.plugins.record_conformance(p_info.kind, p_info.name, suite="base+plugin-tests")
        typer.echo(f"OK: {kind}/{name} passed conformance suite 'base+plugin-tests'")


@new_app.command("plugin")
@handle_cli_errors
def new_plugin(
    ctx: typer.Context,
    kind: str = typer.Argument(..., help="Plugin kind (e.g. notifier, venue)"),
    name: str = typer.Argument(..., help="Plugin name"),
) -> None:
    """Scaffold a new plugin in the primary plugin directory."""
    with _open_rt(ctx, ensure=True) as rt:
        dest_dir = rt.paths.plugin_dirs[0]
        target = scaffold_plugin(dest_dir, kind, name)
        typer.echo(f"Scaffolded new plugin {kind}/{name} at {target}")
