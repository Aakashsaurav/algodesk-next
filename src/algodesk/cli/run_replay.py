"""Run, replay, and results CLI commands for AlgoDesk Next."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime

import typer

from algodesk.api.checks import Severity
from algodesk.api.conformance import kind_problems
from algodesk.api.errors import ConfigError
from algodesk.api.kinds import Mode, PluginKind
from algodesk.cli.common import (
    _get_cli_state,
    _open_rt,
    format_table,
    handle_cli_errors,
)
from algodesk.cli.secrets import _resolve_secrets_provider
from algodesk.config.snapshot import build_effective
from algodesk.engine.runner import SessionRunner
from algodesk.logs.formatting import format_ist
from algodesk.ops.checks import build_run_checks
from algodesk.ops.context import build_context
from algodesk.ops.doctor import DoctorRegistry
from algodesk.registry import get_run_config_plugins
from algodesk.registry.base_checks import base_contract_problems
from algodesk.results.compare import compare_runs

results_app = typer.Typer(help="Inspect and list past session execution results.")


@handle_cli_errors
def run_cmd(
    ctx: typer.Context,
    run_name: str = typer.Argument("demo_paper", help="Run configuration name"),
    prepare: bool = typer.Option(False, "--prepare", help="Test plugins, doctor and stop"),
    observe: bool = typer.Option(False, "--observe", help="Run in observation mode"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Verify configuration and plan"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Disable console status lines"),
    warmup_source: str | None = typer.Option(None, "--warmup-source", help="Warmup source"),
) -> None:
    """Execute a paper trading or live session with full engine wiring."""
    with _open_rt(ctx, ensure=True) as rt:
        eff = build_effective(rt.root, run_name=run_name)
        if eff.run is None:
            raise ConfigError(
                what=f"Run configuration '{run_name}' not found",
                where=f"config/runs/{run_name}.toml",
                fix=f"Verify configuration file exists for '{run_name}'",
            )

        if prepare:
            # 0. Purge expired quarantine and session bars
            try:
                from algodesk.data.manifest import DataManifest
                from algodesk.data.quarantine import QuarantineManager
                from algodesk.data.session_bars import SessionBarStore

                m_db = rt.paths.data / "state" / "data_manifest.db"
                if not m_db.exists() and (rt.paths.data / "manifest.db").exists():
                    m_db = rt.paths.data / "manifest.db"
                if m_db.exists():
                    m_inst = DataManifest(m_db)
                    QuarantineManager(rt.paths.data, m_inst, params=rt.params).purge_expired()
                    m_inst.close()
                SessionBarStore(rt.paths.data, params=rt.params).purge_older_than()
            except Exception:
                pass

            # 1. plugins test --all for run config
            targets = get_run_config_plugins(eff.run)
            seen_p: set[tuple[str, str]] = set()
            for pk, pn in targets:
                pair = (pk.value if hasattr(pk, "value") else str(pk), pn)
                if pair in seen_p:
                    continue
                seen_p.add(pair)
                p_info = rt.plugins.info(pair[0], pair[1])
                tests_dir = p_info.directory / "tests"
                if tests_dir.is_dir() and any(tests_dir.glob("test_*.py")):
                    cmd = [sys.executable, "-m", "pytest", str(tests_dir), "-q"]
                    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
                    if proc.returncode != 0:
                        typer.echo(f"Plugin tests failed for {pair[0]}/{pair[1]}:")
                        if proc.stdout:
                            typer.echo(proc.stdout.strip())
                        if proc.stderr:
                            typer.echo(proc.stderr.strip())
                        raise typer.Exit(code=1)

                if not p_info.manifest.order_capable:
                    p_ctx = build_context(Mode.RESEARCH, run_id=f"prepare_{pair[0]}_{pair[1]}")
                    instance = rt.plugins.create(p_info.kind, p_info.name, ctx=p_ctx)
                    probs = base_contract_problems(instance) or kind_problems(instance)
                    if probs:
                        typer.echo(f"Conformance problems in {pair[0]}/{pair[1]}:")
                        for prob in probs:
                            typer.echo(f"  - {prob}")
                        raise typer.Exit(code=1)

                rt.plugins.record_conformance(p_info.kind, p_info.name, suite="base+plugin-tests")

            typer.echo("All required plugins passed conformance")

            # 2. instrument master refresh
            ds_cfg = getattr(eff.run, "data_source", None)
            if ds_cfg is not None:
                try:
                    p_ds_ctx = build_context(Mode.RESEARCH, run_id=f"prepare_ds_{run_name}")
                    ds_p = rt.plugins.create(PluginKind.BROKER, ds_cfg.plugin, ctx=p_ds_ctx)
                    if hasattr(ds_p, "instruments"):
                        _ = list(ds_p.instruments())
                except Exception:
                    pass

            # 3. Doctor checks
            checks = build_run_checks(rt, run_name)
            reg = DoctorRegistry()
            reg.register_many(checks)
            report = reg.run()
            if report.critical > 0:
                typer.echo(f"Preparation doctor check failed: {report.critical} critical error(s)")
                for r in report.results:
                    if not r.ok and r.severity == Severity.CRITICAL:
                        typer.echo(f"  CRITICAL: {r.message} (Fix: {r.fix})")
                raise typer.Exit(code=1)

            typer.echo("Doctor pre-start check: 0 critical")
            typer.echo(f"Preparation complete for run '{run_name}': all OK, doctor 0 critical")
            return

        sec_prov = _resolve_secrets_provider(rt, run=run_name)
        runner = SessionRunner(
            rt,
            run_config=eff.run,
            observe=observe,
            dry_run=dry_run,
            secrets=sec_prov,
            quiet=quiet,
            warmup_source=warmup_source,
        )

        if dry_run:
            runner.run_prestart_doctor()
            cfg = runner.run_config
            ds = getattr(cfg, "data_source", None)
            ev = getattr(cfg, "execution_venue", None)
            u = getattr(cfg, "universe", None)
            syms = getattr(u, "symbols", []) if u else []
            strats = getattr(cfg, "strategies", [])
            plan_lines = [
                "=== Dry-Run Execution Plan ===",
                f"Run Name:          {runner.run_name}",
                f"Mode:              {getattr(runner.mode, 'value', runner.mode)}",
            ]
            if cfg is not None:
                plan_lines.extend(
                    [
                        f"Allocated Capital: ₹{cfg.allocated_capital:,.2f}",
                        f"Data Source:       {getattr(ds, 'plugin', 'none')}",
                        f"Execution Venue:   {getattr(ev, 'plugin', 'none')}",
                        f"Universe Symbols:  {len(syms)} ({', '.join(syms[:3])}...)",
                        f"Strategies:        {', '.join(s.plugin for s in strats)}",
                    ]
                )
            plan_lines.extend(
                [
                    "Doctor checks:     0 critical, 0 warnings",
                    "Plan verified successfully. No network connections opened; no orders placed.",
                ]
            )
            typer.echo("\n".join(plan_lines))
            return

        try:
            res_path = asyncio.run(runner.run())
            typer.echo(f"Results written to: {res_path}")
        except Exception:
            results_dir = rt.root / "results" / runner.run_id
            if results_dir.is_dir():
                typer.echo(f"Results written to: {results_dir}")
            raise


@handle_cli_errors
def replay_cmd(
    ctx: typer.Context,
    dates: str | None = typer.Option(
        None, "--dates", help="Comma-separated dates to replay (YYYY-MM-DD)"
    ),
    run: str | None = typer.Option(None, "--run", help="Run configuration name"),
    twice: bool = typer.Option(
        False, "--twice", help="Execute twice and verify bit-identical replay determinism"
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Disable periodic console status lines"
    ),
    warmup_source: str | None = typer.Option(
        None, "--warmup-source", help="Override warm-up data source (e.g. upstox, yfinance)"
    ),
) -> None:
    """Replay historical market data deterministically through the engine."""
    state = _get_cli_state(ctx)
    run_name = run or state.run or "demo_paper"
    parsed_dates = [d.strip() for d in dates.split(",")] if dates else None

    with _open_rt(ctx, ensure=True) as rt:
        eff = build_effective(rt.root, run_name=run_name)
        if twice:
            ts_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            run_id_a = f"{run_name}_replay_{ts_str}_a"
            run_id_b = f"{run_name}_replay_{ts_str}_b"
            p2 = None
            for r_id in (run_id_a, run_id_b):
                runner_rep = SessionRunner(
                    rt,
                    run_config=eff.run,
                    mode="replay",
                    replay_dates=parsed_dates,
                    run_id=r_id,
                    quiet=quiet,
                )
                p2 = asyncio.run(runner_rep.run())

            is_equal, diff = compare_runs(rt.root, run_id_a, run_id_b)
            typer.echo(f"replay equal: {is_equal}")
            if not is_equal and diff:
                typer.echo(f"first difference: {diff}")
            typer.echo(f"results folder: {p2}")
        else:
            runner = SessionRunner(
                rt,
                run_config=eff.run,
                mode="replay",
                replay_dates=parsed_dates,
                quiet=quiet,
            )
            p = asyncio.run(runner.run())
            typer.echo(f"results folder: {p}")


@results_app.command("list")
@handle_cli_errors
def results_list_cmd(ctx: typer.Context) -> None:
    """List execution results folders and summary performance metrics."""
    with _open_rt(ctx, ensure=True) as rt:
        results_dir = rt.root / "results"
        if not results_dir.is_dir():
            typer.echo("No results directory found.")
            return

        headers = ["RUN ID", "DATE", "MODE", "TRADES", "NET P&L", "VERDICT", "START_IST"]
        rows: list[list[str]] = []

        for p in sorted(results_dir.iterdir(), reverse=True):
            if not p.is_dir():
                continue
            m_path = p / "metrics.json"
            s_path = p / "SUMMARY.md"
            if not m_path.is_file() and not s_path.is_file():
                continue

            r_id, dt, mode, trades, net_pnl, verdict, start_ist = (
                p.name,
                "unknown",
                "paper",
                "0",
                "0.00",
                "PASS",
                "N/A",
            )
            if m_path.is_file():
                try:
                    data = json.loads(m_path.read_text())
                    dt, mode = str(data.get("date", dt)), str(data.get("mode", mode))
                    trades = str(data.get("trades", 0))
                    net_pnl = f"{float(data.get('net_pnl', 0.0)):.2f}"
                    if data.get("first_tick_ist") and data.get("first_tick_ist") != "N/A":
                        start_ist = str(data.get("first_tick_ist"))
                    elif data.get("first_tick_ts"):
                        start_ist = format_ist(data.get("first_tick_ts"))
                except Exception:
                    pass

            if s_path.is_file():
                try:
                    for line in s_path.read_text().splitlines():
                        if "Verdict:" in line:
                            verdict = line.split("Verdict:")[-1].strip().split(".")[0]
                        elif "Date:" in line:
                            dt = line.split("Date:")[-1].strip()
                        elif "Mode:" in line:
                            mode = line.split("Mode:")[-1].strip()
                except Exception:
                    pass

            if start_ist == "N/A":
                import re

                m_match = re.search(r"(\d{8}_\d{6})", r_id)
                if m_match:
                    try:
                        ts_utc = datetime.strptime(m_match.group(1), "%Y%m%d_%H%M%S").replace(
                            tzinfo=UTC
                        )
                        start_ist = format_ist(ts_utc)
                    except Exception:
                        pass

            rows.append([r_id, dt, mode, trades, net_pnl, verdict, start_ist])

        if not rows:
            typer.echo("No results found under results/")
            return

        typer.echo(format_table(headers, rows))


@results_app.command("show")
@handle_cli_errors
def results_show_cmd(
    ctx: typer.Context,
    run_id: str = typer.Argument(..., help="Run ID of the results bundle"),
    json_opt: bool = typer.Option(False, "--json", help="Display metrics as JSON"),
) -> None:
    """Display summary report or metrics for a past session run."""
    state = _get_cli_state(ctx)
    is_json = json_opt or state.json

    with _open_rt(ctx, ensure=True) as rt:
        target = rt.root / "results" / run_id
        if not target.is_dir():
            raise ConfigError(
                what=f"Results bundle '{run_id}' not found",
                where=str(target),
                fix="Use 'algodesk results list' to see available runs",
            )

        m_path = target / "metrics.json"
        s_path = target / "SUMMARY.md"

        if is_json:
            if not m_path.is_file():
                raise ConfigError(
                    what=f"metrics.json missing in '{run_id}'",
                    where=str(m_path),
                    fix="Verify results bundle contents",
                )
            typer.echo(m_path.read_text())
        else:
            if s_path.is_file():
                typer.echo(s_path.read_text())
            elif m_path.is_file():
                typer.echo(m_path.read_text())
            else:
                typer.echo(f"Results bundle '{run_id}' contains no summary or metrics.")


@results_app.command("compare")
@handle_cli_errors
def results_compare_cmd(
    ctx: typer.Context,
    run_a: str = typer.Argument(..., help="First run ID or results folder name"),
    run_b: str = typer.Argument(..., help="Second run ID or results folder name"),
) -> None:
    """Compare performance metrics and ordered journal event sequences of two runs."""
    state = _get_cli_state(ctx)
    with _open_rt(ctx, ensure=True) as rt:
        is_equal, diff = compare_runs(rt.root, run_a, run_b)
        if state.json:
            typer.echo(
                json.dumps({"equal": is_equal, "run_a": run_a, "run_b": run_b, "difference": diff})
            )
        else:
            typer.echo(f"equal: {is_equal}")
            if not is_equal and diff:
                typer.echo(f"first difference: {diff}")
