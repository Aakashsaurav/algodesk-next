"""Plugin inspection, testing, and scaffolding CLI commands."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

import typer

from algodesk.api.conformance import kind_problems
from algodesk.api.errors import ConfigError
from algodesk.api.kinds import Mode
from algodesk.cli.common import _get_cli_state, _open_rt, format_table, handle_cli_errors
from algodesk.config.snapshot import build_effective
from algodesk.ops.context import build_context
from algodesk.registry import get_run_config_plugins
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
    kind: str | None = typer.Argument(None, help="Plugin kind (e.g. notifier, venue)"),
    name: str | None = typer.Argument(None, help="Plugin name"),
    all_plugins: bool = typer.Option(
        False, "--all", help="Run conformance suite for all plugins used by run config or workspace"
    ),
    run: str | None = typer.Option(
        None, "--run", help="Run configuration name to test plugins for"
    ),
    force: bool = typer.Option(
        False, "--force", help="Force running tests even if code hash has a current pass"
    ),
) -> None:
    """Run tests for plugin and verify contract adherence."""
    with _open_rt(ctx, ensure=True) as rt:
        targets: list[tuple[str, str]] = []
        if all_plugins or run:
            if run:
                eff = build_effective(rt.root, run_name=run)
                if eff.run is None:
                    raise ConfigError(
                        what=f"Run configuration '{run}' not found",
                        where=f"config/runs/{run}.toml",
                        fix=f"Verify configuration file exists for '{run}'",
                    )
                for pk, pn in get_run_config_plugins(eff.run):
                    targets.append((pk.value if hasattr(pk, "value") else str(pk), pn))
            else:
                for p in rt.plugins.list():
                    targets.append(
                        (p.kind.value if hasattr(p.kind, "value") else str(p.kind), p.name)
                    )
        elif kind and name:
            targets.append((kind, name))
        else:
            typer.echo("Error: specify plugin kind and name, or use '--all'.")
            raise typer.Exit(code=1)

        # Remove duplicate targets while preserving order
        seen: set[tuple[str, str]] = set()
        deduped: list[tuple[str, str]] = []
        for t in targets:
            if t not in seen:
                seen.add(t)
                deduped.append(t)

        for t_kind, t_name in deduped:
            p_info = rt.plugins.info(t_kind, t_name)
            if (
                all_plugins
                and not force
                and rt.plugins.conformance_current(p_info.kind, p_info.name)
            ):
                typer.echo(f"OK: {t_kind}/{t_name} skipped (current)")
                continue

            tests_dir = p_info.directory / "tests"
            if tests_dir.is_dir() and any(tests_dir.glob("test_*.py")):
                cmd = [sys.executable, "-m", "pytest", str(tests_dir), "-q"]
                env = {**os.environ, "ALGODESK_NO_BROWSER": "1"}
                proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
                if proc.returncode != 0:
                    typer.echo(
                        f"Plugin tests failed for {t_kind}/{t_name} (exit code {proc.returncode}):"
                    )
                    if proc.stdout:
                        typer.echo(proc.stdout.strip())
                    if proc.stderr:
                        typer.echo(proc.stderr.strip())
                    raise typer.Exit(code=1)

            if not p_info.manifest.order_capable:
                p_ctx = build_context(Mode.RESEARCH, run_id=f"test_{t_kind}_{t_name}")
                instance = rt.plugins.create(p_info.kind, p_info.name, ctx=p_ctx)
                problems = base_contract_problems(instance)
                if not problems:
                    problems = kind_problems(instance)
                if problems:
                    typer.echo(f"Conformance problems found in {t_kind}/{t_name}:")
                    for prob in problems:
                        typer.echo(f"  - {prob}")
                    raise typer.Exit(code=1)

            rt.plugins.record_conformance(p_info.kind, p_info.name, suite="base+plugin-tests")
            typer.echo(f"OK: {t_kind}/{t_name} passed conformance suite 'base+plugin-tests'")


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
