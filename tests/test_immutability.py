"""Every immutable protocol collection rejects in-place mutation."""

import pytest

from aigineering.protocol.types import (
    Asset,
    Candidate,
    Contract,
    ProjectionResult,
    RejectedCandidate,
    Session,
    ToolSpec,
    TraceEntry,
)


class TestContractImmutability:
    """Contract fields must reject in-place mutation."""

    def test_inputs_cannot_be_mutated(self):
        c = Contract(
            id="c1",
            name="test",
            inputs=["a", "b"],
            outputs=["x"],
            tool_scope=["read"],
            labels=["l1"],
        )
        with pytest.raises((AttributeError, TypeError)):
            c.inputs.append("hacked")  # type: ignore[union-attr]

    def test_outputs_cannot_be_mutated(self):
        c = Contract(id="c1", outputs=["x"])
        with pytest.raises((AttributeError, TypeError)):
            c.outputs.append("undeclared")  # type: ignore[union-attr]

    def test_tool_scope_cannot_be_mutated(self):
        c = Contract(id="c1", tool_scope=["read"])
        with pytest.raises((AttributeError, TypeError)):
            c.tool_scope.append("write")  # type: ignore[union-attr]

    def test_labels_cannot_be_mutated(self):
        c = Contract(id="c1", labels=["l1"])
        with pytest.raises((AttributeError, TypeError)):
            c.labels.append("admin")  # type: ignore[union-attr]

    def test_contract_fields_are_hashable(self):
        """Tuples (but not lists) support hashing — confirms conversion."""
        c = Contract(
            id="c1", inputs=["a"], outputs=["b"], tool_scope=["read"], labels=["l1"]
        )
        # If inputs/outputs are tuples, this works; if lists, TypeError
        try:
            hash(c.inputs)
            hash(c.outputs)
            hash(c.tool_scope)
            hash(c.labels)
        except TypeError:
            pytest.fail("Contract fields must be hashable (tuples, not lists)")


class TestTraceEntryImmutability:
    """TraceEntry fragment lists must reject in-place mutation."""

    def test_accepted_fragments_cannot_be_mutated(self):
        t = TraceEntry(
            id="e1",
            contract_id="c1",
            event_type="projection",
            accepted_fragments=["a1"],
            rejected_fragments=["r1"],
        )
        with pytest.raises((AttributeError, TypeError)):
            t.accepted_fragments.append("added_after_fact")  # type: ignore[union-attr]

    def test_rejected_fragments_cannot_be_mutated(self):
        t = TraceEntry(
            id="e1",
            contract_id="c1",
            event_type="projection",
            rejected_fragments=["r1"],
        )
        with pytest.raises((AttributeError, TypeError)):
            t.rejected_fragments.append("modified_rejection")  # type: ignore[union-attr]

    def test_disclosed_assets_cannot_be_mutated(self):
        t = TraceEntry(
            id="e1",
            contract_id="c1",
            event_type="disclosure",
            disclosed_assets=["a1"],
        )
        with pytest.raises((AttributeError, TypeError)):
            t.disclosed_assets.append("leaked_asset")  # type: ignore[union-attr]

    def test_accepted_asset_names_cannot_be_mutated(self):
        t = TraceEntry(
            id="e1",
            contract_id="c1",
            event_type="projection",
            accepted_asset_names=["report"],
        )
        with pytest.raises((AttributeError, TypeError)):
            t.accepted_asset_names.append("injected_name")  # type: ignore[union-attr]


class TestSessionImmutability:
    """Session ID lists and snapshots must reject in-place mutation."""

    def test_contract_ids_cannot_be_mutated(self):
        s = Session(id="s1", contract_ids=["c1"])
        with pytest.raises((AttributeError, TypeError)):
            s.contract_ids.append("c2")  # type: ignore[union-attr]

    def test_asset_ids_cannot_be_mutated(self):
        s = Session(id="s1", asset_ids=["a1"])
        with pytest.raises((AttributeError, TypeError)):
            s.asset_ids.append("a2")  # type: ignore[union-attr]

    def test_trace_ids_cannot_be_mutated(self):
        s = Session(id="s1", trace_ids=["t1"])
        with pytest.raises((AttributeError, TypeError)):
            s.trace_ids.append("t2")  # type: ignore[union-attr]

    def test_config_snapshot_cannot_be_mutated(self):
        s = Session(id="s1", config_snapshot={"key": "val"})
        with pytest.raises((AttributeError, TypeError)):
            s.config_snapshot["new_key"] = "injected"  # type: ignore[index]

    def test_worker_snapshot_cannot_be_mutated(self):
        s = Session(id="s1", worker_snapshot={"kind": "mock"})
        with pytest.raises((AttributeError, TypeError)):
            s.worker_snapshot["kind"] = "hijacked"  # type: ignore[index]


class TestProjectionResultImmutability:
    """ProjectionResult asset lists must reject in-place mutation."""

    def test_accepted_assets_cannot_be_mutated(self):
        r = ProjectionResult(accepted_assets=[])
        with pytest.raises((AttributeError, TypeError)):
            r.accepted_assets.append(  # type: ignore[union-attr]
                Asset(id="injected", name="hack", content="evil")
            )

    def test_rejected_candidates_cannot_be_mutated(self):
        r = ProjectionResult(rejected_candidates=[])
        with pytest.raises((AttributeError, TypeError)):
            r.rejected_candidates.append(  # type: ignore[union-attr]
                RejectedCandidate(
                    name="fabricated", content="fake", reject_reason="none"
                )
            )

    def test_authority_policy_cannot_be_mutated(self):
        r = ProjectionResult(authority_policy={"key": "val"})
        with pytest.raises((AttributeError, TypeError)):
            r.authority_policy["injected"] = "bypass"  # type: ignore[index]


class TestToolSpecImmutability:
    """ToolSpec input_schema must reject in-place mutation."""

    def test_input_schema_cannot_be_mutated(self):
        ts = ToolSpec(name="read", input_schema={"type": "object"})
        with pytest.raises((AttributeError, TypeError)):
            ts.input_schema["extra"] = "injected_field"  # type: ignore[index]


class TestCandidateImmutability:
    """Candidate parsed_action must reject in-place mutation."""

    def test_parsed_action_cannot_be_mutated(self):
        c = Candidate(
            worker_id="w1", raw_output="hello", parsed_action={"type": "exec"}
        )
        with pytest.raises((AttributeError, TypeError)):
            c.parsed_action["type"] = "hijack"  # type: ignore[index]
