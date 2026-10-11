"""Strict configuration parsing and side-effect-free preparation for local runs."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import tomllib

from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.control_plane import build_control_plane_asset
from aigineering.core.contract_admission import validate_contract_commitment
from aigineering.core.skill_loader import SkillLoader
from aigineering.agent.tool_registry_loader import load_tool_registry
from aigineering.fleet_config import (
    LocalFleetConfig,
    FleetWorkerSpec,
    load_fleet_config,
)
from aigineering.protocol.types import Asset, Contract

_INPUT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]*\Z")
_MAX_INPUT_BYTES = 1_048_576
_TASK_FIELDS = frozenset(
    {
        "name",
        "inputs",
        "outputs",
        "activation",
        "budget",
        "description",
        "description_file",
        "labels",
        "tool_scope",
        "worker_capabilities",
        "worker_pools",
        "delegation_capabilities",
        "delegation_pools",
        "sensitive_input_policy",
        "acceptance_policy",
        "acceptance_policy_file",
        "acceptance_evidence_inputs",
    }
)
_CONTRACT_LIST_FIELDS = (
    "inputs",
    "outputs",
    "labels",
    "tool_scope",
    "worker_capabilities",
    "worker_pools",
    "delegation_capabilities",
    "delegation_pools",
)


@dataclass(frozen=True)
class PreparedConfiguredRun:
    source_path: Path
    fleet: LocalFleetConfig
    fleet_path: Path
    skill_assets: tuple[Asset, ...]
    input_assets: tuple[Asset, ...]
    contract: Contract | None
    acceptance_evidence_inputs: tuple[str, ...]
    resume_task_id: str | None
    workers: tuple[tuple[FleetWorkerSpec, object], ...]
    tool_registries: tuple[tuple[str, object], ...]
    poll_interval: float

    @property
    def db_path(self) -> str:
        # Fleet paths intentionally retain the caller's workspace semantics.
        return self.fleet.db_path


@dataclass(frozen=True)
class PreparedFleetResources:
    """Validated adapters and registries shared by bounded and persistent runs."""

    workers: tuple[tuple[FleetWorkerSpec, object], ...]
    tool_registries: tuple[tuple[str, object], ...]


def prepare_fleet_resources(
    fleet: LocalFleetConfig, *, base_dir: str | Path | None = None
) -> PreparedFleetResources:
    """Build every configured adapter through one application-owned path.

    Existing configured runs retain their current working-directory lookup by
    omitting ``base_dir``. Persistent service configuration can pass the
    config directory so explicit file-based tool registries are resolved there.
    """
    from aigineering.fleet_config import build_fleet_worker

    base = Path(base_dir).resolve() if base_dir is not None else None
    registry_refs = sorted(
        {
            _resolve_tool_registry_reference(spec.tool_registry, base)
            for spec in fleet.workers
            if spec.tool_registry
        }
    )
    registries = tuple(
        (reference, load_tool_registry(reference)) for reference in registry_refs
    )
    registry_map = dict(registries)
    workers = tuple(
        (
            spec,
            build_fleet_worker(
                spec,
                registry=registry_map[
                    _resolve_tool_registry_reference(spec.tool_registry, base)
                ],
            )
            if spec.tool_registry
            else build_fleet_worker(spec),
        )
        for spec in fleet.workers
    )
    return PreparedFleetResources(workers=workers, tool_registries=registries)


def _resolve_tool_registry_reference(reference: str, base: Path | None) -> str:
    if base is None:
        return reference
    module, separator, factory = reference.rpartition(":")
    if not separator or not module or not factory:
        return reference
    candidate = Path(module)
    if candidate.is_absolute() or candidate.suffix == ".py" or "/" in module:
        if not candidate.is_absolute():
            candidate = base / candidate
        if candidate.is_file():
            return f"{candidate.resolve()}:{factory}"
    return reference


def prepare_configured_run(
    path: str | Path, *, resume_task_id: str | None = None
) -> PreparedConfiguredRun:
    """Validate declarations/files and build adapters before runtime provisioning."""
    source = Path(path).resolve()
    data = _read_toml(source)
    if resume_task_id is not None and (
        not isinstance(resume_task_id, str) or not resume_task_id.strip()
    ):
        raise ValueError("resume_task_id must be a non-empty Contract ID")
    if resume_task_id is not None:
        if "run" in data:
            _keys(data, {"run", "inputs", "task"}, "run preset")
            run = _table(data, "run", required=True)
            _keys(run, {"fleet", "skills"}, "[run]")
            fleet_value = _string(run, "fleet", required=True)
            fleet_path = _relative_file(source.parent, fleet_value, "run.fleet")
            fleet = load_fleet_config(fleet_path)
        else:
            if set(data) - {"fleet", "workers"}:
                raise ValueError("resume config must be a fleet TOML or run preset")
            fleet = load_fleet_config(source)
            fleet_path = source
        skills: tuple[Asset, ...] = ()
        inputs: tuple[Asset, ...] = ()
        contract = None
        evidence_inputs: tuple[str, ...] = ()
    else:
        _keys(data, {"run", "inputs", "task"}, "run preset")
        run = _table(data, "run", required=True)
        _keys(run, {"fleet", "skills"}, "[run]")
        fleet_value = _string(run, "fleet", required=True)
        fleet_path = _relative_file(source.parent, fleet_value, "run.fleet")
        fleet = load_fleet_config(fleet_path)
        raw_skills = run.get("skills", [])
        if not _string_list(raw_skills):
            raise ValueError("run.skills must be a list of strings")
        skill_dirs = [
            str(_relative_dir(source.parent, value, "run.skills"))
            for value in raw_skills
        ]
        loader = SkillLoader()
        manifests = loader.scan(skill_dirs)
        for directory in skill_dirs:
            if not any(Path(directory).rglob("skill.toml")):
                raise ValueError(
                    f"configured Skill directory has no skill.toml: {directory}"
                )
        for manifest in manifests:
            if not isinstance(manifest.name, str) or not manifest.name.strip():
                raise ValueError(f"{manifest.directory}: skill name must be non-empty")
            if not isinstance(manifest.version, str) or not manifest.version.strip():
                raise ValueError(
                    f"{manifest.directory}: skill version must be non-empty"
                )
            if not manifest.content_path.is_file():
                raise ValueError(
                    f"skill content file is missing: {manifest.content_path}"
                )
            body = _read_text(manifest.content_path)
            if not body.strip():
                raise ValueError(f"skill content is empty: {manifest.content_path}")
        skills = tuple(loader.build_assets())
        inputs = _prepare_inputs(data.get("inputs", []), source.parent)
        task = _table(data, "task", required=True)
        contract = _prepare_contract(task, inputs, skills, source.parent)
        evidence_inputs = tuple(task.get("acceptance_evidence_inputs", ()))

    if not fleet.db_path.strip() or fleet.db_path == ":memory:":
        raise ValueError("configured runs require a non-empty persistent fleet.db_path")

    asset_names = [asset.name for asset in (*inputs, *skills)]
    if len(asset_names) != len(set(asset_names)):
        raise ValueError("prepared input and Skill Asset names must be unique")

    # Build every configured adapter before any domain/store/identity mutation.
    resources = prepare_fleet_resources(fleet)
    return PreparedConfiguredRun(
        source_path=source,
        fleet=fleet,
        fleet_path=fleet_path,
        skill_assets=skills,
        input_assets=inputs,
        contract=contract,
        acceptance_evidence_inputs=evidence_inputs,
        resume_task_id=resume_task_id,
        workers=resources.workers,
        tool_registries=resources.tool_registries,
        poll_interval=fleet.poll_interval,
    )


def _read_toml(path: Path) -> dict:
    try:
        raw = path.read_bytes()
        value = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read TOML config {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("config must contain a TOML table")
    return value


def _prepare_inputs(raw: object, base: Path) -> tuple[Asset, ...]:
    if not isinstance(raw, list):
        raise ValueError("inputs must be an array of tables")
    assets: list[Asset] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"inputs[{index}] must be a table")
        _keys(item, {"name", "path"}, f"inputs[{index}]")
        name = _string(item, "name", required=True)
        if not _INPUT_NAME.fullmatch(name):
            raise ValueError(f"invalid input name {name!r}")
        if name in seen:
            raise ValueError(f"duplicate input name {name!r}")
        seen.add(name)
        file_path = _relative_file(
            base, _string(item, "path", required=True), f"inputs[{index}].path"
        )
        try:
            with file_path.open("rb") as stream:
                raw = stream.read(_MAX_INPUT_BYTES + 1)
            if len(raw) > _MAX_INPUT_BYTES:
                raise ValueError(f"input exceeds {_MAX_INPUT_BYTES} bytes: {file_path}")
            content = raw.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"cannot read UTF-8 input {file_path}: {exc}") from exc
        if not content.strip():
            raise ValueError(f"input file is empty: {file_path}")
        assets.append(build_control_plane_asset(name=name, content=content))
    return tuple(assets)


def _prepare_contract(
    task: dict, inputs: tuple[Asset, ...], skills: tuple[Asset, ...], base: Path
) -> Contract:
    _keys(task, _TASK_FIELDS, "[task]")
    values = dict(task)
    name = values.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("task.name must be a non-empty string")
    activation = values.get("activation", "")
    if not isinstance(activation, str):
        raise ValueError("task.activation must be a string")
    for field in _CONTRACT_LIST_FIELDS:
        if field in values and not _string_list(values[field]):
            raise ValueError(f"task.{field} must be a list of strings")
        values[field] = tuple(values.get(field, ()))
        if len(set(values[field])) != len(values[field]):
            raise ValueError(f"task.{field} must not contain duplicates")
    if "description_file" in values and "description" in values:
        raise ValueError(
            "task.description and task.description_file are mutually exclusive"
        )
    if "description_file" in values:
        values["description"] = _read_text(
            _relative_file(
                base, _string(values, "description_file", True), "task.description_file"
            )
        )
    if not isinstance(values.get("description", ""), str):
        raise ValueError("task.description must be a string")
    values.pop("description_file", None)
    if "acceptance_policy_file" in values and "acceptance_policy" in values:
        raise ValueError(
            "task.acceptance_policy and task.acceptance_policy_file are mutually exclusive"
        )
    if "acceptance_policy_file" in values:
        try:
            policy = json.loads(
                _read_text(
                    _relative_file(
                        base,
                        _string(values, "acceptance_policy_file", True),
                        "task.acceptance_policy_file",
                    )
                )
            )
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"acceptance policy file is not valid JSON: {exc}"
            ) from exc
        values["acceptance_policy"] = policy
    values.pop("acceptance_policy_file", None)
    evidence_inputs = values.pop("acceptance_evidence_inputs", [])
    if not _string_list(evidence_inputs):
        raise ValueError("task.acceptance_evidence_inputs must be a list of strings")
    if len(set(evidence_inputs)) != len(evidence_inputs):
        raise ValueError("task.acceptance_evidence_inputs must not contain duplicates")
    if not set(evidence_inputs) <= set(values["inputs"]):
        raise ValueError("acceptance_evidence_inputs must be a subset of task.inputs")
    policy = values.get("acceptance_policy")
    if evidence_inputs:
        if not isinstance(policy, dict) or policy.get("mode") != "independent":
            raise ValueError(
                "acceptance_evidence_inputs requires an independent acceptance policy"
            )
        if "evidence_asset_ids" in policy:
            raise ValueError(
                "acceptance_evidence_inputs conflicts with explicit evidence_asset_ids"
            )
        policy = dict(policy)
        # Candidate publication assigns the durable asset:v1 IDs. These sorted
        # validation placeholders are replaced with those exact IDs at commit.
        policy["evidence_asset_ids"] = sorted(
            f"prepared:{name}" for name in evidence_inputs
        )
    if policy is not None and not isinstance(policy, dict):
        raise ValueError("task.acceptance_policy must be a table/object")
    if "sensitive_input_policy" in values and not isinstance(
        values["sensitive_input_policy"], dict
    ):
        raise ValueError("task.sensitive_input_policy must be a table")
    budget = values.get("budget", 5)
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise ValueError("task.budget must be a positive integer")
    values["budget"] = budget
    input_names = {asset.name for asset in inputs}
    if not set(values["inputs"]) <= input_names:
        raise ValueError("task.inputs must refer to declared [[inputs]] names")
    if not values["inputs"]:
        raise ValueError("task.inputs must declare at least one input")
    if not values["outputs"]:
        raise ValueError("task.outputs must declare at least one output")
    if set(values["outputs"]) & input_names:
        raise ValueError("task.outputs cannot reuse a declared input name")
    values["acceptance_policy"] = policy
    contract = build_control_plane_contract(
        **{
            key: value
            for key, value in values.items()
            if key in _CONTRACT_LIST_FIELDS
            or key
            in {
                "name",
                "outputs",
                "activation",
                "budget",
                "description",
                "sensitive_input_policy",
                "acceptance_policy",
            }
        }
    )
    available = inputs + skills
    labels = [label for label in contract.labels if not label.startswith("plugin:")]
    by_name = {asset.name: asset for asset in available if asset.promptable}
    missing = sorted(set(labels) - set(by_name))
    if missing:
        raise ValueError(
            f"task labels do not resolve to prepared promptable Assets: {missing}"
        )

    try:
        validate_contract_commitment(contract)
    except ValueError as exc:
        raise ValueError(
            f"invalid Contract activation, acceptance policy, or identity: {exc}"
        ) from exc
    return contract


def _relative_file(base: Path, value: str, field: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    if not candidate.is_file():
        raise ValueError(f"{field} must name an existing regular file: {candidate}")
    return candidate.resolve()


def _relative_dir(base: Path, value: str, field: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    if not candidate.is_dir():
        raise ValueError(f"{field} must name an existing directory: {candidate}")
    return candidate.resolve()


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read UTF-8 file {path}: {exc}") from exc


def _table(data: dict, key: str, *, required: bool = False) -> dict:
    value = data.get(key)
    if value is None and not required:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"[{key}] must be a TOML table")
    return value


def _keys(data: dict, allowed: set[str] | frozenset[str], where: str) -> None:
    unknown = set(data) - set(allowed)
    if unknown:
        raise ValueError(f"unknown fields in {where}: {sorted(unknown)}")


def _string(table: dict, key: str, required: bool = False) -> str:
    value = table.get(key)
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(
            f"{key} must be a non-empty string"
            if required
            else f"{key} must be a string"
        )
    return value


def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, str) and bool(item.strip()) for item in value
    )
