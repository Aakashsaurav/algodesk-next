"""Load shipped default parameter specifications from TOML files."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from algodesk.api.errors import ParamError
from algodesk.params.specs import ParamSpec

ALLOWED_PARAM_KEYS = {
    "name",
    "type",
    "default",
    "min",
    "max",
    "choices",
    "unit",
    "scope",
    "tighten",
    "description",
    "owner",
}


def _parse_param_table(raw: dict[str, Any], path: Path) -> ParamSpec:
    """Validate keys and construct a ParamSpec from a TOML table."""
    unknown = set(raw.keys()) - ALLOWED_PARAM_KEYS
    if unknown:
        unknown_str = ", ".join(sorted(unknown))
        name = str(raw.get("name", "unnamed"))
        raise ParamError(
            what=f"Unknown parameter attribute(s): {unknown_str}",
            where=f"{path}:{name}",
            fix=f"Allowed keys are: {sorted(ALLOWED_PARAM_KEYS)}",
        )
    return ParamSpec(**raw)


def load_defaults(dir_path: Path | str) -> list[ParamSpec]:
    """Load and validate all parameter specs from *.toml files in dir_path."""
    path = Path(dir_path)
    if not path.is_dir():
        raise ParamError(
            what=f"Defaults directory not found: {path}",
            where=str(path),
            fix="Provide an existing directory containing *.toml files",
        )

    specs: list[ParamSpec] = []
    seen: set[str] = set()

    for toml_file in sorted(path.glob("*.toml")):
        with open(toml_file, "rb") as f:
            data = tomllib.load(f)

        unknown_top = set(data.keys()) - {"param"}
        if unknown_top:
            raise ParamError(
                what=f"Unknown top-level sections: {sorted(unknown_top)}",
                where=str(toml_file),
                fix="TOML parameter files should only contain [[param]] tables",
            )

        param_tables = data.get("param", [])
        if not isinstance(param_tables, list):
            raise ParamError(
                what="Expected [[param]] list of tables",
                where=str(toml_file),
                fix="Define parameters as [[param]] tables",
            )

        for raw_table in param_tables:
            if not isinstance(raw_table, dict):
                raise ParamError(
                    what="Each [[param]] entry must be a table",
                    where=str(toml_file),
                    fix="Check TOML syntax for [[param]] entries",
                )
            spec = _parse_param_table(raw_table, toml_file)
            if spec.name in seen:
                raise ParamError(
                    what=f"Duplicate parameter name: {spec.name}",
                    where=f"{toml_file}:{spec.name}",
                    fix="Remove duplicate parameter declaration across defaults",
                )
            seen.add(spec.name)
            specs.append(spec)

    return specs
