"""Replay configuration loader reconstructing run configs and registry from session artifacts."""

from __future__ import annotations

import ast
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from algodesk.api.errors import ConfigError
from algodesk.api.kinds import Mode
from algodesk.config.models import (
    DeploymentConfig,
    FeesConfig,
    PluginRef,
    RunConfig,
    RunMeta,
    StrategyConfig,
    UniverseConfig,
)
from algodesk.ops.runtime import Runtime
from algodesk.params.registry import ParamRegistry
from algodesk.params.specs import validate_value

_TOP_FIELDS = (
    "allocated_capital",
    "run",
    "data_source",
    "execution_venue",
    "calendars",
    "secrets",
    "fees",
    "deployment",
    "universe",
    "strategies",
    "notifiers",
)


def _safe_eval_ast_node(node: ast.AST, allowed: set[Any], ns: dict[str, Any]) -> Any:
    """Evaluate AST node safely against allowed types and namespace without eval()."""
    if isinstance(node, ast.Expression):
        return _safe_eval_ast_node(node.body, allowed, ns)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp):
        val = _safe_eval_ast_node(node.operand, allowed, ns)
        return -val if isinstance(node.op, ast.USub) else +val
    if isinstance(node, ast.List):
        return [_safe_eval_ast_node(el, allowed, ns) for el in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_safe_eval_ast_node(el, allowed, ns) for el in node.elts)
    if isinstance(node, ast.Dict):
        return {
            _safe_eval_ast_node(k, allowed, ns): _safe_eval_ast_node(v, allowed, ns)
            for k, v in zip(node.keys, node.values, strict=False)
            if k is not None
        }
    if isinstance(node, ast.Name):
        if node.id in ns:
            return ns[node.id]
        raise ValueError(f"Unknown identifier in config: {node.id}")
    if isinstance(node, ast.Attribute):
        return getattr(_safe_eval_ast_node(node.value, allowed, ns), node.attr)
    if isinstance(node, ast.Call):
        func = _safe_eval_ast_node(node.func, allowed, ns)
        if func not in allowed:
            raise ValueError(f"Disallowed call in config: {func}")
        args = [_safe_eval_ast_node(a, allowed, ns) for a in node.args]
        kwargs = {
            kw.arg: _safe_eval_ast_node(kw.value, allowed, ns)
            for kw in node.keywords
            if kw.arg is not None
        }
        return func(*args, **kwargs)
    raise ValueError(f"Unsupported AST node: {type(node).__name__}")


def parse_recorded_config(text: str) -> RunConfig:
    """Parse string representation from config.effective.txt into RunConfig."""
    clean_text = text.strip()
    if not clean_text:
        raise ConfigError(
            what="Recorded config.effective.txt is empty",
            where="config.effective.txt",
            fix="Provide a valid session recording with populated config.effective.txt",
        )
    # Convert Mode enums (<Mode.PAPER: 'paper'> -> Mode.PAPER)
    clean_text = re.sub(r"<Mode\.([A-Z]+):[^>]+>", r"Mode.\1", clean_text)
    splits = [m.start() for m in re.finditer(r"\b(" + "|".join(_TOP_FIELDS) + r")=", clean_text)]
    splits.append(len(clean_text))
    parts = [clean_text[splits[i] : splits[i + 1]].strip() for i in range(len(splits) - 1)]

    ns = {
        "RunConfig": RunConfig,
        "RunMeta": RunMeta,
        "PluginRef": PluginRef,
        "FeesConfig": FeesConfig,
        "DeploymentConfig": DeploymentConfig,
        "UniverseConfig": UniverseConfig,
        "StrategyConfig": StrategyConfig,
        "Decimal": Decimal,
        "Mode": Mode,
    }
    allowed = set(ns.values())
    py_expr = "RunConfig(" + ", ".join(parts) + ")"
    try:
        tree = ast.parse(py_expr, mode="eval")
        cfg = _safe_eval_ast_node(tree, allowed, ns)
        if not isinstance(cfg, RunConfig):
            raise ConfigError(
                what="Parsed configuration is not a RunConfig instance",
                where="config.effective.txt",
                fix="Ensure config.effective.txt contains valid serialized RunConfig",
            )
    except Exception as exc:
        raise ConfigError(
            what=f"Failed to parse recorded configuration: {exc}",
            where="config.effective.txt",
            fix="Ensure config.effective.txt contains valid serialized RunConfig",
        ) from exc

    # Default missing strategy product to delivery (cnc)
    for s_cfg in cfg.strategies:
        if isinstance(s_cfg.params, dict) and "product" not in s_cfg.params:
            s_cfg.params["product"] = "cnc"
    return cfg


def apply_recorded_registry_snapshot(snapshot_path: Path, params: ParamRegistry) -> None:
    """Validate and apply recorded parameter snapshot to ParamRegistry."""
    if not snapshot_path.is_file():
        return
    try:
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(
            what=f"Corrupt registry snapshot JSON: {exc}",
            where=str(snapshot_path),
            fix="Verify registry.snapshot.json format",
        ) from exc

    for key, val in data.get("params", {}).items():
        p_name, scope = key.split("@", 1) if "@" in key else (key, "global")
        try:
            spec = params.spec(p_name)
        except Exception:
            raise ConfigError(
                what=f"Recorded parameter '{p_name}' is not registered in parameter registry",
                where=f"registry.snapshot.json:{p_name}",
                fix="Register parameter specification or remove unknown parameter",
            ) from None

        try:
            valid_val = validate_value(spec, val)
        except Exception as exc:
            raise ConfigError(
                what=f"Recorded value '{val}' for parameter '{p_name}' cannot be applied: {exc}",
                where=f"registry.snapshot.json:{p_name}",
                fix="Ensure recorded parameter value matches spec constraints",
            ) from exc

        params._overrides[(p_name, scope)] = (valid_val, "recorded_snapshot")
        params._cached_effective[p_name] = params.get(p_name, scope=scope)


def check_session_manifest_warnings(
    manifest_path: Path, runtime: Runtime, run_config: RunConfig
) -> list[str]:
    """Check fee schedule existence and collect version / hash diff warnings."""
    sched = run_config.fees.schedule
    try:
        p_info = runtime.plugins.info("fees", sched)
        if p_info is None:
            raise ConfigError(
                what=f"Recorded fee schedule '{sched}' is not installed",
                where="FeesConfig.schedule",
                fix="Install required fee schedule plugin",
            )
    except Exception as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(
            what=f"Recorded fee schedule '{sched}' is not installed",
            where="FeesConfig.schedule",
            fix="Install required fee schedule plugin",
        ) from exc

    warnings: list[str] = []
    if not manifest_path.is_file():
        return warnings
    try:
        man = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return warnings

    code_ver = man.get("code_version")
    if code_ver and code_ver != "0.1.0":
        warnings.append(f"code version: {code_ver} (paper) != 0.1.0 (current)")

    pv_sess = man.get("plugin_versions") or {}
    curr_plugins = {p.name: p for p in runtime.plugins.list()}
    for p_name, p_info in pv_sess.items():
        p_curr = curr_plugins.get(p_name)
        if not p_curr:
            continue
        rec_ver = p_info.get("version")
        if rec_ver and p_curr.manifest.version != rec_ver:
            warnings.append(
                f"plugin '{p_name}' version: {rec_ver} (paper) != "
                f"{p_curr.manifest.version} (current)"
            )
        rec_hash = p_info.get("hash")
        if rec_hash and p_curr.code_hash != rec_hash:
            warnings.append(
                f"plugin '{p_name}' code hash: {rec_hash} (paper) != {p_curr.code_hash} (current)"
            )
    return warnings


def load_recorded_session_config(
    session_dir: Path, runtime: Runtime
) -> tuple[RunConfig, list[str]]:
    """Load RunConfig, apply registry snapshot, and verify fee schedule and versions."""
    eff_path = session_dir / "config.effective.txt"
    if not eff_path.is_file():
        raise ConfigError(
            what=f"Recorded configuration not found in {session_dir.name}",
            where=f"{session_dir.name}/config.effective.txt",
            fix="Provide a session directory containing config.effective.txt",
        )
    cfg = parse_recorded_config(eff_path.read_text(encoding="utf-8"))
    if runtime.params is not None and isinstance(runtime.params, ParamRegistry):
        apply_recorded_registry_snapshot(session_dir / "registry.snapshot.json", runtime.params)
    warnings = check_session_manifest_warnings(session_dir / "manifest.json", runtime, cfg)
    return cfg, warnings
