"""Behavior of the private, process-safe named-board registry."""

from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from aigineering.core.domain import load_genesis
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_boards import (
    BoardSetupPending,
    BoardEntry,
    BoardRegistry,
    BoardRegistryError,
    SCHEMA,
    archive_board,
    create_board,
    discover_registry_root,
    read_registry,
    resume_board_setup,
    resolve_board_workspace,
    select_board,
    update_registry,
)
from aigineering.local_domain import initialize_local_domain


def test_registry_writes_private_atomic_document_and_preserves_selection(tmp_path):
    entry = BoardEntry("research", str((tmp_path / "work").resolve()), "genesis:test")
    result = update_registry(
        tmp_path, lambda current: BoardRegistry("research", (*current.boards, entry))
    )
    path = tmp_path / ".aig" / "boards.json"

    assert result.selected == "research"
    assert json.loads(path.read_text()) == {
        "schema": SCHEMA,
        "selected": "research",
        "boards": [
            {
                "name": "research",
                "workspace": entry.workspace,
                "domain_id": "genesis:test",
                "archived": False,
            }
        ],
    }
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
        assert (tmp_path / ".aig" / "boards.json.lock").stat().st_mode & 0o077 == 0


def test_concurrent_registry_updates_do_not_lose_entries(tmp_path):
    def add(index: int) -> None:
        entry = BoardEntry(
            f"board-{index}",
            str((tmp_path / f"work-{index}").resolve()),
            f"genesis:{index}",
        )

        def append(current):
            return BoardRegistry(current.selected, (*current.boards, entry))

        update_registry(tmp_path, append)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add, range(16)))

    registry = read_registry(tmp_path)
    assert {item.name for item in registry.boards} == {f"board-{i}" for i in range(16)}


@pytest.mark.parametrize("body", [b"{", b"{}", b'{"schema":"wrong"}'])
def test_corrupt_or_unsupported_registry_fails_without_repair(tmp_path, body):
    directory = tmp_path / ".aig"
    directory.mkdir()
    path = directory / "boards.json"
    path.write_bytes(body)
    path.chmod(0o600)

    with pytest.raises(BoardRegistryError):
        read_registry(tmp_path)
    assert path.read_bytes() == body


def test_oversized_and_symlink_registry_fail_closed(tmp_path):
    directory = tmp_path / ".aig"
    directory.mkdir()
    path = directory / "boards.json"
    path.write_bytes(b" " * (1_048_577))
    path.chmod(0o600)
    with pytest.raises(BoardRegistryError, match="size"):
        read_registry(tmp_path)

    path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text('{"schema":"aig-local-boards-v1","selected":null,"boards":[]}')
    path.symlink_to(outside)
    with pytest.raises(BoardRegistryError, match="symlink"):
        read_registry(tmp_path)


def test_create_board_rejects_store_database_symlink(tmp_path):
    root = tmp_path / "project"
    (root / ".aig").mkdir(parents=True)
    target = tmp_path / "external.db"
    target.write_bytes(b"not a board store")
    (root / ".aig" / "store.db").symlink_to(target)

    with pytest.raises(BoardRegistryError, match="Store path.*symlink"):
        create_board(tmp_path, "linked", workspace=root)


@pytest.mark.parametrize("link_name", ["boards", "board-name", "identity"])
def test_managed_board_creation_rejects_symlink_components(
    tmp_path, link_name, monkeypatch
):
    import aigineering.local_boards as boards

    root = tmp_path / "project"
    (root / ".aig").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    boards_root = root / ".aig" / "boards"
    boards_root.mkdir()
    if link_name == "boards":
        boards_root.rmdir()
        boards_root.symlink_to(outside, target_is_directory=True)
    else:
        target = boards_root / "linked"
        target.mkdir()
        if link_name == "board-name":
            target.rmdir()
            target.symlink_to(outside, target_is_directory=True)
        else:
            (target / ".aig").mkdir()
            (target / ".aig" / "identity").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(boards, "sync_runtime", lambda *_args, **_kwargs: None)
    with pytest.raises(BoardRegistryError, match="symlink"):
        create_board(root, "linked")
    assert list(outside.iterdir()) == []


def test_registry_rejects_noncanonical_paths_and_duplicate_identity(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    entry = BoardEntry("one", str(work), "genesis:one")
    update_registry(tmp_path, lambda current: BoardRegistry(None, (entry,)))
    path = tmp_path / ".aig" / "boards.json"
    data = json.loads(path.read_text())
    data["boards"].append({**data["boards"][0], "name": "two"})
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    with pytest.raises(BoardRegistryError, match="duplicate"):
        read_registry(tmp_path)


def test_shared_domain_initializer_pins_exact_genesis_and_refuses_reinit(tmp_path):
    workspace = tmp_path / "workspace"
    store_path = workspace / ".aig" / "store.db"
    store_path.parent.mkdir(parents=True)
    key_path = workspace / ".aig" / "identity" / "root.ed25519"
    store = SQLiteStore(store_path)
    created = initialize_local_domain(
        store, domain_name="board:research", key_path=key_path
    )
    assert load_genesis(store).id == created.manifest.id
    assert created.key_path == key_path
    if os.name != "nt":
        assert key_path.stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError, match="already initialized"):
        initialize_local_domain(store, key_path=key_path)
    store.close()


def test_partial_key_without_genesis_is_not_silently_replaced(tmp_path):
    workspace = tmp_path / "workspace"
    store_path = workspace / ".aig" / "store.db"
    store_path.parent.mkdir(parents=True)
    key_path = workspace / ".aig" / "identity" / "root.ed25519"
    key_path.parent.mkdir(parents=True)
    key_path.write_text("existing partial key\n")
    store = SQLiteStore(store_path)

    with pytest.raises(ValueError, match="key already exists"):
        initialize_local_domain(store, key_path=key_path)
    assert key_path.read_text() == "existing partial key\n"
    assert not store.scan_runtime_records()
    store.close()


def test_committed_genesis_is_adopted_and_synced_after_relay_pending(
    tmp_path, monkeypatch
):
    import aigineering.local_boards as boards
    from aigineering.runtime_services import RedisSyncPending

    def pending(*_args, **_kwargs):
        raise RedisSyncPending(accepted=None)

    monkeypatch.setattr(boards, "sync_runtime", pending)
    with pytest.raises(BoardSetupPending) as captured:
        create_board(tmp_path, "recoverable")
    failure = captured.value
    assert failure.facts_retained is True
    assert failure.registry_registered is False
    assert failure.phase == "redis_sync"
    assert failure.retry_command == "aig board create recoverable"
    workspace = Path(failure.workspace)
    store = SQLiteStore(workspace / ".aig" / "store.db")
    genesis_before = load_genesis(store)
    store.close()
    assert read_registry(tmp_path).by_name("recoverable") is None

    observed = []
    monkeypatch.setattr(
        boards,
        "sync_runtime",
        lambda store, **kwargs: observed.append(load_genesis(store).id),
    )
    entry = create_board(tmp_path, "recoverable")
    assert entry.domain_id == genesis_before.id
    assert observed == [genesis_before.id]
    assert read_registry(tmp_path).by_name("recoverable") == entry


def test_registry_replace_failure_before_publish_retries_create(tmp_path, monkeypatch):
    import aigineering.local_boards as boards

    monkeypatch.setattr(boards, "sync_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        boards.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )
    with pytest.raises(BoardSetupPending) as captured:
        create_board(tmp_path, "retry-before")
    pending = captured.value
    assert pending.phase == "registry_publish"
    assert pending.registry_registered is False
    assert pending.retry_command == "aig board create retry-before"
    assert read_registry(tmp_path).by_name("retry-before") is None


def test_directory_fsync_failure_resumes_exact_registered_board(tmp_path, monkeypatch):
    import aigineering.local_boards as boards

    monkeypatch.setattr(boards, "sync_runtime", lambda *_args, **_kwargs: None)
    real_fsync = boards.os.fsync

    def fail_directory_fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("directory sync failed")
        return real_fsync(descriptor)

    monkeypatch.setattr(boards.os, "fsync", fail_directory_fsync)
    with pytest.raises(BoardSetupPending) as captured:
        create_board(tmp_path, "retry-after")
    pending = captured.value
    assert pending.phase == "registry_publish"
    assert pending.registry_registered is True
    assert pending.retry_command == "aig board create retry-after --resume"
    registered = read_registry(tmp_path).by_name("retry-after")
    assert registered is not None

    monkeypatch.setattr(boards.os, "fsync", real_fsync)
    resumed = resume_board_setup(tmp_path, "retry-after")
    assert resumed == registered
    assert read_registry(tmp_path).by_name("retry-after") == registered


def test_resume_refuses_registry_entry_changed_during_sync(tmp_path, monkeypatch):
    import aigineering.local_boards as boards

    monkeypatch.setattr(boards, "sync_runtime", lambda *_args, **_kwargs: None)
    entry = create_board(
        tmp_path,
        "changed",
    )

    def change_registry(_store, **_kwargs):
        update_registry(
            tmp_path,
            lambda current: BoardRegistry(
                current.selected,
                tuple(
                    BoardEntry(item.name, item.workspace, item.domain_id, True)
                    if item.name == "changed"
                    else item
                    for item in current.boards
                ),
            ),
        )

    monkeypatch.setattr(boards, "sync_runtime", change_registry)
    with pytest.raises(BoardRegistryError, match="changed during resume"):
        resume_board_setup(tmp_path, "changed")
    assert read_registry(tmp_path).by_name("changed") == BoardEntry(
        entry.name, entry.workspace, entry.domain_id, True
    )


def test_selected_archive_blocks_routing_until_explicit_restore(tmp_path):
    workspace = tmp_path / "workspace"
    store_path = workspace / ".aig" / "store.db"
    store_path.parent.mkdir(parents=True)
    store = SQLiteStore(store_path)
    genesis = initialize_local_domain(
        store, key_path=workspace / ".aig" / "identity" / "root.ed25519"
    ).manifest
    store.close()
    entry = BoardEntry("current", str(workspace.resolve()), genesis.id)
    update_registry(tmp_path, lambda current: BoardRegistry("current", (entry,)))

    assert discover_registry_root(workspace) == tmp_path
    assert resolve_board_workspace(workspace) == workspace
    archived = archive_board(tmp_path, "current")
    assert archived.archived is True
    assert read_registry(tmp_path).selected == "current"
    with pytest.raises(BoardRegistryError, match="archived"):
        resolve_board_workspace(workspace)

    restored = archive_board(tmp_path, "current", restore=True)
    assert restored.archived is False
    assert select_board(tmp_path, "current") == entry
    assert resolve_board_workspace(workspace) == workspace
