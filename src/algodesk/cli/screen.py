"""Screen inspection, listing, and execution CLI commands."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import typer

from algodesk.api.errors import ConfigError
from algodesk.api.kinds import Mode, PluginKind
from algodesk.api.screens import ScreenResult
from algodesk.cli.common import _open_rt, format_table, handle_cli_errors
from algodesk.data.groups import load_groups
from algodesk.ops.context import build_context
from algodesk.ops.screens import build_screen_runner
from algodesk.results.screen_writer import write_screen_results
from algodesk.screens.models import TomlScreenModel, load_toml_screen

screen_app = typer.Typer(
    name="screen",
    help="Inspect, list, and run market screens over universe data.",
)


def _collect_screens(rt: Any) -> dict[str, dict[str, Any]]:
    screens: dict[str, dict[str, Any]] = {}

    # 1. Scan config/screens/*.toml
    cfg_screens_dir = rt.root / "config" / "screens"
    if cfg_screens_dir.is_dir():
        for toml_path in sorted(cfg_screens_dir.glob("*.toml")):
            try:
                model = load_toml_screen(toml_path)
                file_hash = hashlib.sha256(toml_path.read_bytes()).hexdigest()
                screens[model.name] = {
                    "name": model.name,
                    "kind": "toml",
                    "timeframe": model.timeframe,
                    "description": model.description,
                    "path": toml_path,
                    "model": model,
                    "file_hash": file_hash,
                }
            except Exception as exc:
                screens[toml_path.stem] = {
                    "name": toml_path.stem,
                    "kind": "toml (invalid)",
                    "timeframe": "1d",
                    "description": f"Error: {exc}",
                    "path": toml_path,
                    "model": None,
                    "file_hash": None,
                }

    # 2. Scan screen plugins
    try:
        p_ctx = build_context(Mode.RESEARCH, run_id="screen_list")
        for p_info in rt.plugins.list(PluginKind.SCREEN):
            try:
                plugin = rt.plugins.create(PluginKind.SCREEN, p_info.name, config={}, ctx=p_ctx)
                spec = getattr(plugin, "spec", None)
                tf = getattr(spec, "timeframe", "1d") if spec else "1d"
                desc = getattr(spec, "description", "") if spec else ""
                code_hash = getattr(p_info, "code_hash", "")
                screens[p_info.name] = {
                    "name": p_info.name,
                    "kind": "plugin",
                    "timeframe": tf,
                    "description": desc,
                    "path": p_info.directory,
                    "plugin": plugin,
                    "spec": spec,
                    "file_hash": code_hash,
                }
            except Exception as exc:
                screens[p_info.name] = {
                    "name": p_info.name,
                    "kind": "plugin (error)",
                    "timeframe": "1d",
                    "description": f"Load error: {exc}",
                    "path": p_info.directory,
                    "plugin": None,
                    "spec": None,
                    "file_hash": None,
                }
    except Exception:
        pass

    return screens


def _format_operand_display(op: Any) -> str:
    if not hasattr(op, "is_field"):
        return str(op)
    if op.is_field:
        return f"{op.field}" if op.bars_ago == 0 else f"{op.field}[{op.bars_ago} bars ago]"
    if op.is_indicator:
        param_strs = [f"{k}={v}" for k, v in sorted(op.params.items())]
        if op.output and op.output != "value":
            param_strs.append(f"output={op.output}")
        if op.bars_ago > 0:
            param_strs.append(f"bars_ago={op.bars_ago}")
        inner = ", ".join(param_strs)
        return f"{op.ind}({inner})" if inner else f"{op.ind}()"
    if op.is_const:
        return f"{op.const}"
    if op.is_range:
        return f"[{op.low} to {op.high}]"
    return str(op)


@screen_app.command("list")
@handle_cli_errors
def screen_list(ctx: typer.Context) -> None:
    """List available screens from config/screens/ and screen plugins."""
    with _open_rt(ctx, ensure=False) as rt:
        screens = _collect_screens(rt)

    if not screens:
        typer.echo(
            "No screens found. Place TOML screens in config/screens/ or install screen plugins."
        )
        return

    headers = ["NAME", "KIND", "TIMEFRAME", "DESCRIPTION"]
    rows: list[list[str]] = []
    for name in sorted(screens.keys()):
        info = screens[name]
        rows.append([name, info["kind"], info["timeframe"], info["description"]])

    typer.echo(format_table(headers, rows))


@screen_app.command("show")
@handle_cli_errors
def screen_show(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Name of screen (TOML screen name or plugin name)"),
) -> None:
    """Show details and rules for a screen."""
    with _open_rt(ctx, ensure=False) as rt:
        screens = _collect_screens(rt)

    if name not in screens:
        available = ", ".join(sorted(screens.keys())) if screens else "none"
        raise ConfigError(
            what=f"Screen '{name}' not found. Available screens: {available}",
            where=name,
            fix=f"Specify one of the available screens: {available}",
        )

    info = screens[name]
    typer.echo(f"Name:        {name}")
    typer.echo(f"Kind:        {info['kind']}")
    typer.echo(f"Timeframe:   {info['timeframe']}")
    typer.echo(f"Description: {info['description']}")

    if info.get("model") is not None:
        model: TomlScreenModel = info["model"]
        typer.echo(f"Adjust:      {model.adjust}")
        if model.universe:
            typer.echo(f"Universe:    {model.universe}")
        if model.require:
            typer.echo("\nRequirements (all must hold):")
            for i, req in enumerate(model.require, 1):
                left_str = _format_operand_display(req.left)
                right_str = _format_operand_display(req.right)
                within_str = (
                    f" within {req.within} bars"
                    if req.op in ("crosses_above", "crosses_below")
                    else ""
                )
                typer.echo(f"  {i}. {left_str} {req.op} {right_str}{within_str}")
        if model.group:
            typer.echo("\nGroups:")
            for i, grp in enumerate(model.group, 1):
                n_str = f" (n={grp.n})" if grp.n else ""
                typer.echo(f"  Group {i} [mode: {grp.mode}{n_str}]:")
                for j, req in enumerate(grp.require, 1):
                    left_str = _format_operand_display(req.left)
                    right_str = _format_operand_display(req.right)
                    typer.echo(f"    {j}. {left_str} {req.op} {right_str}")
        if model.rank:
            by_str = _format_operand_display(model.rank.by)
            min_str = f" >= {model.rank.min}" if model.rank.min is not None else ""
            typer.echo(f"\nRanking: by {by_str} (method: {model.rank.method}{min_str})")


@screen_app.command("run")
@handle_cli_errors
def screen_run(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Screen name"),
    as_of: str | None = typer.Option(
        None, "--as-of", help="As-of date YYYY-MM-DD (defaults to last completed session)"
    ),
    group: str | None = typer.Option(
        None, "--group", "-g", help="Instrument group name (e.g. demo5)"
    ),
    symbols: str | None = typer.Option(
        None, "--symbols", "-s", help="Comma-separated symbols (e.g. RELIANCE,TCS)"
    ),
    limit: int | None = typer.Option(
        None, "--limit", "-l", help="Limit universe to first N symbols"
    ),
    adjust: str | None = typer.Option(
        None, "--adjust", help="Adjustment mode: total, splits, none"
    ),
    accept_unverified: bool = typer.Option(
        False, "--accept-unverified", help="Accept unverified corporate actions"
    ),
    timing: bool = typer.Option(
        False, "--timing", help="Print execution timing and throughput breakdown"
    ),
    json_output: bool = typer.Option(False, "--json", help="Output results in JSON format"),
) -> None:
    """Run a market screen over universe instruments and record results bundle."""
    with _open_rt(ctx, ensure=False) as rt:
        screens = _collect_screens(rt)
        if name not in screens:
            available = ", ".join(sorted(screens.keys())) if screens else "none"
            raise ConfigError(
                what=f"Screen '{name}' not found. Available screens: {available}",
                where=name,
                fix=f"Specify one of the available screens: {available}",
            )

        info = screens[name]
        screen_obj = info.get("model") or info.get("plugin")
        if screen_obj is None:
            raise ConfigError(
                what=f"Screen '{name}' could not be loaded: {info.get('description')}",
                where=name,
                fix="Check screen file or plugin code",
            )

        # Resolve universe symbols
        sym_list: list[str] = []
        groups_map = load_groups(rt.root / "config" / "groups")
        if group:
            if group not in groups_map:
                avail_groups = ", ".join(sorted(groups_map.keys())) if groups_map else "none"
                raise ConfigError(
                    what=f"Group '{group}' not found. Available groups: {avail_groups}",
                    where=group,
                    fix=f"Specify one of the available groups: {avail_groups}",
                )
            sym_list = list(groups_map[group])
        elif symbols:
            sym_list = [s.strip() for s in symbols.split(",") if s.strip()]
        elif getattr(screen_obj, "universe", None):
            u_name = screen_obj.universe
            if u_name not in groups_map:
                avail_groups = ", ".join(sorted(groups_map.keys())) if groups_map else "none"
                raise ConfigError(
                    what=(
                        f"Screen universe group '{u_name}' not found. "
                        f"Available groups: {avail_groups}"
                    ),
                    where=u_name,
                    fix=f"Specify one of the available groups: {avail_groups}",
                )
            sym_list = list(groups_map[u_name])
        else:
            avail_groups = ", ".join(sorted(groups_map.keys())) if groups_map else "none"
            raise ConfigError(
                what="No universe specified. Provide --group <name> or --symbols <a,b>",
                where="screen_run",
                fix=f"Specify --group <group> (available: {avail_groups}) or --symbols <a,b>",
            )

        runner = build_screen_runner(rt)
        effective_cfg_text = (
            info["path"].read_text(encoding="utf-8")
            if isinstance(info.get("path"), Path) and info["path"].is_file()
            else f"# Screen: {name}\n"
        )

        result: ScreenResult = runner.run(
            screen=screen_obj,
            symbols=sym_list,
            as_of=as_of,
            limit=limit,
            adjust=adjust,
            accept_unverified=accept_unverified,
            screen_file_hash=info.get("file_hash"),
        )

        # Write results bundle
        import time as _time

        t_write0 = _time.perf_counter()
        results_dir = rt.root / "results"
        run_dir = write_screen_results(
            results_dir, result, effective_config_text=effective_cfg_text
        )
        t_writing = _time.perf_counter() - t_write0

    # Output presentation
    passed_rows = [r for r in result.rows if r.passed]
    total_symbols = len(result.rows) + len(result.skipped)

    t_reads = float(result.timing.get("reads", 0.0))
    t_indicators = float(result.timing.get("indicators", 0.0))
    t_rules = float(result.timing.get("rules", 0.0))
    t_total = t_reads + t_indicators + t_rules + t_writing
    symbols_per_sec = (total_symbols / t_total) if t_total > 0 else 0.0

    if json_output:
        out_dict = {
            "run_id": result.run_id,
            "name": result.name,
            "as_of": str(result.as_of),
            "verdict": result.verdict,
            "fail_reason": result.fail_reason,
            "result_hash": result.result_hash,
            "universe_hash": result.universe_hash,
            "stats": {
                "universe_size": total_symbols,
                "passed": len(passed_rows),
                "skipped": len(result.skipped),
            },
            "passed_symbols": [r.symbol for r in passed_rows],
            "results_dir": str(run_dir),
            "timing": {
                "reads": t_reads,
                "indicators": t_indicators,
                "rules": t_rules,
                "writing": t_writing,
                "total": t_total,
                "symbols_per_sec": symbols_per_sec,
            },
        }
        typer.echo(json.dumps(out_dict, indent=2))
    else:
        typer.echo(f"\nScreen Run: {result.name}")
        typer.echo(f"As-of:       {result.as_of}")
        typer.echo(
            f"Verdict:     {result.verdict}"
            + (f" ({result.fail_reason})" if result.fail_reason else "")
        )
        typer.echo(f"Result Hash: {result.result_hash[:16]}...")
        typer.echo(
            f"Universe:    {total_symbols} | Passed: {len(passed_rows)} | "
            f"Skipped: {len(result.skipped)}"
        )
        typer.echo(f"Artifacts:   {run_dir}\n")

        if timing:
            typer.echo(
                f"Timing: reads={t_reads:.3f}s, indicators={t_indicators:.3f}s, "
                f"rules={t_rules:.3f}s, writing={t_writing:.3f}s | "
                f"Throughput: {symbols_per_sec:.1f} symbols/s\n"
            )

        if passed_rows:
            headers = ["SYMBOL", "PASSED", "SCORE"]
            rows: list[list[str]] = []
            for r in passed_rows:
                rows.append([r.symbol, "YES", f"{r.score:.2f}"])
            typer.echo(format_table(headers, rows))
        else:
            typer.echo("No symbols passed screen conditions.")

    if result.verdict == "FAIL":
        raise typer.Exit(code=1)
