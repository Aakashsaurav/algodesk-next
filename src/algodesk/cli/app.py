"""AlgoDesk command-line interface implementation with Typer."""

from __future__ import annotations

import functools
import getpass
import json
import subprocess
import sys
import traceback
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import click
import typer

from algodesk.api import InstrumentId, SeriesKey, resolve_secret_ref
from algodesk.api.conformance import kind_problems
from algodesk.api.errors import AlgoDeskError, ConfigError, ParamError
from algodesk.api.kinds import Mode
from algodesk.config.snapshot import render_effective
from algodesk.data.catalog import (
    coverage,
    execute_delete,
    list_trash,
    plan_delete,
    purge_trash,
)
from algodesk.data.groups import get_group, list_group_details
from algodesk.data.manifest import DataManifest
from algodesk.data.recorder import MarketRecorder
from algodesk.data.synth import synth_ticks
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
calendar_app = typer.Typer(help="Inspect trading calendars, sessions, and market holidays.")
fees_app = typer.Typer(help="Calculate trade charges and inspect fee schedule vectors.")
data_app = typer.Typer(help="Fetch, inspect, delete, and manage market data.")
data_trash_app = typer.Typer(help="Manage data safety trash.")
groups_app = typer.Typer(help="Inspect and list configured instrument groups.")
login_app = typer.Typer(help="Manage broker authentication and credentials.")
feed_app = typer.Typer(help="Probe and inspect live market data feeds.")

app.add_typer(param_app, name="param")
app.add_typer(plugins_app, name="plugins")
app.add_typer(journal_app, name="journal")
app.add_typer(config_app, name="config")
app.add_typer(new_app, name="new")
app.add_typer(docs_app, name="docs")
app.add_typer(secrets_app, name="secrets")
app.add_typer(calendar_app, name="calendar")
app.add_typer(fees_app, name="fees")
app.add_typer(data_app, name="data")
data_app.add_typer(data_trash_app, name="trash")
app.add_typer(groups_app, name="groups")
app.add_typer(login_app, name="login")
app.add_typer(feed_app, name="feed")


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
    run: str | None = typer.Option(None, "--run", help="Run configuration name"),
    json_opt: bool = typer.Option(False, "--json", help="Output machine-readable JSON"),
) -> None:
    """Run platform and component diagnostic health checks."""
    state = _get_cli_state(ctx)
    is_json = json_opt or state.json
    run_name = run or state.run
    with _open_rt(ctx, ensure=False) as rt:
        checks = build_core_checks(rt)
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


def _resolve_secrets_provider(
    rt: Any,
    provider_name: str | None = None,
    run: str | None = None,
) -> Any:
    p_name = provider_name
    if not p_name and run:
        from algodesk.config.loader import load_toml

        run_path = rt.root / "config" / "runs" / f"{run}.toml"
        if run_path.is_file():
            try:
                rc = load_toml(run_path)
                sec_dict = rc.get("secrets", {})
                if isinstance(sec_dict, dict) and "plugin" in sec_dict:
                    p_name = str(sec_dict["plugin"])
            except Exception:
                pass

    if not p_name:
        if rt.effective.run is not None and getattr(rt.effective.run, "secrets", None) is not None:
            p_name = rt.effective.run.secrets.plugin
        elif sys.platform == "darwin":
            p_name = "keychain"
        else:
            p_name = "env"

    p_ctx = build_context(Mode.RESEARCH, run_id="secrets_cli")
    return rt.plugins.create("secrets", p_name, config={}, ctx=p_ctx)


@secrets_app.command("set")
@handle_cli_errors
def secrets_set_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Secret name"),
    use_stdin: bool = typer.Option(False, "--stdin", help="Read secret value from stdin"),
    run: str | None = typer.Option(None, "--run", help="Run configuration name"),
    provider: str | None = typer.Option(None, "--provider", help="Secrets provider name"),
) -> None:
    """Store a secret securely in the configured secrets provider."""
    import getpass

    if use_stdin:
        val = sys.stdin.read().strip()
    else:
        val = getpass.getpass(f"Enter secret value for '{name}': ").strip()

    if not val:
        raise typer.BadParameter("Secret value cannot be empty")

    with _open_rt(ctx, ensure=False) as rt:
        prov = _resolve_secrets_provider(rt, provider_name=provider, run=run)
        prov.set(name, val)
    typer.echo(f"Secret '{name}' stored successfully.")


@secrets_app.command("list")
@handle_cli_errors
def secrets_list_cmd(
    ctx: typer.Context,
    run: str | None = typer.Option(None, "--run", help="Run configuration name"),
    provider: str | None = typer.Option(None, "--provider", help="Secrets provider name"),
) -> None:
    """List stored secret names (names only, values never displayed)."""
    with _open_rt(ctx, ensure=False) as rt:
        prov = _resolve_secrets_provider(rt, provider_name=provider, run=run)
        names = prov.names()
    for n in sorted(names):
        typer.echo(n)


@secrets_app.command("delete")
@handle_cli_errors
def secrets_delete_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Secret name to delete"),
    run: str | None = typer.Option(None, "--run", help="Run configuration name"),
    provider: str | None = typer.Option(None, "--provider", help="Secrets provider name"),
) -> None:
    """Delete a secret from the configured secrets provider."""
    with _open_rt(ctx, ensure=False) as rt:
        prov = _resolve_secrets_provider(rt, provider_name=provider, run=run)
        prov.delete(name)
    typer.echo(f"Secret '{name}' deleted.")


@calendar_app.command("show")
@handle_cli_errors
def calendar_show(
    ctx: typer.Context,
    exchange: str = typer.Option(
        ..., "--exchange", "-e", help="Exchange code (e.g. nse, bse, mcx)"
    ),
    segment: str = typer.Option(
        "", "--segment", "-s", help="Segment code (e.g. eq, com, fno, cds)"
    ),
    date_str: str = typer.Option(..., "--date", "-d", help="Date in YYYY-MM-DD format"),
) -> None:
    """Show trading sessions for an exchange and date."""
    d = date.fromisoformat(date_str)
    with _open_rt(ctx, ensure=False) as rt:
        p_ctx = build_context(Mode.RESEARCH, run_id=f"cal_show_{exchange}")
        cal: Any = rt.plugins.create(
            "calendar", "exchange", config={"exchange": exchange}, ctx=p_ctx
        )
        seg = segment or ("com" if exchange.lower() == "mcx" else "eq")
        sessions = cal.sessions(d, segment=seg)
        if not sessions:
            typer.echo(f"Market closed on {d} for {exchange.upper()}:{seg}")
            return
        headers = ["SESSION", "OPEN (UTC)", "CLOSE (UTC)", "OPEN (IST)", "CLOSE (IST)"]
        ist_tz = ZoneInfo("Asia/Kolkata")
        rows: list[list[str]] = []
        for s in sessions:
            open_ist = s.start.astimezone(ist_tz).strftime("%H:%M")
            close_ist = s.end.astimezone(ist_tz).strftime("%H:%M")
            open_utc = s.start.strftime("%H:%M")
            close_utc = s.end.strftime("%H:%M")
            rows.append([s.name, open_utc, close_utc, open_ist, close_ist])
        typer.echo(format_table(headers, rows))


@calendar_app.command("holidays")
@handle_cli_errors
def calendar_holidays(
    ctx: typer.Context,
    exchange: str = typer.Option(
        ..., "--exchange", "-e", help="Exchange code (e.g. nse, bse, mcx)"
    ),
    year: int | None = typer.Option(None, "--year", "-y", help="Filter holidays by year"),
) -> None:
    """List declared market holidays for an exchange."""
    with _open_rt(ctx, ensure=False) as rt:
        p_ctx = build_context(Mode.RESEARCH, run_id=f"cal_holidays_{exchange}")
        cal: Any = rt.plugins.create(
            "calendar", "exchange", config={"exchange": exchange}, ctx=p_ctx
        )
        h_list = cal.holidays(year=year)
        if not h_list:
            typer.echo(
                f"No holidays found for {exchange.upper()}" + (f" in {year}" if year else "")
            )
            return
        headers = ["DATE", "NAME", "CLOSED"]
        rows: list[list[str]] = []
        for h in h_list:
            closed_str = ", ".join(h.get("closed", ["all"]))
            rows.append([str(h["date"]), h["name"], closed_str])
        typer.echo(format_table(headers, rows))
        typer.echo(f"\n{len(h_list)} holiday(s) found for {exchange.upper()}")


@calendar_app.command("check")
@handle_cli_errors
def calendar_check(
    ctx: typer.Context,
    exchange: str = typer.Option(
        ..., "--exchange", "-e", help="Exchange code (e.g. nse, bse, mcx)"
    ),
    days: int = typer.Option(
        ..., "--days", "-d", help="Days forward to check coverage and sessions"
    ),
) -> None:
    """Verify calendar coverage and pending special sessions for the next N days."""
    with _open_rt(ctx, ensure=False) as rt:
        p_ctx = build_context(Mode.RESEARCH, run_id=f"cal_check_{exchange}")
        cal: Any = rt.plugins.create(
            "calendar", "exchange", config={"exchange": exchange}, ctx=p_ctx
        )
        cov_start, cov_end = cal.coverage()
        today = date.today()
        target = today + timedelta(days=days)

        problems: list[str] = []
        if today < cov_start or target > cov_end:
            problems.append(
                f"Coverage [{cov_start}, {cov_end}] does not span required [{today}, {target}]"
            )

        for sp in cal.pending_special_sessions():
            sp_date = sp.get("date")
            if sp_date and today <= sp_date <= target:
                problems.append(
                    f"Pending special session without timings on {sp_date}: {sp.get('name')}"
                )

        if problems:
            typer.echo(f"Calendar check failed for {exchange.upper()}:")
            for p in problems:
                typer.echo(f"  - {p}")
            raise typer.Exit(code=1)

        typer.echo(
            f"OK: Calendar for {exchange.upper()} covers next {days} days "
            "with no pending special sessions."
        )


@fees_app.command("calc")
@handle_cli_errors
def fees_calc_cmd(
    ctx: typer.Context,
    exchange: str = typer.Option("nse", "--exchange", "-e", help="Exchange code (e.g. nse)"),
    segment: str = typer.Option("eq", "--segment", "-s", help="Segment code (e.g. eq, fno)"),
    product: str = typer.Option(
        "MIS", "--product", "-p", help="Product type (e.g. CNC, MIS, NRML)"
    ),
    side: str = typer.Option("buy", "--side", help="Order side (buy, sell)"),
    qty: int = typer.Option(..., "--qty", "-q", help="Execution quantity"),
    price: float = typer.Option(..., "--price", help="Execution unit price"),
    date_str: str = typer.Option(..., "--date", "-d", help="Date in YYYY-MM-DD format"),
    symbol: str | None = typer.Option(None, "--symbol", help="Optional scrip symbol"),
) -> None:
    """Calculate itemised trade charges and total for a given trade."""
    from decimal import Decimal

    from algodesk.fees.engine import FeeEngine

    d = date.fromisoformat(date_str)
    price_dec = Decimal(str(price))
    turnover = Decimal(qty) * price_dec

    with _open_rt(ctx, ensure=False) as rt:
        p_ctx = build_context(Mode.RESEARCH, run_id=f"fees_calc_{exchange}")
        plugin_name = f"zerodha_{exchange.lower()}"
        fees_plugin: Any = rt.plugins.create("fees", plugin_name, config={}, ctx=p_ctx)
        sch = fees_plugin.schedule()
        charges = FeeEngine.calculate_trade_charges(
            turnover=turnover,
            side=side,
            segment=segment,
            product=product,
            trade_date=d,
            schedule=sch,
        )

    headers = ["COMPONENT", "AMOUNT (INR)"]
    rows = [[k, f"{v:.2f}"] for k, v in charges.items.items()]
    rows.append(["TOTAL", f"{charges.total:.2f}"])
    typer.echo(
        f"Itemised charges for {exchange.upper()}:{segment.upper()}:{product.upper()} "
        f"{side.upper()} {qty} @ {price:.2f} (Turnover: {turnover:.2f}) on {d}:"
    )
    typer.echo(format_table(headers, rows))


@fees_app.command("vectors")
@handle_cli_errors
def fees_vectors_cmd(ctx: typer.Context) -> None:
    """Display the verified trade fee vectors table."""
    from decimal import Decimal

    from algodesk.fees.engine import FeeEngine

    vectors: list[tuple[str, str, str, str, int, Decimal, bool]] = [
        ("1", "eq", "CNC", "buy", 100, Decimal("500.00"), True),
        ("2", "eq", "CNC", "sell", 100, Decimal("500.00"), True),
        ("3", "eq", "CNC", "sell", 50, Decimal("500.00"), True),
        ("4", "eq", "MIS", "buy", 10, Decimal("2500.00"), True),
        ("5", "eq", "MIS", "sell", 10, Decimal("2500.00"), True),
        ("6", "eq", "MIS", "buy", 1000, Decimal("500.00"), True),
        ("7", "fno", "NRML", "buy", 250, Decimal("2000.00"), True),
        ("8", "fno", "NRML", "sell", 250, Decimal("2000.00"), True),
    ]

    with _open_rt(ctx, ensure=False) as rt:
        p_ctx = build_context(Mode.RESEARCH, run_id="fees_vectors")
        fees_plugin: Any = rt.plugins.create("fees", "zerodha_nse", config={}, ctx=p_ctx)
        sch = fees_plugin.schedule()

        headers = ["#", "SEGMENT", "PRODUCT", "SIDE", "QTY", "PRICE", "TURNOVER", "TOTAL", "STATUS"]
        rows: list[list[str]] = []
        d = date(2026, 10, 26)

        for num, seg, prod, s, q, p, is_first in vectors:
            turnover = Decimal(q) * p
            charges = FeeEngine.calculate_trade_charges(
                turnover=turnover,
                side=s,
                segment=seg,
                product=prod,
                trade_date=d,
                schedule=sch,
                is_first_scrip_day=is_first,
            )
            rows.append(
                [
                    num,
                    seg.upper(),
                    prod.upper(),
                    s.upper(),
                    str(q),
                    f"{p:.2f}",
                    f"{turnover:.2f}",
                    f"{charges.total:.2f}",
                    "computed",
                ]
            )

    typer.echo("Computed fee vectors (pending your hand check against Zerodha's calculator):")
    typer.echo(format_table(headers, rows))


def _parse_data_cli_dt(val: str, is_end: bool) -> datetime:
    val = val.strip()
    ist = ZoneInfo("Asia/Kolkata")
    try:
        dt = datetime.fromisoformat(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        d = date.fromisoformat(val)
        if is_end:
            return datetime.combine(d, time(23, 59, 59), tzinfo=ist).astimezone(UTC)
        return datetime.combine(d, time.min, tzinfo=ist).astimezone(UTC)


@data_app.command("get")
@handle_cli_errors
def data_get(
    ctx: typer.Context,
    symbol: str = typer.Argument(..., help="Symbol (e.g. RELIANCE, INFY)"),
    timeframe: str = typer.Argument(..., help="Timeframe (e.g. 1m, 5m, 15m, 30m, 1h, 1d)"),
    start: str = typer.Argument(..., help="Start date/time (e.g. YYYY-MM-DD or ISO8601)"),
    end: str = typer.Argument(..., help="End date/time (e.g. YYYY-MM-DD or ISO8601)"),
    adjusted: bool = typer.Option(False, "--adjusted", help="Use adjusted prices"),
    source: str = typer.Option("yfinance", "--source", help="Data source broker plugin"),
) -> None:
    """Fetch and display historical market data bars in a table."""
    ist = ZoneInfo("Asia/Kolkata")
    start_dt = _parse_data_cli_dt(start, is_end=False)
    end_dt = _parse_data_cli_dt(end, is_end=True)

    inst_id = InstrumentId(symbol if ":" in symbol else f"NSE:EQ:{symbol}")
    key = SeriesKey(
        instrument=inst_id,
        kind="bars",
        timeframe=timeframe,
        adjustment="adjusted" if adjusted else "raw",
        source=source,
    )

    with _open_rt(ctx, ensure=False) as rt:
        p_ctx = build_context(Mode.RESEARCH, run_id=f"data_get_{source}")
        broker: Any = rt.plugins.create("broker", source, config={}, ctx=p_ctx)
        bars = list(broker.history(key, start_dt, end_dt))

    if not bars:
        typer.echo(f"No bars returned for {symbol} ({timeframe}) between {start} and {end}.")
        return

    is_daily = timeframe.lower() == "1d"
    headers = ["DATE" if is_daily else "DATE/TIME", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]
    rows: list[list[str]] = []
    for b in bars:
        ts_ist = b.start_ts.astimezone(ist)
        d_str = ts_ist.strftime("%Y-%m-%d") if is_daily else ts_ist.strftime("%Y-%m-%d %H:%M")
        rows.append(
            [
                d_str,
                f"{b.open:.2f}",
                f"{b.high:.2f}",
                f"{b.low:.2f}",
                f"{b.close:.2f}",
                str(b.volume),
            ]
        )
    typer.echo(format_table(headers, rows))


def _log_data_deleted(
    rt: Runtime,
    reason: str,
    selectors: Sequence[str],
    instruments: Sequence[str],
    files_count: int,
    total_bytes: int,
    purge: bool,
) -> None:
    try:
        from algodesk.journal.writer import Journal, JournalSettings

        flush_interval = int(rt.params.get("journal.flush_interval_ms")) if rt.params else 50
        max_buffer = int(rt.params.get("journal.max_buffer_events")) if rt.params else 1000
        bp_timeout = int(rt.params.get("journal.backpressure_timeout_ms")) if rt.params else 5000
        settings = JournalSettings(
            flush_interval_ms=flush_interval,
            max_buffer_events=max_buffer,
            backpressure_timeout_ms=bp_timeout,
        )
        with Journal(rt.state.journal_db, settings=settings) as journal:
            journal.start_run(
                mode="research",
                code_version="core",
                config_hash="maint",
                registry_hash="data_delete",
            )
            journal.append(
                "data.deleted",
                {
                    "reason": reason,
                    "selectors": list(selectors),
                    "instruments": list(instruments),
                    "files_count": files_count,
                    "bytes": total_bytes,
                    "purge": purge,
                },
                durable=True,
            )
            journal.end_run("ok")
    except Exception:
        pass


@data_app.command("list")
@handle_cli_errors
def data_list(
    ctx: typer.Context,
    group_by: str = typer.Option(
        "none", "--group-by", help="Grouping: exchange|segment|underlying|group|none"
    ),
    group: str | None = typer.Option(None, "--group", help="Filter by instrument group"),
    kind: str | None = typer.Option(None, "--kind", help="Filter by data kind (ticks, bars)"),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """List recorded market data coverage across partitions."""
    with _open_rt(ctx, ensure=False) as rt:
        manifest_db = rt.root / "data" / "state" / "data_manifest.db"
        manifest = DataManifest(manifest_db)
        groups_dir = rt.root / "config" / "groups"
        rows = coverage(
            manifest,
            group_by=group_by if group_by != "none" else None,
            filters={"kind": kind} if kind else None,
            groups_dir=groups_dir,
        )

        if group:
            members = get_group(group, groups_dir) or []
            if group_by == "none":
                rows = [r for r in rows if r.target in members]

        if json_out:
            typer.echo(json.dumps([asdict(r) for r in rows], indent=2))
            return

        target_col = group_by.upper() if group_by != "none" else "INSTRUMENT"
        headers = [
            target_col,
            "KIND",
            "TIMEFRAME",
            "FIRST DATE",
            "LAST DATE",
            "DAYS",
            "ROWS",
            "BYTES",
            "SOURCE",
        ]
        table_rows = [
            [
                r.target,
                r.kind,
                r.timeframe,
                r.first_date,
                r.last_date,
                str(r.days),
                str(r.rows),
                str(r.bytes),
                r.source,
            ]
            for r in rows
        ]
        typer.echo(format_table(headers, table_rows))


@data_app.command("delete")
@handle_cli_errors
def data_delete(
    ctx: typer.Context,
    selectors: list[str] = typer.Argument(..., help="Symbol, ID, glob, or @group selector(s)"),
    from_date: str | None = typer.Option(None, "--from", help="Start date (YYYY-MM-DD)"),
    to_date: str | None = typer.Option(None, "--to", help="End date (YYYY-MM-DD)"),
    kind: str | None = typer.Option(None, "--kind", help="Data kind (ticks, bars)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Display deletion plan without deleting"),
    purge: bool = typer.Option(False, "--purge", help="Permanently delete files bypassing trash"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    reason: str = typer.Option(..., "--reason", help="Mandatory reason for audit log"),
) -> None:
    """Delete recorded market data with safe trash and transactional manifest updates."""
    with _open_rt(ctx, ensure=False) as rt:
        manifest_db = rt.root / "data" / "state" / "data_manifest.db"
        manifest = DataManifest(manifest_db)
        groups_dir = rt.root / "config" / "groups"
        data_dir = rt.root / "data"

        plan = plan_delete(
            selectors,
            manifest,
            from_date=from_date,
            to_date=to_date,
            kind=kind,
            groups_dir=groups_dir,
        )

        headers = ["INSTRUMENT", "DATE", "KIND", "ROWS", "BYTES", "PATH"]
        table_rows = [
            [f.instrument, f.date, f.kind, str(f.rows), str(f.bytes), f.path] for f in plan.files
        ]
        if table_rows:
            typer.echo(format_table(headers, table_rows))
        typer.echo(
            f"Plan: {plan.total_files} file(s) across {len(plan.instruments)} instrument(s) "
            f"({plan.total_bytes} bytes, {plan.total_rows} rows)"
        )

        if dry_run:
            return

        if plan.total_files == 0:
            typer.echo("No files found matching selector(s).")
            return

        if not yes:
            expected = f"DELETE {plan.total_files} files"
            resp = typer.prompt(f"Type '{expected}' to confirm")
            if resp.strip() != expected:
                typer.echo("Deletion aborted.")
                raise typer.Exit(code=1)

        raw_trash_days = rt.params.get("data.trash_days") if rt.params else None
        trash_days = int(raw_trash_days) if raw_trash_days is not None else None
        lock_path = rt.state.journal_db.with_name(f"{rt.state.journal_db.name}.lock")

        count = execute_delete(
            plan,
            manifest=manifest,
            data_dir=data_dir,
            purge=purge,
            trash_days=trash_days,
            reason=reason,
            lock_path=lock_path,
        )
        _log_data_deleted(
            rt,
            reason=reason,
            selectors=selectors,
            instruments=plan.instruments,
            files_count=count,
            total_bytes=plan.total_bytes,
            purge=purge,
        )
        action_str = "purged permanently" if purge else "moved to safety trash"
        typer.echo(f"Successfully deleted {count} file(s) ({action_str}).")


@data_app.command("compact")
@handle_cli_errors
def data_compact(
    ctx: typer.Context,
    date_val: str | None = typer.Option(None, "--date", help="Date to compact (YYYY-MM-DD)"),
) -> None:
    """Compact raw partition parts into day.parquet."""
    with _open_rt(ctx, ensure=False) as rt:
        manifest_db = rt.root / "data" / "state" / "data_manifest.db"
        manifest = DataManifest(manifest_db)
        recorder = MarketRecorder(data_dir=rt.root / "data", manifest=manifest)

        dates_to_compact: list[str] = (
            [date_val] if date_val else sorted({f.date for f in manifest.get_files(state="part")})
        )
        total_compacted = 0
        for d in dates_to_compact:
            compacted = recorder.compact(d)
            total_compacted += len(compacted)
        typer.echo(f"Compacted {total_compacted} partition(s).")


@data_app.command("synth")
@handle_cli_errors
def data_synth(
    ctx: typer.Context,
    symbols: str = typer.Option(..., "--symbols", help="Comma-separated instrument symbols"),
    days: int = typer.Option(1, "--days", help="Number of trading days to generate"),
    rows: int = typer.Option(1000, "--rows", help="Rows per day per symbol"),
    seed: int = typer.Option(1, "--seed", help="Random seed for deterministic generation"),
) -> None:
    """Generate deterministic synthetic market data ticks."""
    sym_list = [s.strip() for s in symbols.split(",") if s.strip()]
    with _open_rt(ctx, ensure=False) as rt:
        manifest_db = rt.root / "data" / "state" / "data_manifest.db"
        manifest = DataManifest(manifest_db)
        total_rows = synth_ticks(
            symbols=sym_list,
            days=days,
            rows_per_day=rows,
            seed=seed,
            data_dir=rt.root / "data",
            manifest=manifest,
        )
        typer.echo(
            f"Generated {total_rows} synthetic ticks for {len(sym_list)} "
            f"symbol(s) across {days} day(s)."
        )


@data_trash_app.command("list")
@handle_cli_errors
def trash_list_cmd(
    ctx: typer.Context,
    json_out: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """List directories and files in safety trash."""
    with _open_rt(ctx, ensure=False) as rt:
        items = list_trash(rt.root / "data")
        if json_out:
            typer.echo(json.dumps([asdict(i) for i in items], indent=2))
            return
        headers = ["DIRECTORY", "DELETED AT", "FILES", "BYTES", "REASON"]
        rows = [
            [i.name, i.deleted_at, str(i.files_count), str(i.bytes_count), i.reason] for i in items
        ]
        typer.echo(format_table(headers, rows))


@data_trash_app.command("purge")
@handle_cli_errors
def trash_purge_cmd(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Purge all files in safety trash permanently."""
    if not yes:
        typer.confirm("Permanently purge all data in safety trash?", abort=True)
    with _open_rt(ctx, ensure=False) as rt:
        purged = purge_trash(rt.root / "data")
        typer.echo(f"Purged {purged} trash directory/directories.")


@groups_app.command("list")
@handle_cli_errors
def groups_list_cmd(
    ctx: typer.Context,
    json_out: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """List configured instrument groups."""
    with _open_rt(ctx, ensure=False) as rt:
        groups_dir = rt.root / "config" / "groups"
        details = list_group_details(groups_dir)
        if json_out:
            typer.echo(json.dumps(details, indent=2))
            return
        headers = ["NAME", "DESCRIPTION", "MEMBERS"]
        rows = []
        for d in details.values():
            m_count = len(d["members"])
            preview = ", ".join(d["members"][:3])
            suffix = "..." if m_count > 3 else ""
            rows.append([d["name"], d["description"], f"{m_count}: {preview}{suffix}"])
        typer.echo(format_table(headers, rows))


@groups_app.command("show")
@handle_cli_errors
def groups_show_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Group name"),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Show details and members of an instrument group."""
    with _open_rt(ctx, ensure=False) as rt:
        groups_dir = rt.root / "config" / "groups"
        details = list_group_details(groups_dir)
        if name not in details:
            known = list(details.keys())
            raise ConfigError(
                what=f"Instrument group '{name}' not found",
                where=f"config/groups/{name}.toml",
                fix=f"Available groups: {', '.join(known)}",
            )
        info = details[name]
        if json_out:
            typer.echo(json.dumps(info, indent=2))
            return
        typer.echo(f"Group: {info['name']}")
        if info["description"]:
            typer.echo(f"Description: {info['description']}")
        typer.echo("Members:")
        for m in info["members"]:
            typer.echo(f"  - {m}")


@login_app.command("upstox")
@handle_cli_errors
def login_upstox(
    ctx: typer.Context,
    manual: bool = typer.Option(False, "--manual", help="Force manual browser login URL flow"),
    run: str | None = typer.Option(None, "--run", help="Run configuration name"),
) -> None:
    """Authenticate with Upstox via automated loopback OAuth flow or manual URL."""
    with _open_rt(ctx, ensure=False) as rt:
        sec_prov = _resolve_secrets_provider(rt, run=run)
        p_ctx = build_context(Mode.RESEARCH, run_id="login_upstox", secrets=sec_prov)
        broker: Any = rt.plugins.create("broker", "upstox", config={}, ctx=p_ctx)
        if manual:
            req = broker.begin()
            typer.echo("Open the following URL in your browser to authorize AlgoDesk:")
            typer.echo(f"\n  {req.url}\n")
            code = typer.prompt("Enter authorization code from redirect URL")
            broker.complete(code.strip())
            typer.echo("Successfully authenticated with Upstox! Access token stored.")
        else:
            typer.echo("Opening browser for Upstox OAuth login...")
            try:
                broker.begin_auto()
                typer.echo("Successfully authenticated with Upstox! Access token stored.")
            except Exception as exc:
                typer.echo(f"Automated flow failed ({exc}). Falling back to manual flow...")
                req = broker.begin()
                typer.echo("Open the following URL in your browser to authorize AlgoDesk:")
                typer.echo(f"\n  {req.url}\n")
                code = typer.prompt("Enter authorization code from redirect URL")
                broker.complete(code.strip())
                typer.echo("Successfully authenticated with Upstox! Access token stored.")


@login_app.command("status")
@handle_cli_errors
def login_status(
    ctx: typer.Context,
    run: str | None = typer.Option(None, "--run", help="Run configuration name"),
) -> None:
    """Show authentication status and token validity for configured brokers."""
    with _open_rt(ctx, ensure=False) as rt:
        sec_prov = _resolve_secrets_provider(rt, run=run)
        p_ctx = build_context(Mode.RESEARCH, run_id="login_status", secrets=sec_prov)
        broker: Any = rt.plugins.create("broker", "upstox", config={}, ctx=p_ctx)

        headers = ["PROVIDER", "AUTH_TYPE", "CONFIGURED", "VALID", "EXPIRES"]
        rows: list[list[str]] = []

        # Analytics token check
        ana_tok = None
        try:
            ana_tok = resolve_secret_ref(broker._cfg.analytics_token, p_ctx.secrets)
        except Exception:
            pass
        if ana_tok:
            from algodesk.api import AuthSession

            sess = AuthSession(token=ana_tok)
            valid = broker.validate(sess)
            exp = broker.expires_at(sess)
            exp_str = exp.strftime("%Y-%m-%d") if exp else "never"
            rows.append(["upstox", "analytics", "yes", "valid" if valid else "invalid", exp_str])
        else:
            rows.append(["upstox", "analytics", "no", "-", "-"])

        # OAuth access token check
        oauth_tok = None
        try:
            oauth_tok = resolve_secret_ref(broker._cfg.access_token, p_ctx.secrets)
        except Exception:
            pass
        if oauth_tok:
            from algodesk.api import AuthSession

            sess = AuthSession(token=oauth_tok)
            valid = broker.validate(sess)
            exp = broker.expires_at(sess)
            exp_str = exp.strftime("%Y-%m-%d %H:%M") if exp else "unknown"
            rows.append(["upstox", "oauth", "yes", "valid" if valid else "invalid", exp_str])
        else:
            rows.append(["upstox", "oauth", "no", "-", "-"])

    typer.echo(format_table(headers, rows))


def _format_event_line(event: Any, depth: bool) -> list[str]:
    exch_ts = getattr(event, "exch_ts", None)
    recv_ts = getattr(event, "recv_ts", datetime.now(UTC))
    t_str = exch_ts.strftime("%H:%M:%S") if exch_ts else recv_ts.strftime("%H:%M:%S")
    ev_type = type(event).__name__
    inst = getattr(event, "instrument", "")
    if ev_type == "Quote":
        ltp = getattr(event, "ltp", "-")
        vol = getattr(event, "volume", "-")
        lines = [f"[{t_str}] QUOTE {inst} | LTP: {ltp} | Vol: {vol}"]
        if depth:
            bids = " | ".join(f"{b.price} x {b.qty}" for b in getattr(event, "bids", ()))
            asks = " | ".join(f"{a.price} x {a.qty}" for a in getattr(event, "asks", ()))
            lines.append(f"   Bids (L5): {bids or '-'}")
            lines.append(f"   Asks (L5): {asks or '-'}")
        return lines
    if ev_type == "Tick":
        ltp = getattr(event, "ltp", "-")
        qty = getattr(event, "ltq", "-")
        vol = getattr(event, "volume", "-")
        return [f"[{t_str}] TICK  {inst} | LTP: {ltp} | Qty: {qty} | Vol: {vol}"]
    if ev_type == "FeedStatus":
        return [f"[{t_str}] STATUS state={getattr(event, 'state', '-')}"]
    return []


def _track_latency(
    event: Any,
    latencies: dict[str, list[float]],
    gaps: dict[str, int],
    last_ts: dict[str, datetime],
) -> None:
    recv_ts = getattr(event, "recv_ts", datetime.now(UTC))
    inst_k = str(getattr(event, "instrument", ""))
    exch_ts = getattr(event, "exch_ts", None)
    if inst_k in latencies and exch_ts is not None:
        latencies[inst_k].append((recv_ts - exch_ts).total_seconds() * 1000.0)
        if inst_k in last_ts and (exch_ts - last_ts[inst_k]).total_seconds() > 5.0:
            gaps[inst_k] += 1
        last_ts[inst_k] = exch_ts


async def _run_feed_probe(
    broker: Any,
    inst_ids: list[InstrumentId],
    seconds: float,
    record: Path | None,
    live: bool = False,
    depth: bool = False,
) -> tuple[dict[str, list[float]], dict[str, int], list[list[str]], int]:
    import asyncio

    latencies: dict[str, list[float]] = {str(i): [] for i in inst_ids}
    gaps: dict[str, int] = {str(i): 0 for i in inst_ids}
    sample_rows: list[list[str]] = []
    rec_count, start_t = 0, datetime.now(UTC).timestamp()
    last_ts: dict[str, datetime] = {}
    if record is not None:
        record.mkdir(parents=True, exist_ok=True)

    stream_iter = broker.stream(inst_ids).__aiter__()
    while seconds <= 0 or (datetime.now(UTC).timestamp() - start_t) < seconds:
        t_out = (
            max(0.1, seconds - (datetime.now(UTC).timestamp() - start_t)) if seconds > 0 else 1.0
        )
        try:
            event = await asyncio.wait_for(stream_iter.__anext__(), timeout=t_out)
        except TimeoutError:
            continue
        except (StopAsyncIteration, asyncio.CancelledError):
            break

        _track_latency(event, latencies, gaps, last_ts)
        if live:
            for line in _format_event_line(event, depth):
                typer.echo(line)
        inst_k = str(getattr(event, "instrument", ""))
        if len(sample_rows) < 10 and inst_k:
            e_val = getattr(event, "exch_ts", None) or getattr(event, "recv_ts", None)
            t_str = e_val.strftime("%H:%M:%S") if isinstance(e_val, datetime) else "-"
            sample_rows.append(
                [t_str, inst_k, f"{getattr(event, 'ltp', '-')}", str(getattr(event, "volume", "-"))]
            )
        if record is not None:
            rec_count += 1
            (record / f"event_{rec_count:06d}.txt").write_text(str(event), encoding="utf-8")

    return latencies, gaps, sample_rows, rec_count


def _build_probe_summary(
    inst_ids: list[InstrumentId], lats: dict[str, list[float]], gaps: dict[str, int]
) -> list[list[str]]:
    import statistics

    rows: list[list[str]] = []
    for inst in inst_ids:
        k = str(inst)
        lat_list = lats.get(k, [])
        min_l = f"{min(lat_list):.1f}" if lat_list else "-"
        med_l = f"{statistics.median(lat_list):.1f}" if lat_list else "-"
        max_l = f"{max(lat_list):.1f}" if lat_list else "-"
        rows.append([k, str(len(lat_list)), min_l, med_l, max_l, str(gaps.get(k, 0))])
    return rows


class _FeedProbeCommand(typer.core.TyperCommand):
    """Custom command that defaults --record to data/probe when passed as a flag."""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        new_args: list[str] = []
        i = 0
        while i < len(args):
            arg = args[i]
            if arg == "--record":
                new_args.append(arg)
                if i + 1 < len(args) and not args[i + 1].startswith("-"):
                    new_args.append(args[i + 1])
                    i += 2
                    continue
                new_args.append("data/probe")
                i += 1
                continue
            new_args.append(arg)
            i += 1
        return super().parse_args(ctx, new_args)


@feed_app.command("probe", cls=_FeedProbeCommand)
@handle_cli_errors
def feed_probe_cmd(
    ctx: typer.Context,
    symbols: str = typer.Option(..., "--symbols", help="Comma-separated symbols to probe"),
    seconds: float = typer.Option(5.0, "--seconds", help="Duration in seconds (0 for continuous)"),
    record: Path | None = typer.Option(
        None, "--record", help="Directory to save raw frames (defaults to data/probe/)"
    ),
    source: str = typer.Option("upstox", "--source", help="Data source broker plugin"),
    run: str | None = typer.Option(None, "--run", help="Run configuration name"),
    live: bool = typer.Option(False, "--live", help="Stream incoming events live to terminal"),
    depth: bool = typer.Option(
        False, "--depth", help="Include 5-level market depth for quote events"
    ),
) -> None:
    """Probe live market data feed latency, tick arrival, and gaps."""
    import asyncio

    raw_syms = [s.strip() for s in symbols.split(",") if s.strip()]
    if not raw_syms:
        raise typer.BadParameter("Symbols must not be empty", param_hint="--symbols")
    inst_ids = [InstrumentId(s if ":" in s else f"NSE:EQ:{s}") for s in raw_syms]

    with _open_rt(ctx, ensure=False) as rt:
        sec_prov = _resolve_secrets_provider(rt, run=run)
        p_ctx = build_context(Mode.RESEARCH, run_id="feed_probe", secrets=sec_prov)
        broker: Any = rt.plugins.create("broker", source, config={}, ctx=p_ctx)
        try:
            lats, gaps, samples, rec_count = asyncio.run(
                _run_feed_probe(broker, inst_ids, seconds, record, live=live, depth=depth)
            )
        except KeyboardInterrupt:
            typer.echo("\nInterrupted by user.")
            return

    headers = ["INSTRUMENT", "TICKS", "MIN_LAT_MS", "MED_LAT_MS", "MAX_LAT_MS", "GAPS"]
    typer.echo(format_table(headers, _build_probe_summary(inst_ids, lats, gaps)))
    if samples:
        typer.echo("\nSample rows:")
        typer.echo(format_table(["TIME", "INSTRUMENT", "LTP", "VOLUME"], samples))
    if record is not None:
        typer.echo(f"\nRecorded {rec_count} event(s) to {record}")


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
