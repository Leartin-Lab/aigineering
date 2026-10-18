"""Read-only verified task-lineage command."""

from __future__ import annotations

import click

from aigineering.application import collaboration_service
from aigineering.cli._common import _output_json, _persistent_snapshot
from aigineering.redis_collaboration import RedisCollaborationUnavailable
from aigineering.task_lineage import TaskLineageError, task_lineage


def register_task_lineage_command(group: click.Group) -> None:
    @group.command(
        "lineage",
        help=(
            "Show a bidirectional component for containment, Worker-failure "
            "recovery, supported method recovery, retries, handoffs and revisions. "
            "Method recovery sources are authenticated invocation failures, "
            "claim expirations and admitted output rejections; legacy generic "
            "recovery keeps its existing source-proof limit. Unavailable "
            "schedules are observations. The view does not pair a failure "
            "with a publication or prove tool-observation causation. Starting "
            "at either endpoint returns the same component. It also follows "
            "signed retry publications, handoff publications and revision "
            "publications. See changes 098 "
            "and 099."
        ),
    )
    @click.argument("contract_id")
    @click.option("--json", "as_json", is_flag=True, help="Output a JSON envelope.")
    def lineage(contract_id: str, as_json: bool) -> None:
        """Show verified containment, Worker-failure and supported method recovery, retry, handoff and revision links.

        Method recovery sources include authenticated invocation failure, claim
        expiration and admitted output rejection. Legacy generic recovery retains
        its existing source-proof limit. Unavailable schedules are observations;
        this view does not pair failures with publications or prove tool-observation
        causation. Starting at either endpoint returns the same component.
        """
        store = None
        try:
            store = _persistent_snapshot()
            collaboration_service(store).sync()
            result = task_lineage(store, contract_id)
        except RedisCollaborationUnavailable:
            _error("redis_unavailable", as_json)
        except TaskLineageError as exc:
            _error(exc.code, as_json)
        except Exception:
            _error("lineage_unavailable", as_json)
        finally:
            if store is not None:
                store.close()
        if as_json:
            _output_json(result)
            return
        data = result["data"]
        click.echo(
            f"Task lineage for {data['selected_contract_id']} "
            f"(revision {data['revision']})"
        )
        click.echo("covered relations: " + ", ".join(data["covered_relations"]))
        for card in data["contracts"]:
            task = card["task"]
            click.echo(f"{card['contract_id']}  {task['status']}  {card['name']}")
            for blocker in task.get("blockers", ()):
                click.echo(f"  blocker: {blocker}")
        for edge in data["edges"]:
            if edge["kind"] == "retry_publication":
                click.echo(
                    f"{edge['kind']}: {edge['source_contract_id']} -> "
                    f"{edge['target_contract_id']} "
                    f"source_receipt={edge['source_receipt_id']} "
                    f"source_declaration={edge['source_declaration_id']} "
                    f"source_head={edge['source_head_id']} "
                    f"target_receipt={edge['target_receipt_id']} "
                    f"target_declaration={edge['target_declaration_id']} "
                    f"target_head={edge['target_head_id']} "
                    f"candidate={edge['candidate_id']}"
                )
            elif edge["kind"] == "handoff_publication":
                click.echo(
                    f"{edge['kind']}: {edge['source_contract_id']} -> "
                    f"{edge['target_contract_id']} "
                    f"source_declaration={edge['source_declaration_id']} "
                    f"source_receipt={edge['source_receipt_id']} "
                    f"source_head={edge['source_head_id']} "
                    f"handoff_link={edge['handoff_link_id']} "
                    f"handoff_receipt={edge['handoff_receipt_id']} "
                    f"handoff_head={edge['handoff_head_id']} "
                    f"cancelled_terminal={edge['cancelled_terminal_id']} "
                    f"target_declaration={edge['target_declaration_id']} "
                    f"target_receipt={edge['target_receipt_id']} "
                    f"target_head={edge['target_head_id']}"
                )
            elif edge["kind"] == "revision_publication":
                click.echo(
                    f"{edge['kind']}: {edge['source_contract_id']} -> "
                    f"{edge['target_contract_id']} "
                    f"source_declaration={edge['source_declaration_id']} "
                    f"source_receipt={edge['source_receipt_id']} "
                    f"source_head={edge['source_head_id']} "
                    f"revision_link={edge['revision_link_id']} "
                    f"revision_receipt={edge['revision_receipt_id']} "
                    f"revision_head={edge['revision_head_id']} "
                    f"cancelled_terminal={edge['cancelled_terminal_id']} "
                    f"target_declaration={edge['target_declaration_id']} "
                    f"target_receipt={edge['target_receipt_id']} "
                    f"target_head={edge['target_head_id']}"
                )
            else:
                click.echo(
                    f"{edge['kind']}: {edge['source_contract_id']} -> "
                    f"{edge['target_contract_id']} "
                    f"receipt={edge.get('receipt_id', 'n/a')} "
                    f"declaration={edge.get('declaration_id', 'n/a')} "
                    f"head={edge.get('head_id', 'n/a')}"
                )
        for issue in data["issues"]:
            reference = (
                ",".join(
                    value
                    for value in (
                        issue.get("contract_id"),
                        issue.get("source_contract_id"),
                        issue.get("target_contract_id"),
                        issue.get("parent_contract_id"),
                        issue.get("record_id"),
                    )
                    if value
                )
                or "domain"
            )
            click.echo(f"unverified relation ({issue['code']}): {reference}")
        for diagnostic in data["source_diagnostics"]:
            for failure in diagnostic.get("invocation_failures", ()):
                click.echo(
                    "source invocation failure: "
                    f"{failure['contract_id']} {failure['failure_record_id']} "
                    f"({failure['category']})"
                )
            for schedule in diagnostic.get("schedule_records", ()):
                click.echo(
                    "verified source recovery schedule: "
                    f"{diagnostic['contract_id']} {schedule['record_id']}"
                )
            for observation in diagnostic.get("unverified_schedule_observations", ()):
                click.echo(
                    "unverified schedule observation: "
                    f"{diagnostic['contract_id']} {observation['record_id']}"
                )


def _error(code: str, as_json: bool) -> None:
    if as_json:
        _output_json({"ok": False, "error": code})
    else:
        click.echo(f"Task lineage unavailable ({code})", err=True)
    raise click.exceptions.Exit(1)
