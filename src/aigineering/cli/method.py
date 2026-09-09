"""CLI for reusable method packages and deterministic method evaluation."""

from __future__ import annotations

from pathlib import Path

import click

from aigineering.cli._candidate import commit_local_effects, require_accepted
from aigineering.cli._common import _output_json, _persistent_store
from aigineering.local_identity import ensure_local_plugin_publisher
from aigineering.methods.packages import visible_asset
from aigineering.methods.schema import MAX_PACKAGE_BYTES
from aigineering.methods.service import MethodService


def _read_bounded(path: str) -> str:
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_PACKAGE_BYTES + 1)
    except OSError as exc:
        raise click.ClickException(str(exc)) from exc
    if len(data) > MAX_PACKAGE_BYTES:
        raise click.ClickException("input file exceeds 256 KiB")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise click.ClickException("input file is not valid UTF-8") from exc


def _bindings(values: tuple[str, ...], option: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        slot, separator, bound = value.partition("=")
        if not separator or not slot or not bound:
            raise click.ClickException(f"{option} must use SLOT=VALUE")
        if slot in result:
            raise click.ClickException(f"duplicate {option} binding for slot {slot!r}")
        result[slot] = bound
    return result


def _emit(value: dict, as_json: bool) -> None:
    if as_json:
        _output_json(value)
    else:
        for key, item in value.items():
            click.echo(f"{key}: {item}")


def _service(store) -> MethodService:
    return MethodService(
        store,
        lambda effects, idempotency_key: commit_local_effects(
            store, effects, idempotency_key=idempotency_key
        ),
    )


@click.group("method")
def method_group() -> None:
    """Import, instantiate, test, and assess reusable methods."""


@method_group.command("import")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.option("--name", default=None)
@click.option("--json", "as_json", is_flag=True)
def method_import(file: str, name: str | None, as_json: bool) -> None:
    """Import one method package JSON file."""
    store = _persistent_store()
    try:
        asset = _service(store).import_package(_read_bounded(file), name=name)
        _emit({"asset_id": asset.id, "name": asset.name}, as_json)
    except (LookupError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        store.close()


@method_group.command("cases")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.option("--name", required=True)
@click.option("--json", "as_json", is_flag=True)
def method_cases(file: str, name: str, as_json: bool) -> None:
    """Import one method test suite JSON file."""
    store = _persistent_store()
    try:
        asset = _service(store).import_cases(_read_bounded(file), name)
        _emit({"asset_id": asset.id, "name": asset.name}, as_json)
    except (LookupError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        store.close()


@method_group.command("show")
@click.argument("asset_id")
@click.option("--json", "as_json", is_flag=True)
def method_show(asset_id: str, as_json: bool) -> None:
    """Show one exact method Asset."""
    store = _persistent_store()
    try:
        asset = visible_asset(asset_id, store.get_asset)
        _emit(
            {
                "asset_id": asset.id,
                "name": asset.name,
                "content_type": asset.content_type,
                "content": asset.content,
                "origin": asset.origin,
                "trust_tier": asset.trust_tier,
            },
            as_json,
        )
    except (LookupError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        store.close()


@method_group.command("instantiate")
@click.argument("method_id")
@click.option("--input", "inputs", multiple=True)
@click.option("--output", "outputs", multiple=True)
@click.option("--name", required=True)
@click.option("--budget", type=int, required=True)
@click.option("--allow-tool", "allowed_tools", multiple=True)
@click.option("--evaluation", "evaluation_id", default=None)
@click.option("--allow-unverified", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def method_instantiate(
    method_id: str,
    inputs: tuple[str, ...],
    outputs: tuple[str, ...],
    name: str,
    budget: int,
    allowed_tools: tuple[str, ...],
    evaluation_id: str | None,
    allow_unverified: bool,
    as_json: bool,
) -> None:
    """Create an ordinary Contract from a method Asset."""
    store = _persistent_store()
    try:
        contract = _service(store).instantiate(
            method_id,
            inputs=_bindings(inputs, "--input"),
            outputs=_bindings(outputs, "--output"),
            name=name,
            budget=budget,
            allowed_tools=allowed_tools,
            evaluation_id=evaluation_id,
            allow_unverified=allow_unverified,
        )
        _emit({"contract_id": contract.id, "name": contract.name}, as_json)
    except (LookupError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        store.close()


@method_group.command("create")
@click.option("--request", "request_id", required=True)
@click.option("--output", required=True)
@click.option("--name", required=True)
@click.option("--budget", type=int, required=True)
@click.option("--context", "context_ids", multiple=True)
@click.option("--json", "as_json", is_flag=True)
def method_create(
    request_id: str,
    output: str,
    name: str,
    budget: int,
    context_ids: tuple[str, ...],
    as_json: bool,
) -> None:
    """Create an ordinary Contract for a request."""
    store = _persistent_store()
    try:
        contract = _service(store).create(
            request_id, output=output, name=name, budget=budget, context=context_ids
        )
        _emit({"contract_id": contract.id, "name": contract.name}, as_json)
    except (LookupError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        store.close()


@method_group.command("test")
@click.argument("method_id")
@click.option("--cases", "suite_id", required=True)
@click.option("--name", required=True)
@click.option("--budget", type=int, required=True)
@click.option("--allow-tool", "allowed_tools", multiple=True)
@click.option("--json", "as_json", is_flag=True)
def method_test(
    method_id: str,
    suite_id: str,
    name: str,
    budget: int,
    allowed_tools: tuple[str, ...],
    as_json: bool,
) -> None:
    """Create an ordinary method test run Contract."""
    store = _persistent_store()
    try:
        run_manifest, root = _service(store).prepare_tests(
            method_id,
            suite_id,
            name=name,
            budget=budget,
            allowed_tools=allowed_tools,
        )
        _emit(
            {"run_id": run_manifest.id, "contract_id": root.id, "name": root.name},
            as_json,
        )
    except (LookupError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        store.close()


@method_group.command("assess")
@click.argument("run_id")
@click.option("--json", "as_json", is_flag=True)
def method_assess(run_id: str, as_json: bool) -> None:
    """Create a deterministic evaluation Asset and acceptance Contract."""
    store = _persistent_store()
    try:
        evaluation, contract = _service(store).assess(run_id)
        _emit({"evaluation_id": evaluation.id, "contract_id": contract.id}, as_json)
    except (LookupError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        store.close()


@method_group.command("attest")
@click.argument("evaluation_id")
@click.option(
    "--verdict", type=click.Choice(("accepted", "rejected")), default="accepted"
)
@click.option("--json", "as_json", is_flag=True)
def method_attest(evaluation_id: str, verdict: str, as_json: bool) -> None:
    """Publish a signed method review attestation."""
    store = _persistent_store()
    try:
        effect = _service(store).attestation_effect(evaluation_id, verdict=verdict)
        publisher = ensure_local_plugin_publisher(
            store, "method.review.v1", ("asset.attest", "method.verify")
        )
        decision = require_accepted(
            publisher.publish(
                (effect,),
                idempotency_key=f"method:attest:{evaluation_id}:{verdict}",
                causal_parents=(evaluation_id,),
            )
        )
        _emit(
            {
                "evaluation_id": evaluation_id,
                "verdict": verdict,
                "accepted": decision.accepted,
            },
            as_json,
        )
    except (LookupError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        store.close()


@method_group.command("inspect")
@click.argument("method_id")
@click.option("--json", "as_json", is_flag=True)
def method_inspect(method_id: str, as_json: bool) -> None:
    """Inspect the parsed method package and exact dependencies."""
    store = _persistent_store()
    try:
        value = _service(store).inspect(method_id)
        if not isinstance(value, dict):
            raise ValueError("method inspection must return an object")
        _emit(value, as_json)
    except (LookupError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        store.close()
