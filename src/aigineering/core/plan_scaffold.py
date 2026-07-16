"""Plan scaffold model, parser, validator, and symbolic compiler (v0.5.0).

ADR-018 structured planning scaffold: before creating child contracts, the plan
LLM output is parsed as an intermediate scaffold that validates coverage,
data-flow, activation integrity, output re-commitment, and input reachability.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aigineering.core.activation import activation_names as extract_activation_names
from aigineering.core.authority import RESERVED_PREFIXES
from aigineering.protocol.immutability import deep_freeze, deep_thaw

if TYPE_CHECKING:
    from aigineering.protocol.types import Asset, Contract

# ---------------------------------------------------------------------------
# Scaffold field detection
# ---------------------------------------------------------------------------

_SCAFFOLD_STRUCTURAL_FIELDS: frozenset[str] = frozenset(
    {
        "step_1_tasks",
        "step_2_data_flow",
        "step_3_activation",
        "intermediate_assets",
        "goal_outline",
    }
)

# Plan-specific reserved prefixes (superset of authority.RESERVED_PREFIXES).
_PLAN_RESERVED_PREFIXES: frozenset[str] = RESERVED_PREFIXES | frozenset({"_persona_"})

# Fields the planner must not set.
_PLAN_PROTECTED_FIELDS: frozenset[str] = frozenset({"trust_tier", "created_by"})

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScaffoldTask:
    """A single task inside a plan scaffold (step 1)."""

    name: str
    description: str = ""
    consumes: tuple[str, ...] = ()  # asset names this task reads
    produces: tuple[str, ...] = ()  # asset names this task writes
    activation: str = ""  # boolean expression over asset names
    budget: int = 1
    tool_scope: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "consumes", tuple(self.consumes))
        object.__setattr__(self, "produces", tuple(self.produces))
        object.__setattr__(self, "tool_scope", tuple(self.tool_scope))
        object.__setattr__(self, "labels", tuple(self.labels))


@dataclass(frozen=True)
class ScaffoldDataFlow:
    """Data-flow wiring for a task (step 2)."""

    task_name: str
    consumes: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "consumes", tuple(self.consumes))
        object.__setattr__(self, "produces", tuple(self.produces))


@dataclass(frozen=True)
class ScaffoldActivation:
    """Activation gating for a task (step 3)."""

    task_name: str
    expression: str = ""
    depends_on: tuple[str, ...] = ()  # asset names this task waits for

    def __post_init__(self) -> None:
        object.__setattr__(self, "depends_on", tuple(self.depends_on))


@dataclass(frozen=True)
class PlanScaffold:
    """Structured planning scaffold — the intermediate layer between LLM plan
    output and child contract creation.
    """

    reason: str = ""
    goal_outline: str = ""
    intermediate_assets: tuple[str, ...] = ()  # semantic names like {raw_evidence}
    step_1_tasks: tuple[ScaffoldTask, ...] = ()
    step_2_data_flow: tuple[ScaffoldDataFlow, ...] = ()
    step_3_activation: tuple[ScaffoldActivation, ...] = ()
    final_contracts: tuple[dict, ...] = ()  # raw contract dicts, debug/legacy only

    def __post_init__(self) -> None:
        object.__setattr__(self, "intermediate_assets", tuple(self.intermediate_assets))
        object.__setattr__(self, "step_1_tasks", tuple(self.step_1_tasks))
        object.__setattr__(self, "step_2_data_flow", tuple(self.step_2_data_flow))
        object.__setattr__(self, "step_3_activation", tuple(self.step_3_activation))
        object.__setattr__(
            self,
            "final_contracts",
            tuple(deep_freeze(contract) for contract in self.final_contracts),
        )


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def parse_plan_scaffold(asset: Asset) -> PlanScaffold | None:
    """Try parsing an asset content as a PlanScaffold.

    Returns a PlanScaffold if scaffold-specific fields are present,
    ``None`` if the content is a legacy ``{"contracts": [...]}`` format
    or otherwise unparseable as a scaffold.
    """
    try:
        payload = json.loads(asset.content)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    # Detect scaffold vs legacy
    has_scaffold_fields = bool(_SCAFFOLD_STRUCTURAL_FIELDS & set(payload.keys()))
    has_final_contracts = isinstance(payload.get("final_contracts"), list)

    if not has_scaffold_fields:
        # Legacy: only "contracts" field, no scaffold structure
        return None

    # Build scaffold from JSON
    return _dict_to_scaffold(payload, has_final_contracts)


def _dict_to_scaffold(d: dict, has_final_contracts: bool) -> PlanScaffold:
    """Convert a validated dict into a PlanScaffold."""
    tasks_raw: list[dict] = _ensure_list(d.get("step_1_tasks"))
    data_flow_raw: list[dict] = _ensure_list(d.get("step_2_data_flow"))
    activation_raw: list[dict] = _ensure_list(d.get("step_3_activation"))
    intermediate_raw: list = _ensure_list(d.get("intermediate_assets"))
    final_contracts_raw: list[dict] = (
        _ensure_list(d.get("final_contracts")) if has_final_contracts else []
    )

    tasks = tuple(
        ScaffoldTask(
            name=str(t.get("name", "")),
            description=str(t.get("description", "")),
            consumes=tuple(_string_list(t.get("consumes"))),
            produces=tuple(_string_list(t.get("produces"))),
            activation=str(t.get("activation", "")),
            budget=_positive_int(t.get("budget"), 1),
            tool_scope=tuple(_string_list(t.get("tool_scope"))),
            labels=tuple(_string_list(t.get("labels"))),
        )
        for t in tasks_raw
        if isinstance(t, dict)
    )

    data_flows = tuple(
        ScaffoldDataFlow(
            task_name=str(df.get("task_name", "")),
            consumes=tuple(_string_list(df.get("consumes"))),
            produces=tuple(_string_list(df.get("produces"))),
        )
        for df in data_flow_raw
        if isinstance(df, dict)
    )

    activations = tuple(
        ScaffoldActivation(
            task_name=str(a.get("task_name", "")),
            expression=str(a.get("expression", "")),
            depends_on=tuple(_string_list(a.get("depends_on"))),
        )
        for a in activation_raw
        if isinstance(a, dict)
    )

    return PlanScaffold(
        reason=str(d.get("reason", "")),
        goal_outline=str(d.get("goal_outline", "")),
        intermediate_assets=tuple(
            str(s) for s in intermediate_raw if isinstance(s, str)
        ),
        step_1_tasks=tasks,
        step_2_data_flow=data_flows,
        step_3_activation=activations,
        final_contracts=tuple(final_contracts_raw),
    )


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def validate_plan_scaffold(
    scaffold: PlanScaffold,
    parent_contract: Contract | None = None,
) -> list[dict]:
    """Validate a scaffold's structural integrity.

    Returns a list of validation error dicts (empty = valid).  Errors use
    a format compatible with the rejection entries in
    ``contracts_from_plan_asset``.
    """
    errors: list[dict] = []

    data_flow_names = {df.task_name for df in scaffold.step_2_data_flow}
    activation_names = {a.task_name for a in scaffold.step_3_activation}

    # --- Coverage: every task must have data-flow and activation entries ---
    for task in scaffold.step_1_tasks:
        if task.name not in data_flow_names:
            errors.append(
                {
                    "child_name": task.name,
                    "field": "step_2_data_flow",
                    "reason": f"task '{task.name}' has no data-flow entry",
                    "action": "scaffold_rejected",
                    "expected": f"data-flow entry for '{task.name}'",
                    "actual": "missing",
                }
            )
        if task.name not in activation_names:
            errors.append(
                {
                    "child_name": task.name,
                    "field": "step_3_activation",
                    "reason": f"task '{task.name}' has no activation entry",
                    "action": "scaffold_rejected",
                    "expected": f"activation entry for '{task.name}'",
                    "actual": "missing",
                }
            )

    if errors:
        return errors

    # Build lookup maps
    df_map = {df.task_name: df for df in scaffold.step_2_data_flow}
    act_map = {a.task_name: a for a in scaffold.step_3_activation}

    # --- Collect the declared universe of asset names ---
    parent_outputs: set[str] = (
        set(parent_contract.outputs) if parent_contract else set()
    )
    parent_inputs: set[str] = set(parent_contract.inputs) if parent_contract else set()
    intermediate_set: set[str] = set(scaffold.intermediate_assets)
    sibling_outputs: set[str] = set()
    for df in scaffold.step_2_data_flow:
        sibling_outputs.update(df.produces)

    all_provided = parent_inputs | intermediate_set | sibling_outputs | parent_outputs

    # --- Per-task validation ---
    for task in scaffold.step_1_tasks:
        df = df_map.get(task.name)
        act = act_map.get(task.name)

        consumes: tuple[str, ...] = df.consumes if df else task.consumes
        produces: tuple[str, ...] = df.produces if df else task.produces
        activation_expr: str = act.expression if act else task.activation
        activation_deps: tuple[str, ...] = act.depends_on if act else ()

        # Protected output names
        violated = [
            o for o in produces if any(o.startswith(p) for p in _PLAN_RESERVED_PREFIXES)
        ]
        if violated:
            errors.append(
                {
                    "child_name": task.name,
                    "field": "outputs",
                    "reason": f"outputs {violated} use reserved prefixes",
                    "action": "scaffold_rejected",
                    "expected": f"no prefix in {sorted(_PLAN_RESERVED_PREFIXES)}",
                    "actual": str(violated),
                }
            )

        # Input reachability: every consumed asset must be declared somewhere
        missing_consumes = set(consumes) - all_provided - set(produces)
        if missing_consumes:
            errors.append(
                {
                    "child_name": task.name,
                    "field": "inputs",
                    "reason": (
                        f"consumed assets {sorted(missing_consumes)} are not in "
                        f"parent inputs, intermediate assets, sibling outputs, "
                        f"or parent outputs"
                    ),
                    "action": "scaffold_rejected",
                    "expected": "subset of declared universe",
                    "actual": str(sorted(missing_consumes)),
                }
            )

        # Activation reachability: every activation dependency must be declared
        missing_activation_deps = set(activation_deps) - all_provided - set(produces)
        if missing_activation_deps:
            errors.append(
                {
                    "child_name": task.name,
                    "field": "activation",
                    "reason": (
                        f"activation depends on {sorted(missing_activation_deps)} "
                        f"which are not declared in scaffold"
                    ),
                    "action": "scaffold_rejected",
                    "expected": "subset of declared universe",
                    "actual": str(sorted(missing_activation_deps)),
                }
            )

        # Activation expression: check that referenced names are declared
        if activation_expr:
            activation_refs = extract_activation_names(activation_expr)
            unknown_refs = activation_refs - all_provided - set(produces)
            if unknown_refs:
                errors.append(
                    {
                        "child_name": task.name,
                        "field": "activation",
                        "reason": (
                            f"activation expression references "
                            f"{sorted(unknown_refs)} which are not declared "
                            f"in scaffold"
                        ),
                        "action": "scaffold_rejected",
                        "expected": "subset of declared universe",
                        "actual": str(sorted(unknown_refs)),
                    }
                )

        # Protected fields (trust_tier, created_by) — scaffold tasks should not set them
        # (ScaffoldTask doesn't have these fields, so this is a no-op for scaffold)

    # --- Output re-commitment: if parent has outstanding outputs, check they
    #     are promised by at least one task ---
    if parent_contract and parent_contract.outputs:
        # For replan: check that every parent output is promised by at least
        # one task, or is already satisfied.
        promised_outputs: set[str] = set()
        for df in scaffold.step_2_data_flow:
            promised_outputs.update(df.produces)
        for task in scaffold.step_1_tasks:
            promised_outputs.update(task.produces)

        outstanding = set(parent_contract.outputs) - promised_outputs
        if outstanding and scaffold.step_1_tasks:
            # Only flag if there ARE tasks (empty scaffold with only final_contracts
            # is a legacy case handled elsewhere)
            errors.append(
                {
                    "child_name": "(parent)",
                    "field": "output_recommitment",
                    "reason": (
                        f"parent outputs {sorted(outstanding)} are not "
                        f"re-promised by any task in the scaffold"
                    ),
                    "action": "scaffold_rejected",
                    "expected": f"all parent outputs {sorted(parent_contract.outputs)} promised",
                    "actual": f"missing: {sorted(outstanding)}",
                }
            )

    return errors


# ---------------------------------------------------------------------------
# Symbolic placeholder compiler
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_-]*)\}")


def compile_placeholder_names(
    scaffold: PlanScaffold,
    placeholder_map: dict[str, str] | None = None,
) -> PlanScaffold:
    """Compile symbolic placeholders like ``{raw_evidence}`` to stable names.

    Parameters
    ----------
    scaffold : PlanScaffold
        The scaffold to compile.
    placeholder_map : dict[str, str] | None
        Mapping from placeholder tokens (without braces) to stable names.
        If ``None``, placeholder names are used as-is (braces stripped).

    Returns a new PlanScaffold with compiled names.
    """
    pm = placeholder_map or {}

    def _resolve(name: str) -> str:
        """Resolve a single name, expanding placeholders if applicable."""
        # Check if the name is entirely a placeholder: {name}
        m = _PLACEHOLDER_RE.fullmatch(name)
        if m:
            key = m.group(1)
            return pm.get(key, key)
        # Check if the name contains embedded placeholders: prefix_{name}_suffix
        result = name
        for match in _PLACEHOLDER_RE.finditer(name):
            key = match.group(1)
            replacement = pm.get(key, key)
            result = result.replace(match.group(0), replacement)
        return result

    def _compile_names(names: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_resolve(n) for n in names)

    def _compile_task(task: ScaffoldTask) -> ScaffoldTask:
        return ScaffoldTask(
            name=_resolve(task.name),
            description=task.description,
            consumes=_compile_names(task.consumes),
            produces=_compile_names(task.produces),
            activation=_resolve(task.activation),
            budget=task.budget,
            tool_scope=task.tool_scope,
            labels=task.labels,
        )

    def _compile_data_flow(df: ScaffoldDataFlow) -> ScaffoldDataFlow:
        return ScaffoldDataFlow(
            task_name=_resolve(df.task_name),
            consumes=_compile_names(df.consumes),
            produces=_compile_names(df.produces),
        )

    def _compile_activation(act: ScaffoldActivation) -> ScaffoldActivation:
        return ScaffoldActivation(
            task_name=_resolve(act.task_name),
            expression=_resolve(act.expression),
            depends_on=_compile_names(act.depends_on),
        )

    compiled_intermediate = _compile_names(scaffold.intermediate_assets)
    compiled_tasks = tuple(_compile_task(t) for t in scaffold.step_1_tasks)
    compiled_df = tuple(_compile_data_flow(df) for df in scaffold.step_2_data_flow)
    compiled_act = tuple(_compile_activation(a) for a in scaffold.step_3_activation)

    return PlanScaffold(
        reason=scaffold.reason,
        goal_outline=scaffold.goal_outline,
        intermediate_assets=compiled_intermediate,
        step_1_tasks=compiled_tasks,
        step_2_data_flow=compiled_df,
        step_3_activation=compiled_act,
        final_contracts=scaffold.final_contracts,
    )


# ---------------------------------------------------------------------------
# Scaffold → raw contract dicts (for integration with contracts_from_plan_asset)
# ---------------------------------------------------------------------------


def _scaffold_tasks_to_raw_dicts(scaffold: PlanScaffold) -> list[dict]:
    """Convert scaffold tasks into raw contract dicts suitable for the
    containment-check loop in ``contracts_from_plan_asset``.
    """
    df_map = {df.task_name: df for df in scaffold.step_2_data_flow}
    act_map = {a.task_name: a for a in scaffold.step_3_activation}

    raw_list: list[dict] = []
    for task in scaffold.step_1_tasks:
        df = df_map.get(task.name)
        act = act_map.get(task.name)

        raw_list.append(
            {
                "name": task.name,
                "description": task.description,
                "inputs": list(df.consumes) if df else list(task.consumes),
                "outputs": list(df.produces) if df else list(task.produces),
                "activation": act.expression if act else task.activation,
                "budget": task.budget,
                "tool_scope": list(task.tool_scope),
                "labels": list(task.labels),
            }
        )
    return raw_list


# ---------------------------------------------------------------------------
# contracts_from_scaffold — standalone derivation with containment
# ---------------------------------------------------------------------------


def contracts_from_scaffold(
    scaffold: PlanScaffold,
    parent_id: str | None,
    parent_contract: Contract | None = None,
) -> tuple[list, list[dict]]:
    """Derive concrete ``Contract`` objects from a validated scaffold.

    Builds raw contract dicts from scaffold tasks + data_flow + activation,
    then delegates to ``contracts_from_plan_asset`` for containment checks
    and ``Contract`` construction.

    Falls through to the legacy ``final_contracts`` path if the scaffold
    has no task definitions but contains ``final_contracts``.
    """
    from aigineering.plugins.task_semantics import contracts_from_plan_asset

    # If there are no scaffold tasks, fall through to legacy final_contracts
    if not scaffold.step_1_tasks:
        if scaffold.final_contracts:
            temp_content = json.dumps(
                {"contracts": deep_thaw(scaffold.final_contracts)}, sort_keys=True
            )
            temp_asset = _temp_asset(temp_content)
            return contracts_from_plan_asset(temp_asset, parent_id, parent_contract)
        return [], []

    # Build raw contract dicts from scaffold tasks
    raw_contracts = _scaffold_tasks_to_raw_dicts(scaffold)

    # Append legacy final_contracts if mixed format
    if scaffold.final_contracts:
        raw_contracts = raw_contracts + list(scaffold.final_contracts)

    temp_content = json.dumps({"contracts": raw_contracts}, sort_keys=True)
    temp_asset = _temp_asset(temp_content)
    return contracts_from_plan_asset(temp_asset, parent_id, parent_contract)


def _temp_asset(content: str) -> Asset:
    """Create a minimal temporary Asset for passing to contracts_from_plan_asset."""
    from aigineering.protocol.types import Asset as AssetT

    return AssetT(
        id="scaffold_temp",
        name="_plan_result_scaffold",
        content=content,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _string_list(value: object) -> list[str]:
    """Normalise a JSON list value to a list of strings."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _positive_int(value: object, default: int) -> int:
    """Parse a positive integer with fallback default."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _ensure_list(value: object) -> list:
    """Return value as a list if it is one, otherwise an empty list."""
    if isinstance(value, list):
        return value
    return []
