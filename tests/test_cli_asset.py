"""Tests for the CLI aig asset add/ls/show commands."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

import aigineering.cli.asset as asset_cli
from aigineering.cli.main import cli
from aigineering.core.control_plane import build_control_plane_asset
from aigineering.core.query_projection import StoreQueryProjection
from aigineering.core.sqlite_store import SQLiteStore
from conftest import candidate_runtime


@pytest.fixture(autouse=True)
def initialize_candidate_domain_before_asset_publication(monkeypatch):
    """Keep each CLI test focused while asset publication requires Genesis."""
    original = CliRunner.invoke

    def invoke(runner, command, args=None, *positional, **kwargs):
        effective = list(args or ())
        if (
            effective[:2] == ["asset", "add"]
            and not Path(".aig/identity/root.ed25519").exists()
        ):
            initialized = original(runner, command, ["domain", "init"])
            assert initialized.exit_code == 0, initialized.output
        return original(runner, command, args, *positional, **kwargs)

    monkeypatch.setattr(CliRunner, "invoke", invoke)


class TestAssetAdd:
    """Tests for aig asset add."""

    def test_add_inline_content_succeeds(self):
        """aig asset add --name foo --content "bar" succeeds."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                ["asset", "add", "--name", "foo", "--content", "bar"],
            )
            assert result.exit_code == 0, result.output
            assert "Asset injected: foo" in result.output

    def test_add_with_content_file_succeeds(self, tmp_path: Path):
        """aig asset add with --content-file reads from file."""
        content_file = tmp_path / "data.txt"
        content_file.write_text("file content")

        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                [
                    "asset",
                    "add",
                    "--name",
                    "from_file",
                    "--content-file",
                    str(content_file),
                ],
            )
            assert result.exit_code == 0, result.output
            assert "Asset injected: from_file" in result.output

    def test_add_with_content_json_succeeds(self, tmp_path: Path):
        """aig asset add with --content-json reads and validates JSON."""
        json_file = tmp_path / "data.json"
        json_file.write_text('{"key": "value"}')

        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                [
                    "asset",
                    "add",
                    "--name",
                    "json_asset",
                    "--content-json",
                    str(json_file),
                ],
            )
            assert result.exit_code == 0, result.output
            assert "Asset injected: json_asset" in result.output

    def test_add_with_invalid_content_json_fails(self, tmp_path: Path):
        """aig asset add with --content-json pointing to invalid JSON fails."""
        json_file = tmp_path / "bad.json"
        json_file.write_text("not json")

        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                [
                    "asset",
                    "add",
                    "--name",
                    "bad",
                    "--content-json",
                    str(json_file),
                ],
            )
            assert result.exit_code != 0
            assert "not valid JSON" in result.output

    def test_add_protected_name_rejected(self):
        """aig asset add --name _sys_secret is rejected (protected prefix)."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                ["asset", "add", "--name", "_sys_secret", "--content", "x"],
            )
            assert result.exit_code != 0
            assert "protected" in result.output.lower()

    def test_add_content_file_and_json_mutual_exclusion(self, tmp_path: Path):
        """--content-file and --content-json are mutually exclusive."""
        txt = tmp_path / "data.txt"
        txt.write_text("text")
        json_file = tmp_path / "data.json"
        json_file.write_text('{"a": 1}')

        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                [
                    "asset",
                    "add",
                    "--name",
                    "conflict",
                    "--content-file",
                    str(txt),
                    "--content-json",
                    str(json_file),
                ],
            )
            assert result.exit_code != 0
            assert "content-file" in result.output.lower()

    def test_add_content_and_content_file_mutual_exclusion(self, tmp_path: Path):
        """--content is mutually exclusive with --content-file."""
        txt = tmp_path / "data.txt"
        txt.write_text("text")

        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                [
                    "asset",
                    "add",
                    "--name",
                    "conflict",
                    "--content",
                    "inline",
                    "--content-file",
                    str(txt),
                ],
            )
            assert result.exit_code != 0

    def test_add_missing_content_fails(self):
        """asset add without any content source fails."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                ["asset", "add", "--name", "orphan"],
            )
            assert result.exit_code != 0

    def test_add_json_output(self):
        """aig asset add --json returns valid JSON."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                ["asset", "add", "--name", "foo", "--content", "bar", "--json"],
            )
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["name"] == "foo"
            assert "id" in data
            assert data["trust_tier"] == "human"

    def test_add_with_origin_and_trust_tier(self):
        """aig asset add accepts --origin and --trust-tier options."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                [
                    "asset",
                    "add",
                    "--name",
                    "trusted",
                    "--content",
                    "data",
                    "--origin",
                    "admin",
                    "--trust-tier",
                    "verified",
                ],
            )
            assert result.exit_code == 0, result.output
            assert "trust_tier: verified" in result.output

    def test_add_with_no_promptable(self):
        """aig asset add --no-promptable marks asset as not promptable."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                [
                    "asset",
                    "add",
                    "--name",
                    "secret",
                    "--content",
                    "secret_data",
                    "--no-promptable",
                ],
            )
            assert result.exit_code == 0, result.output
            # Verify via show --json
            result2 = runner.invoke(
                cli,
                ["asset", "show", "secret", "--json"],
            )
            assert result2.exit_code == 0
            data = json.loads(result2.output)
            assert data["promptable"] is False


class TestAssetList:
    """Tests for aig asset ls."""

    def test_list_empty(self):
        """aig asset ls on an empty store shows no assets."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            assert runner.invoke(cli, ["domain", "init"]).exit_code == 0
            result = runner.invoke(cli, ["asset", "ls"])
            assert result.exit_code == 0
            assert "No assets found" in result.output

    def test_list_after_add(self):
        """aig asset ls shows injected assets."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["asset", "add", "--name", "alpha", "--content", "a"])
            runner.invoke(cli, ["asset", "add", "--name", "beta", "--content", "b"])
            result = runner.invoke(cli, ["asset", "ls"])
            assert result.exit_code == 0
            assert "alpha" in result.output
            assert "beta" in result.output

    def test_list_json_output(self):
        """aig asset ls --json returns valid JSON array."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["asset", "add", "--name", "x", "--content", "y"])
            result = runner.invoke(cli, ["asset", "ls", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert isinstance(data, list)
            assert len(data) == 1
            assert data[0]["name"] == "x"


class TestAssetShow:
    """Tests for aig asset show."""

    def test_show_existing_asset(self):
        """aig asset show <name> displays metadata and content."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["asset", "add", "--name", "foo", "--content", "bar"])
            result = runner.invoke(cli, ["asset", "show", "foo"])
            assert result.exit_code == 0
            assert "name:            foo" in result.output
            assert "--- content ---" in result.output
            assert "bar" in result.output

    def test_show_nonexistent_asset_fails(self):
        """aig asset show <missing> fails with an error."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            assert runner.invoke(cli, ["domain", "init"]).exit_code == 0
            result = runner.invoke(cli, ["asset", "show", "nope"])
            assert result.exit_code != 0
            assert "No asset named" in result.output

    def test_show_json_output(self):
        """aig asset show <name> --json returns valid JSON."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["asset", "add", "--name", "foo", "--content", "bar"])
            result = runner.invoke(cli, ["asset", "show", "foo", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["name"] == "foo"
            assert data["content"] == "bar"
            assert data["content_type"] == "text"
            assert data["origin"] == "human"
            assert "id" in data
            assert "definition_hash" in data
            assert "content_hash" in data
            assert "signed_by" in data
            assert "promptable" in data

    def test_show_json_is_valid_json(self):
        """aig asset show --json output is parseable as valid JSON."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["asset", "add", "--name", "foo", "--content", "bar"])
            result = runner.invoke(cli, ["asset", "show", "foo", "--json"])
            assert result.exit_code == 0
            json.loads(result.output)  # does not raise

    def _use_store(self, monkeypatch, store):
        monkeypatch.setattr(asset_cli, "_persistent_store", lambda: store)
        monkeypatch.setattr(
            asset_cli, "_query_projection", lambda _store: StoreQueryProjection(_store)
        )
        return store

    def test_show_exact_id_selects_each_same_name_asset(self, monkeypatch, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem():
            store = SQLiteStore(tmp_path / "store.db")
            runtime = candidate_runtime(store)
            first_asset = build_control_plane_asset(name="report", content="original")
            second_asset = build_control_plane_asset(name="report", content="revision")
            first_asset = runtime.accept_asset(first_asset)
            second_asset = runtime.accept_asset(second_asset)
            self._use_store(monkeypatch, store)
            first_id = first_asset.id
            second_id = second_asset.id
            assert first_id != second_id

            for asset_id, expected in ((first_id, "original"), (second_id, "revision")):
                result = runner.invoke(
                    asset_cli.asset_show, ["--id", asset_id, "--json"]
                )
                assert result.exit_code == 0, result.output
                data = json.loads(result.output)
                assert data["id"] == asset_id
                assert data["name"] == "report"
                assert data["content"] == expected

            # The name selector remains backward-compatible with its existing
            # first ID-sorted match behavior.
            listed = StoreQueryProjection(store).get_assets_by_name("report")
            assert [asset.id for asset in listed] == sorted((first_id, second_id))
            legacy = runner.invoke(asset_cli.asset_show, ["report", "--json"])
            assert legacy.exit_code == 0, legacy.output
            assert json.loads(legacy.output)["id"] == listed[0].id

    def test_show_unknown_exact_id_does_not_fall_back_to_name(
        self, monkeypatch, tmp_path
    ):
        runner = CliRunner()
        with runner.isolated_filesystem():
            store = SQLiteStore(tmp_path / "store.db")
            runtime = candidate_runtime(store)
            runtime.accept_asset(
                build_control_plane_asset(name="report", content="must not leak")
            )
            self._use_store(monkeypatch, store)
            result = runner.invoke(
                asset_cli.asset_show, ["--id", "asset:missing", "--json"]
            )
            assert result.exit_code != 0
            assert "No asset with ID 'asset:missing'" in result.output
            assert "must not leak" not in result.output

    @pytest.mark.parametrize(
        "args",
        [
            ["asset", "show"],
            ["asset", "show", "report", "--id", "asset:some-id"],
            ["asset", "show", "--id", ""],
        ],
    )
    def test_show_invalid_selector_fails_before_opening_store(self, monkeypatch, args):
        def fail_if_opened():
            raise AssertionError("selector validation must precede Store setup")

        monkeypatch.setattr(asset_cli, "_persistent_store", fail_if_opened)
        result = CliRunner().invoke(asset_cli.asset_show, args[2:])
        assert result.exit_code != 0
        assert (
            "provide exactly one" in result.output
            or "non-empty Asset ID" in result.output
        )

    def test_show_by_id_preserves_operator_content_and_does_not_write_facts(
        self, monkeypatch, tmp_path
    ):
        runner = CliRunner()
        with runner.isolated_filesystem():
            store = SQLiteStore(tmp_path / "store.db")
            runtime = candidate_runtime(store)
            asset = runtime.accept_asset(
                build_control_plane_asset(
                    name="local-only",
                    content="local-only content",
                    promptable=False,
                )
            )
            self._use_store(monkeypatch, store)
            asset_id = asset.id
            before_revision = store.get_runtime_revision()
            before_records = tuple(
                record.id for _, record in store.scan_runtime_records()
            )
            before_digest = store.runtime_materialization_digest()
            before_contracts = tuple(
                contract.id for contract in store.get_all_contracts()
            )

            result = runner.invoke(asset_cli.asset_show, ["--id", asset_id, "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["id"] == asset_id
            assert data["promptable"] is False
            assert data["content"] == "local-only content"
            assert data["disclosure_view"] == "original"

            after = SQLiteStore.snapshot_from_path(tmp_path / "store.db")
            assert after.get_runtime_revision() == before_revision
            assert (
                tuple(record.id for _, record in after.scan_runtime_records())
                == before_records
            )
            assert after.runtime_materialization_digest() == before_digest
            assert (
                tuple(contract.id for contract in after.get_all_contracts())
                == before_contracts
            )

    @pytest.mark.parametrize("disclosure_view", ["redacted", "transformed"])
    def test_show_redacts_non_original_body_for_name_and_id(
        self, monkeypatch, tmp_path, disclosure_view
    ):
        runner = CliRunner()
        with runner.isolated_filesystem():
            store = SQLiteStore(tmp_path / "store.db")
            runtime = candidate_runtime(store)
            original = build_control_plane_asset(
                name="sealed-review", content=f"private-{disclosure_view}-marker"
            )
            stored = runtime.accept_asset(
                replace(original, disclosure_view=disclosure_view)
            )
            self._use_store(monkeypatch, store)
            monkeypatch.setattr(
                "aigineering.cli.infrastructure.require_redis", lambda: None
            )
            assert asset_cli._persistent_store() is store
            assert isinstance(asset_cli._query_projection(store), StoreQueryProjection)
            before_revision = store.get_runtime_revision()
            before = store.get_asset(stored.id)

            for selector in (["--id", stored.id], ["sealed-review"]):
                for json_output in (False, True):
                    callback_args = (
                        {"name": "sealed-review", "asset_id": None}
                        if selector[0] == "sealed-review"
                        else {"name": None, "asset_id": stored.id}
                    )
                    with runner.isolation() as (stdout, _stderr, _):
                        asset_cli.asset_show.callback.__wrapped__(
                            **callback_args, as_json=json_output
                        )
                    output = stdout.getvalue().decode()
                    assert f"private-{disclosure_view}-marker" not in output
                    assert "[redacted]" in output
                    if json_output:
                        data = json.loads(output)
                        assert data["disclosure_view"] == disclosure_view
                        assert data["content"] == "[redacted]"

            after = store.get_asset(stored.id)
            assert store.get_runtime_revision() == before_revision
            assert after == before
            assert after.content == f"private-{disclosure_view}-marker"


class TestAssetVersionWorkflow:
    """Tests for asset slice / replacement / versions / lineage workflows."""

    def test_slice_lines_range_extracts_subset(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                [
                    "asset",
                    "add",
                    "--name",
                    "doc",
                    "--content",
                    "one\ntwo\nthree\nfour\n",
                ],
            )
            assert result.exit_code == 0, result.output

            result = runner.invoke(
                cli,
                [
                    "asset",
                    "slice",
                    "doc",
                    "--slice-name",
                    "doc.middle",
                    "--range",
                    "lines:2-3",
                    "--json",
                ],
            )
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["name"] == "doc.middle"

            show = runner.invoke(cli, ["asset", "show", "doc.middle", "--json"])
            assert show.exit_code == 0, show.output
            sliced = json.loads(show.output)
            assert sliced["content"] == "two\nthree\n"

            store = SQLiteStore(".aig/store.db")
            committed = store.get_by_event_type("candidate_committed")
            assert any("doc.middle" in e.accepted_asset_names for e in committed)
            assert (
                store.get_assets_by_name("doc.middle")[0].lineage_id
                == data["lineage_id"]
            )

    def test_slice_invalid_range_fails_closed(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["asset", "add", "--name", "doc", "--content", "abc"])
            result = runner.invoke(
                cli,
                [
                    "asset",
                    "slice",
                    "doc",
                    "--slice-name",
                    "bad",
                    "--range",
                    "bytes:1-2",
                ],
            )
            assert result.exit_code != 0
            assert "range_spec" in result.output or "range" in result.output

    def test_replacement_claim_verifies(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            a = runner.invoke(
                cli,
                ["asset", "add", "--name", "report", "--content", "v1", "--json"],
            )
            b = runner.invoke(
                cli,
                ["asset", "add", "--name", "report", "--content", "v2", "--json"],
            )
            assert a.exit_code == 0, a.output
            assert b.exit_code == 0, b.output
            source_id = json.loads(a.output)["id"]
            replacement_id = json.loads(b.output)["id"]

            claim = runner.invoke(
                cli,
                ["asset", "replace", source_id, replacement_id, "--json"],
            )
            assert claim.exit_code == 0, claim.output

            verify = runner.invoke(cli, ["verify", "replacements", "--json"])
            assert verify.exit_code == 0, verify.output
            result = json.loads(verify.output)
            assert result["pass_count"] == 1
            assert result["fail_count"] == 0

            store = SQLiteStore(".aig/store.db")
            claims = store.get_claims_for_asset(source_id)
            assert len(claims) == 1
            assert claims[0].replacement_asset_id == replacement_id
            assert claims[0].signed_by == "human:owner"
            assert (
                len(store.scan_runtime_records(record_type="replacement.claimed")) == 1
            )
