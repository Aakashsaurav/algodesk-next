"""AlgoDesk command-line interface implementation with Typer."""

from __future__ import annotations

import functools
import getpass
import json
import subprocess
import sys
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any

import click
import typer

from algodesk.api.errors import AlgoDeskError, ConfigError, ParamError
from algodesk.api.kinds import Mode
from algodesk.config.snapshot import render_effective
from algodesk.journal.store import runs, tail, verify_journal
from algodesk.ops.checks import build_core_checks
from algodesk.ops.context import build_context
from algodesk.ops.doctor import DoctorRegistry, format_report, report_to_json
from algodesk.ops.runtime import Runtime, open_runtime
from algodesk.params.specs import parse_cli_value
from algodesk.registry.base_checks import base_contract_problems
from algodesk.registry.scaffold import scaffold_plugin


@dataclass
class CliState:
    """Global CLI configuration state passed from main callback."""

    root: Path | None = None
    profile: str | None = None
    run: str | None = None
    json: bool = False
    debug: bool = False


app = typer.Typer(
    name="algodesk",
    help="AlgoDesk Next — pluggable trading workbench for Indian markets.",
    no_args_is_help=True,
)

param_app = typer.Typer(help="Inspect and modify parameters in the parameter registry.")
plugins_app = typer.Typer(help="Discover, check, and test plugins.")
journal_app = typer.Typer(help="Inspect, tail, and verify append-only journal.")
config_app = typer.Typer(help="Inspect effective platform and run configuration.")
new_app = typer.Typer(help="Scaffold new components.")
docs_app = typer.Typer(help="Inspect, check, and generate living documentation.")
secrets_app = typer.Typer(help="Scan codebase for exposed secrets and tokens.")

app.add_typer(param_app, name="param")
app.add_typer(plugins_app, name="plugins")
app.add_typer(journal_app, name="journal")
app.add_typer(config_app, name="config")
app.add_typer(new_app, name="new")
app.add_typer(docs_app, name="docs")
app.add_typer(secrets_app, name="secrets")


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


@app.callback()
def main_callback(
    ctx: typer.Context,
    root: Annotated[Path | None, typer.Option("--root", help="Workspace root directory")] = None,
    profile: Annotated[str | None, typer.Option("--profile", help="Configuration profile")] = None,
    run: Annotated[str | None, typer.Option("--run", help="Run configuration name")] = None,
    json_opt: Annotated[bool, typer.Option("--json", help="Machine-readable JSON output")] = False,
    debug: Annotated[bool, typer.Option("--debug", help="Print debug traceback on errors")] = False,
) -> None:
    """AlgoDesk command-line workbench."""
    ctx.obj = CliState(root=root, profile=profile, run=run, json=json_opt, debug=debug)


@app.command("version")
@handle_cli_errors
def version_cmd() -> None:
    """Print AlgoDesk version."""
    typer.echo("algodesk 0.0.0")


@app.command("doctor")
@handle_cli_errors
def doctor_cmd(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose diagnostic output"),
    only: str | None = typer.Option(None, "--only", help="Run only check with this id"),
    json_opt: bool = typer.Option(False, "--json", help="Output machine-readable JSON"),
) -> None:
    """Run platform and component diagnostic health checks."""
    state = _get_cli_state(ctx)
    is_json = json_opt or state.json
    with _open_rt(ctx, ensure=False) as rt:
        checks = build_core_checks(rt)
        from algodesk.devtools import build_tool_checks

        checks.extend(build_tool_checks(rt))
        registry = DoctorRegistry()
        registry.register_many(checks)
        report = registry.run(only=only)

    if is_json:
        typer.echo(json.dumps(report_to_json(report), indent=2))
    else:
        typer.echo(format_report(report, verbose=verbose))

    if report.exit_code != 0:
        raise typer.Exit(code=report.exit_code)


@config_app.command("show")
@handle_cli_errors
def config_show(ctx: typer.Context) -> None:
    """Show effective configuration snapshot."""
    state = _get_cli_state(ctx)
    with _open_rt(ctx, ensure=False) as rt:
        if state.json:
            typer.echo(json.dumps(rt.effective.platform.model_dump(mode="json"), indent=2))
        else:
            typer.echo(render_effective(rt.effective))


@param_app.command("show")
@handle_cli_errors
def param_show(
    ctx: typer.Context,
    prefix: str = typer.Argument("", help="Parameter prefix to filter"),
    json_opt: bool = typer.Option(False, "--json", help="Output machine-readable JSON"),
) -> None:
    """Show registered parameters and their effective values."""
    state = _get_cli_state(ctx)
    is_json = json_opt or state.json
    with _open_rt(ctx, ensure=False) as rt:
        if rt.params is not None:
            rows = rt.params.show(prefix)
        else:
            # Fallback to in-memory defaults if database not initialized
            from algodesk.params.registry import ParamRegistry

            mem_reg = ParamRegistry(":memory:")
            defaults_dir = rt.root / "config" / "params"
            if defaults_dir.is_dir():
                mem_reg.load_defaults(defaults_dir)
            rows = mem_reg.show(prefix)

    if is_json:
        data = [
            {
                "name": r.name,
                "value": r.value,
                "default": r.default,
                "scope": r.scope,
                "last_change": r.last_change,
            }
            for r in rows
        ]
        typer.echo(json.dumps(data, indent=2))
    else:
        headers = ["NAME", "VALUE", "DEFAULT", "SCOPE", "LAST_CHANGE"]
        table_rows = [
            [r.name, str(r.value), str(r.default), r.scope, str(r.last_change or "-")] for r in rows
        ]
        typer.echo(format_table(headers, table_rows))


@param_app.command("set")
@handle_cli_errors
def param_set(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Parameter name"),
    value: str = typer.Argument(..., help="New parameter value"),
    reason: str = typer.Option(..., "--reason", "-r", help="Mandatory reason for change"),
    scope: str = typer.Option("global", "--scope", "-s", help="Parameter scope"),
    confirm: str | None = typer.Option(None, "--confirm", help="Confirmation phrase for loosen"),
) -> None:
    """Set parameter value override with audit logging."""
    if not reason or not reason.strip():
        raise typer.BadParameter("Reason must not be empty", param_hint="--reason")

    with _open_rt(ctx, ensure=True) as rt:
        if rt.params is None:
            raise ParamError(
                what="Parameter registry database could not be opened",
                where=name,
                fix="Check database permissions in data/state/params.db",
            )
        try:
            spec = rt.params.spec(name)
        except ParamError as exc:
            raise ParamError(
                what=f"Cannot operate on unknown parameter '{name}'",
                where=name,
                fix="Register parameter spec before operating on it",
            ) from exc
        parsed_val = parse_cli_value(spec, value)
        by = getpass.getuser()
        res = rt.params.set(
            name,
            parsed_val,
            reason=reason,
            scope=scope,
            confirm=confirm,
            by=by,
            channel="cli",
        )
        typer.echo(f"OK: {res.name} -> {res.new_value} ({res.direction}, {res.status})")


@param_app.command("history")
@handle_cli_errors
def param_history(
    ctx: typer.Context,
    name: str | None = typer.Argument(None, help="Optional parameter name"),
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum history records"),
) -> None:
    """Show parameter audit change history."""
    with _open_rt(ctx, ensure=False) as rt:
        if rt.params is None:
            typer.echo("Parameter database not initialized.")
            return
        entries = rt.params.history(name=name, limit=limit)

    if not entries:
        typer.echo("No parameter history records found.")
        return

    headers = ["TS_UTC", "NAME", "SCOPE", "OLD_VAL", "NEW_VAL", "BY", "STATUS", "REASON"]
    rows = [
        [e.ts_utc, e.name, e.scope, str(e.old_value), str(e.new_value), e.by, e.status, e.reason]
        for e in entries
    ]
    typer.echo(format_table(headers, rows))


@param_app.command("reset")
@handle_cli_errors
def param_reset(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Parameter name to reset"),
    reason: str = typer.Option(..., "--reason", "-r", help="Mandatory reason for reset"),
    scope: str = typer.Option("global", "--scope", "-s", help="Parameter scope"),
) -> None:
    """Reset parameter override back to default specification."""
    if not reason or not reason.strip():
        raise typer.BadParameter("Reason must not be empty", param_hint="--reason")

    with _open_rt(ctx, ensure=True) as rt:
        if rt.params is None:
            raise ParamError(
                what="Parameter registry database could not be opened",
                where=name,
                fix="Check database permissions in data/state/params.db",
            )
        by = getpass.getuser()
        res = rt.params.reset(name, reason=reason, scope=scope, by=by, channel="cli")
        typer.echo(f"OK: reset {res.name} -> {res.new_value} ({res.direction}, {res.status})")


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
            if problems:
                typer.echo(f"Base contract problems found in {kind}/{name}:")
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


@journal_app.command("verify")
@handle_cli_errors
def journal_verify(ctx: typer.Context) -> None:
    """Verify hash chain integrity of append-only journal."""
    with _open_rt(ctx, ensure=False) as rt:
        db_path = rt.state.journal_db
        if not db_path.exists():
            typer.echo("Journal database does not exist.")
            return
        res = verify_journal(db_path)

    if res.ok:
        typer.echo(f"JOURNAL OK: verified {res.checked} events")
    else:
        typer.echo(f"JOURNAL CORRUPT: {res.reason} (bad sequence {res.first_bad_seq})")
        raise typer.Exit(code=1)


@journal_app.command("tail")
@handle_cli_errors
def journal_tail(
    ctx: typer.Context,
    n: int = typer.Option(10, "-n", help="Number of recent events to display"),
) -> None:
    """Display most recent journal events."""
    with _open_rt(ctx, ensure=False) as rt:
        db_path = rt.state.journal_db
        if not db_path.exists():
            typer.echo("Journal database does not exist.")
            return
        event_list = tail(db_path, n=n)

    if not event_list:
        typer.echo("No journal events found.")
        return

    headers = ["SEQ", "TS_UTC", "KIND", "RUN_ID"]
    rows = [
        [
            str(e.get("seq", "")),
            str(e.get("ts_utc", "")),
            str(e.get("kind", "")),
            str(e.get("run_id", "")),
        ]
        for e in event_list
    ]
    typer.echo(format_table(headers, rows))


@journal_app.command("runs")
@handle_cli_errors
def journal_runs(ctx: typer.Context) -> None:
    """List execution runs recorded in the journal."""
    with _open_rt(ctx, ensure=False) as rt:
        db_path = rt.state.journal_db
        if not db_path.exists():
            typer.echo("Journal database does not exist.")
            return

        raw_runs: list[dict[str, Any]] = []
        try:
            raw_runs = runs(db_path)
        except Exception:
            raw_runs = []

    if not raw_runs:
        typer.echo("No runs found in journal.")
        return

    headers = ["RUN_ID", "MODE", "STATUS", "STARTED_UTC", "ENDED_UTC"]
    rows = [
        [
            str(r.get("run_id", "")),
            str(r.get("mode", "")),
            str(r.get("status", "")),
            str(r.get("started_utc", "")),
            str(r.get("ended_utc") or "-"),
        ]
        for r in raw_runs
    ]
    typer.echo(format_table(headers, rows))


@app.command("budget")
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


@app.command("lint")
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


@secrets_app.command("scan")
@handle_cli_errors
def secrets_scan_cmd(
    ctx: typer.Context,
    json_opt: bool = typer.Option(False, "--json", help="Output machine-readable JSON"),
) -> None:
    """Scan codebase for exposed secrets, tokens, and cloud keys."""
    from algodesk.devtools.secretscan import (
        format_secretscan_json,
        format_secretscan_text,
        run_secretscan,
    )

    state = _get_cli_state(ctx)
    is_json = json_opt or state.json
    with _open_rt(ctx, ensure=False) as rt:
        rep = run_secretscan(rt.root)

    if is_json:
        typer.echo(format_secretscan_json(rep))
    else:
        typer.echo(format_secretscan_text(rep))

    if rep.exit_code != 0:
        raise typer.Exit(code=rep.exit_code)


def main() -> None:
    """Top-level entrypoint for the algodesk CLI."""
    debug = "--debug" in sys.argv
    try:
        app()
    except typer.Exit as e:
        sys.exit(e.exit_code)
    except (typer.BadParameter, click.UsageError, click.exceptions.ClickException):
        sys.exit(2)
    except AlgoDeskError as err:
        if debug:
            traceback.print_exc()
        typer.echo(f"ERROR: {err.what}")
        typer.echo(f"WHERE: {err.where}")
        typer.echo(f"FIX: {err.fix}")
        sys.exit(1)
    except Exception as exc:
        if debug:
            traceback.print_exc()
        typer.echo(f"INTERNAL ERROR (bug): {type(exc).__name__}: {exc}")
        typer.echo("run with --debug for the traceback")
        sys.exit(70)
