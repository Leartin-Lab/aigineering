"""Local heterogeneous Worker fleet commands."""

from __future__ import annotations

import click

from aigineering.agent.tool_registry_loader import load_tool_registry
from aigineering.cli._common import _output_json
from aigineering.cli.run import _publish_tool_descriptors
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.fleet_config import build_fleet_worker, load_fleet_config
from aigineering.local_fleet import FleetHost, run_local_fleet
from aigineering.local_identity import (
    ensure_local_runtime_publishers,
    ensure_local_worker_host,
)


@click.group("fleet")
def fleet_group() -> None:
    """Run multiple capability-routed local Workers."""


@fleet_group.command("run")
@click.option(
    "--config",
    "config_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="TOML Worker fleet configuration.",
)
@click.option("--task", "target_contract_id", required=True, help="Root task ID.")
@click.option(
    "--wait-timeout",
    type=float,
    default=300.0,
    show_default=True,
    help="Maximum seconds to run the bounded fleet.",
)
@click.option("--json", "as_json", is_flag=True, help="Output JSON.")
def fleet_run(
    config_path: str,
    target_contract_id: str,
    wait_timeout: float,
    as_json: bool,
) -> None:
    """Run a local fleet through independent pull/claim/submit loops."""
    try:
        config = load_fleet_config(config_path)
        store = SQLiteStore(config.db_path)
        try:
            ensure_local_runtime_publishers(store)
            fleet_hosts: list[FleetHost] = []
            published_registries: set[str] = set()
            for spec in config.workers:
                if (
                    spec.tool_registry
                    and spec.tool_registry not in published_registries
                ):
                    registry = load_tool_registry(spec.tool_registry)
                    _publish_tool_descriptors(store, registry)
                    published_registries.add(spec.tool_registry)
                worker = build_fleet_worker(spec)
                host = ensure_local_worker_host(
                    store,
                    worker,
                    effect_capabilities=spec.effect_capabilities,
                )
                fleet_hosts.append(FleetHost(host=host, capacity=spec.capacity))
        finally:
            store.close()

        result = run_local_fleet(
            config.db_path,
            tuple(fleet_hosts),
            target_contract_id=target_contract_id,
            timeout=wait_timeout,
            poll_interval=config.poll_interval,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    payload = {
        "contract_id": result.contract_id,
        "status": result.status,
        "completed": result.completed,
        "timed_out": result.timed_out,
        "worker_errors": list(result.worker_errors),
    }
    if as_json:
        _output_json(payload)
    else:
        click.echo(f"fleet {result.status}: {result.contract_id}")
        for error in result.worker_errors:
            click.echo(f"worker recovery: {error}")
    if not result.completed:
        raise click.exceptions.Exit(1)
