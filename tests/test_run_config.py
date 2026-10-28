from __future__ import annotations

from pathlib import Path

import pytest

from aigineering.run_config import prepare_configured_run


@pytest.mark.parametrize(
    "example",
    ["ai4s", "report-consistency", "claim-evidence-review", "guarded-claim-review"],
)
def test_checked_in_run_presets_prepare_without_store_side_effects(
    tmp_path, monkeypatch, example
):
    monkeypatch.chdir(tmp_path)
    preset = Path(__file__).parents[1] / "examples" / example / "run.toml"
    monkeypatch.syspath_prepend(str(preset.parents[2]))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-placeholder-never-used")
    prepared = prepare_configured_run(preset)

    assert prepared.contract is not None
    assert prepared.contract.name
    assert prepared.input_assets
    assert prepared.workers
    assert not (tmp_path / ".aig").exists()


def test_invalid_input_fails_before_store_or_identity_creation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "workers.toml").write_text(
        '[fleet]\ndb_path=".aig/store.db"\n[[workers]]\nid="w"\nkind="llm"\nmodel="offline"\n',
        encoding="utf-8",
    )
    (tmp_path / "bad.txt").write_bytes(b"\xff")
    (tmp_path / "run.toml").write_text(
        '[run]\nfleet="workers.toml"\n[[inputs]]\nname="source"\npath="bad.txt"\n'
        '[task]\nname="root"\ninputs=["source"]\noutputs=["result"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="UTF-8"):
        prepare_configured_run("run.toml")
    assert not (tmp_path / ".aig").exists()


def test_missing_skill_label_fails_during_preparation(tmp_path):
    (tmp_path / "workers.toml").write_text(
        '[fleet]\ndb_path="store.db"\n[[workers]]\nid="w"\nkind="llm"\nmodel="offline"\n',
        encoding="utf-8",
    )
    (tmp_path / "input.txt").write_text("source", encoding="utf-8")
    (tmp_path / "run.toml").write_text(
        '[run]\nfleet="workers.toml"\n[[inputs]]\nname="source"\npath="input.txt"\n'
        '[task]\nname="root"\ninputs=["source"]\noutputs=["result"]\nlabels=["missing_skill"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="do not resolve"):
        prepare_configured_run(tmp_path / "run.toml")


def test_response_format_is_strictly_parsed(tmp_path):
    (tmp_path / "workers.toml").write_text(
        '[fleet]\ndb_path="store.db"\n[[workers]]\nid="w"\nkind="llm"\nmodel="offline"\nresponse_format="bogus"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="response_format"):
        prepare_configured_run(tmp_path / "workers.toml", resume_task_id="task-id")


@pytest.mark.parametrize(
    ("task", "message"),
    [
        ('inputs=["source"]\noutputs=["result"]', "task.name"),
        (
            'name="root"\ninputs=["source"]\noutputs=["result"]\nactivation=4',
            "activation",
        ),
        ('name="root"\ninputs=["source"]\noutputs=[]', "outputs"),
        ('name="root"\ninputs=["source", "source"]\noutputs=["result"]', "duplicates"),
        (
            'name="root"\ninputs=["source"]\noutputs=["result"]\nactivation="source AND"',
            "activation",
        ),
    ],
)
def test_invalid_root_fields_fail_before_database_creation(tmp_path, task, message):
    (tmp_path / "workers.toml").write_text(
        '[fleet]\ndb_path=".aig/store.db"\n[[workers]]\nid="w"\nkind="llm"\nmodel="offline"\n',
        encoding="utf-8",
    )
    (tmp_path / "input.txt").write_text("source", encoding="utf-8")
    (tmp_path / "run.toml").write_text(
        '[run]\nfleet="workers.toml"\n[[inputs]]\nname="source"\npath="input.txt"\n'
        f"[task]\n{task}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=message):
        prepare_configured_run(tmp_path / "run.toml")
    assert not (tmp_path / ".aig").exists()


def test_unreferenced_declared_skill_directory_fails(tmp_path):
    (tmp_path / "workers.toml").write_text(
        '[fleet]\ndb_path="store.db"\n[[workers]]\nid="w"\nkind="llm"\nmodel="offline"\n',
        encoding="utf-8",
    )
    (tmp_path / "empty-skill").mkdir()
    (tmp_path / "input.txt").write_text("source", encoding="utf-8")
    (tmp_path / "run.toml").write_text(
        '[run]\nfleet="workers.toml"\nskills=["empty-skill"]\n'
        '[[inputs]]\nname="source"\npath="input.txt"\n'
        '[task]\nname="root"\ninputs=["source"]\noutputs=["result"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no skill.toml"):
        prepare_configured_run(tmp_path / "run.toml")


def test_input_limit_is_enforced_during_preparation(tmp_path):
    (tmp_path / "workers.toml").write_text(
        '[fleet]\ndb_path="store.db"\n[[workers]]\nid="w"\nkind="llm"\nmodel="offline"\n',
        encoding="utf-8",
    )
    (tmp_path / "large.txt").write_bytes(b"a" * (1_048_576 + 1))
    (tmp_path / "run.toml").write_text(
        '[run]\nfleet="workers.toml"\n[[inputs]]\nname="source"\npath="large.txt"\n'
        '[task]\nname="root"\ninputs=["source"]\noutputs=["result"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exceeds"):
        prepare_configured_run(tmp_path / "run.toml")


@pytest.mark.parametrize("db_path", ["", ":memory:"])
def test_configured_fleet_rejects_ephemeral_store_before_provisioning(
    tmp_path, monkeypatch, db_path
):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "workers.toml"
    config.write_text(
        f'[fleet]\ndb_path="{db_path}"\n'
        '[[workers]]\nid="unused"\nkind="llm"\nmodel="unused"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="persistent fleet.db_path"):
        prepare_configured_run(config, resume_task_id="existing")
    assert not (tmp_path / ".aig").exists()
