"""Private project-local registry for named local workspaces."""

from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from aigineering.core.domain import load_genesis
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_domain import initialize_local_domain
from aigineering.local_identity import actor_key_path
from aigineering.runtime_services import sync_runtime

SCHEMA = "aig-local-boards-v1"
MAX_REGISTRY_BYTES = 1_048_576


class BoardRegistryError(ValueError):
    """A malformed, unsafe, or conflicting local board registry."""


class BoardSetupPending(BoardRegistryError):
    """A domain exists but registry or Redis setup did not finish."""

    def __init__(
        self,
        message: str,
        *,
        workspace: Path,
        domain_id: str,
        board_name: str,
        explicit_workspace: bool,
        phase: str,
        registry_registered: bool = False,
    ):
        super().__init__(message)
        self.workspace = str(workspace)
        self.domain_id = domain_id
        self.phase = phase
        self.registry_registered = registry_registered
        self.facts_retained = True
        command = ["aig", "board", "create", board_name]
        if registry_registered:
            command.append("--resume")
        if explicit_workspace:
            command.extend(["--workspace", str(workspace)])
        self.retry_command = shlex.join(command)


@dataclass(frozen=True)
class BoardEntry:
    name: str
    workspace: str
    domain_id: str
    archived: bool = False


@dataclass(frozen=True)
class BoardRegistry:
    selected: str | None
    boards: tuple[BoardEntry, ...]

    def by_name(self, name: str) -> BoardEntry | None:
        return next((board for board in self.boards if board.name == name), None)


_BOARD_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def validate_name(name: str) -> str:
    if not isinstance(name, str) or _BOARD_NAME.fullmatch(name) is None:
        raise BoardRegistryError(
            "board name must be 1-64 letters, digits, underscores or hyphens"
        )
    return name


def registry_path(root: Path) -> Path:
    return root / ".aig" / "boards.json"


def discover_registry_root(cwd: Path | None = None) -> Path | None:
    """Return the nearest ancestor containing a boards registry, if any."""
    current = (cwd or Path.cwd()).absolute()
    for candidate in (current, *current.parents):
        path = registry_path(candidate)
        if path.exists() or path.is_symlink():
            return candidate
    return None


def _check_parent(root: Path, *, create: bool) -> Path:
    directory = root / ".aig"
    if directory.is_symlink():
        raise BoardRegistryError("board registry directory may not be a symlink")
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def _decode(data: bytes) -> BoardRegistry:
    if len(data) > MAX_REGISTRY_BYTES:
        raise BoardRegistryError("board registry exceeds the supported size")
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BoardRegistryError("board registry is corrupt") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema", "selected", "boards"}:
        raise BoardRegistryError("board registry has an unsupported shape")
    if raw["schema"] != SCHEMA or not isinstance(raw["boards"], list):
        raise BoardRegistryError("board registry has an unsupported schema")
    selected = raw["selected"]
    if selected is not None and (not isinstance(selected, str) or not selected):
        raise BoardRegistryError("board registry selection is invalid")
    if selected is not None:
        validate_name(selected)
    boards: list[BoardEntry] = []
    names: set[str] = set()
    paths: set[str] = set()
    domains: set[str] = set()
    for item in raw["boards"]:
        if not isinstance(item, dict) or set(item) != {
            "name",
            "workspace",
            "domain_id",
            "archived",
        }:
            raise BoardRegistryError("board registry entry has an unsupported shape")
        name, workspace, domain_id, archived = (
            item["name"],
            item["workspace"],
            item["domain_id"],
            item["archived"],
        )
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(workspace, str)
            or not Path(workspace).is_absolute()
            or not isinstance(domain_id, str)
            or not domain_id
            or type(archived) is not bool
        ):
            raise BoardRegistryError("board registry entry contains invalid values")
        validate_name(name)
        if str(Path(workspace).resolve(strict=False)) != workspace:
            raise BoardRegistryError("board workspace path is not canonical")
        if name in names or workspace in paths or domain_id in domains:
            raise BoardRegistryError("board registry contains duplicate identities")
        names.add(name)
        paths.add(workspace)
        domains.add(domain_id)
        boards.append(BoardEntry(name, workspace, domain_id, archived))
    if selected is not None and selected not in names:
        raise BoardRegistryError("selected board is missing from the registry")
    return BoardRegistry(selected, tuple(sorted(boards, key=lambda item: item.name)))


def _encode(registry: BoardRegistry) -> bytes:
    payload = {
        "schema": SCHEMA,
        "selected": registry.selected,
        "boards": [
            {
                "name": item.name,
                "workspace": item.workspace,
                "domain_id": item.domain_id,
                "archived": item.archived,
            }
            for item in sorted(registry.boards, key=lambda value: value.name)
        ],
    }
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    if len(encoded) > MAX_REGISTRY_BYTES:
        raise BoardRegistryError("board registry exceeds the supported size")
    return encoded


def _read(root: Path, *, missing_ok: bool = True) -> BoardRegistry:
    directory = _check_parent(root, create=False)
    path = directory / "boards.json"
    if path.is_symlink():
        raise BoardRegistryError("board registry may not be a symlink")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        if missing_ok:
            return BoardRegistry(None, ())
        raise BoardRegistryError("board registry does not exist")
    except OSError as exc:
        raise BoardRegistryError("board registry cannot be opened safely") from exc
    try:
        info = os.fstat(descriptor)
        if info.st_size > MAX_REGISTRY_BYTES:
            raise BoardRegistryError("board registry exceeds the supported size")
        if not stat.S_ISREG(info.st_mode):
            raise BoardRegistryError("board registry is not a supported regular file")
        if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
            raise BoardRegistryError("board registry permissions must be private")
        chunks = bytearray()
        while len(chunks) <= MAX_REGISTRY_BYTES:
            chunk = os.read(
                descriptor, min(65536, MAX_REGISTRY_BYTES + 1 - len(chunks))
            )
            if not chunk:
                break
            chunks.extend(chunk)
        return _decode(bytes(chunks))
    finally:
        os.close(descriptor)


@contextmanager
def _locked(root: Path, *, create: bool) -> Iterator[None]:
    directory = _check_parent(root, create=create)
    lock_path = directory / "boards.json.lock"
    if lock_path.is_symlink():
        raise BoardRegistryError("board registry lock may not be a symlink")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise BoardRegistryError("board registry lock cannot be opened safely") from exc
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def read_registry(root: Path) -> BoardRegistry:
    # Atomic replacement makes an unlocked read observe either the old or the
    # new complete document. Writers serialize through the adjacent lock.
    return _read(root)


def update_registry(root: Path, transform) -> BoardRegistry:
    """Apply one process-serialized update and atomically replace the file."""
    root = root.absolute()
    with _locked(root, create=True):
        current = _read(root)
        updated = transform(current)
        if not isinstance(updated, BoardRegistry):
            raise TypeError("registry update must return BoardRegistry")
        data = _encode(updated)
        _decode(data)
        path = registry_path(root)
        if path.is_symlink():
            raise BoardRegistryError("board registry may not be a symlink")
        descriptor, temporary = tempfile.mkstemp(
            prefix=".boards.", suffix=".tmp", dir=path.parent
        )
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            if os.name != "nt":
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        return updated


def board_root_for_management(cwd: Path | None = None) -> Path:
    current = (cwd or Path.cwd()).resolve()
    return discover_registry_root(current) or current


def create_board(
    root: Path,
    name: str,
    *,
    workspace: Path | None = None,
) -> BoardEntry:
    """Create a managed domain or pin an explicitly initialized workspace."""
    validate_name(name)
    root = root.resolve()
    _check_parent(root, create=False)
    registry = read_registry(root)
    if registry.by_name(name) is not None:
        raise BoardRegistryError(f"board {name!r} already exists")

    managed = workspace is None
    if managed:
        target = _managed_board_path(root, name, create=True)
    else:
        target = workspace.resolve(strict=True)
    if not target.is_dir():
        raise BoardRegistryError("board workspace must be an existing directory")
    if (target / ".aig").is_symlink():
        raise BoardRegistryError("board workspace .aig directory may not be a symlink")
    target_store_path = target / ".aig" / "store.db"
    if target_store_path.is_symlink():
        raise BoardRegistryError("board workspace Store path may not be a symlink")
    expected_key_path = target / ".aig" / "identity" / "root.ed25519"
    if managed:
        identity_directory = expected_key_path.parent
        if identity_directory.is_symlink() or expected_key_path.is_symlink():
            raise BoardRegistryError("managed board identity path may not be a symlink")
        explicit_key_path = os.environ.get("AIG_ACTOR_KEY_FILE")
        if explicit_key_path:
            configured = actor_key_path().resolve()
            if configured != expected_key_path:
                raise BoardRegistryError(
                    "AIG_ACTOR_KEY_FILE points outside the managed board; refusing to override it"
                )
    else:
        if not target_store_path.is_file():
            raise BoardRegistryError("explicit workspace has no initialized Store")

    for existing in registry.boards:
        if existing.workspace == str(target):
            raise BoardRegistryError("workspace is already registered to another board")

    if managed:
        target_store_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        had_key = expected_key_path.exists()
        store = SQLiteStore(target_store_path)
        try:
            try:
                manifest = load_genesis(store)
            except LookupError:
                if had_key:
                    raise BoardRegistryError(
                        "managed board has a root key but no Genesis; explicit recovery is required"
                    )
                initialized = initialize_local_domain(
                    store,
                    domain_name=f"board:{name}",
                    key_path=expected_key_path,
                )
                manifest = initialized.manifest
            except (RuntimeError, ValueError) as exc:
                raise BoardRegistryError("managed board Genesis is invalid") from exc
            if manifest.domain != f"board:{name}":
                raise BoardRegistryError(
                    "managed board Genesis domain does not match its requested name"
                )
            if any(item.domain_id == manifest.id for item in registry.boards):
                raise BoardRegistryError(
                    "Genesis domain is already registered to another board"
                )
            _sync_for_board_setup(
                store, target, manifest.id, name, explicit_workspace=False
            )
        finally:
            store.close()
    else:
        try:
            store = SQLiteStore.snapshot_from_path(target_store_path)
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise BoardRegistryError("explicit workspace Store is unavailable") from exc
        try:
            try:
                manifest = load_genesis(store)
            except LookupError as exc:
                raise BoardRegistryError(
                    "explicit workspace has no initialized Genesis"
                ) from exc
            if any(item.domain_id == manifest.id for item in registry.boards):
                raise BoardRegistryError(
                    "Genesis domain is already registered to another board"
                )
            _sync_for_board_setup(
                store, target, manifest.id, name, explicit_workspace=True
            )
        finally:
            store.close()

    if any(existing.domain_id == manifest.id for existing in registry.boards):
        raise BoardRegistryError(
            "Genesis domain is already registered to another board"
        )
    entry = BoardEntry(name, str(target), manifest.id, False)

    def append(current: BoardRegistry) -> BoardRegistry:
        if current.by_name(name) is not None:
            raise BoardRegistryError(f"board {name!r} already exists")
        if any(item.workspace == entry.workspace for item in current.boards):
            raise BoardRegistryError("workspace is already registered to another board")
        if any(item.domain_id == entry.domain_id for item in current.boards):
            raise BoardRegistryError(
                "Genesis domain is already registered to another board"
            )
        return BoardRegistry(current.selected, (*current.boards, entry))

    try:
        update_registry(root, append)
    except (OSError, BoardRegistryError) as exc:
        try:
            published = read_registry(root).by_name(name) == entry
        except BoardRegistryError:
            published = False
        raise BoardSetupPending(
            "board domain is initialized but registry publication is pending",
            workspace=target,
            domain_id=manifest.id,
            board_name=name,
            explicit_workspace=not managed,
            phase="registry_publish",
            registry_registered=published,
        ) from exc
    return entry


def _managed_board_path(root: Path, name: str, *, create: bool) -> Path:
    """Resolve a managed workspace without following registry-controlled links."""
    root = root.resolve()
    aig_directory = root / ".aig"
    boards_root = aig_directory / "boards"
    raw_target = boards_root / name
    if (
        aig_directory.is_symlink()
        or boards_root.is_symlink()
        or raw_target.is_symlink()
    ):
        raise BoardRegistryError("managed board path may not contain symlinks")
    canonical_boards = boards_root.resolve(strict=False)
    target = raw_target.resolve(strict=False)
    try:
        target.relative_to(canonical_boards)
    except ValueError as exc:
        raise BoardRegistryError(
            "managed board path escapes its registry root"
        ) from exc
    if create:
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        if raw_target.is_symlink() or not target.resolve(strict=True).is_relative_to(
            canonical_boards
        ):
            raise BoardRegistryError("managed board path changed during creation")
    return target


def resume_board_setup(
    root: Path, name: str, *, workspace: Path | None = None
) -> BoardEntry:
    """Recover sync/registry durability for one already registered exact board."""
    validate_name(name)
    root = root.resolve()
    current = read_registry(root).by_name(name)
    if current is None:
        raise BoardRegistryError(
            f"board {name!r} is not registered; retry with ordinary board create"
        )
    if workspace is None:
        managed_path = _managed_board_path(root, name, create=False)
        expected_workspace = str(managed_path)
        expected_genesis_domain = f"board:{name}"
        explicit_workspace = False
    else:
        expected_workspace = str(workspace.resolve(strict=True))
        expected_genesis_domain = None
        explicit_workspace = True
    if current.workspace != expected_workspace:
        raise BoardRegistryError("resume workspace does not match the registered board")
    target = Path(current.workspace)
    if (target / ".aig").is_symlink():
        raise BoardRegistryError("board workspace .aig directory may not be a symlink")
    if (target / ".aig" / "store.db").is_symlink():
        raise BoardRegistryError("board workspace Store path may not be a symlink")
    store = SQLiteStore.snapshot_from_path(target / ".aig" / "store.db")
    try:
        try:
            manifest = load_genesis(store)
        except (LookupError, RuntimeError, ValueError) as exc:
            raise BoardRegistryError("registered board Genesis is unavailable") from exc
        if manifest.id != current.domain_id or (
            expected_genesis_domain is not None
            and manifest.domain != expected_genesis_domain
        ):
            raise BoardRegistryError(
                "registered board Genesis no longer matches its pin"
            )
        try:
            sync_runtime(store, accepted=None)
        except Exception as exc:
            from aigineering.runtime_services import RedisSyncPending

            if not isinstance(exc, RedisSyncPending):
                raise
            raise BoardSetupPending(
                "board domain is present but Redis synchronization is pending",
                workspace=target,
                domain_id=manifest.id,
                board_name=name,
                explicit_workspace=explicit_workspace,
                phase="redis_sync",
                registry_registered=True,
            ) from exc
    finally:
        store.close()

    def verify_unchanged(latest: BoardRegistry) -> BoardRegistry:
        if latest.by_name(name) != current:
            raise BoardRegistryError("registered board changed during resume")
        return latest

    try:
        update_registry(root, verify_unchanged)
    except OSError as exc:
        raise BoardSetupPending(
            "board recovery synced Redis but registry durability is still pending",
            workspace=target,
            domain_id=current.domain_id,
            board_name=name,
            explicit_workspace=explicit_workspace,
            phase="registry_publish",
            registry_registered=True,
        ) from exc
    return current


def _sync_for_board_setup(
    store, workspace: Path, domain_id: str, board_name: str, *, explicit_workspace: bool
) -> None:
    try:
        sync_runtime(store, accepted=None)
    except Exception as exc:
        from aigineering.runtime_services import RedisSyncPending

        if not isinstance(exc, RedisSyncPending):
            raise
        raise BoardSetupPending(
            "board domain is present but Redis synchronization is pending",
            workspace=workspace,
            domain_id=domain_id,
            board_name=board_name,
            explicit_workspace=explicit_workspace,
            phase="redis_sync",
        ) from exc


def board_status(board: BoardEntry) -> str:
    try:
        validate_board_pin(board)
    except BoardRegistryError as exc:
        if "no Genesis" in str(exc):
            return "missing_genesis"
        if "no longer matches" in str(exc):
            return "domain_mismatch"
        return "unavailable"
    return "available"


def select_board(root: Path, name: str) -> BoardEntry:
    validate_name(name)

    def select(current: BoardRegistry) -> BoardRegistry:
        latest = current.by_name(name)
        if latest is None:
            raise BoardRegistryError(f"board {name!r} does not exist")
        if latest.archived:
            raise BoardRegistryError(f"board {name!r} is archived; restore it first")
        validate_board_pin(latest)
        return BoardRegistry(name, current.boards)

    registry = update_registry(root, select)
    result = registry.by_name(name)
    assert result is not None
    return result


def archive_board(root: Path, name: str, *, restore: bool = False) -> BoardEntry:
    validate_name(name)

    def change(current: BoardRegistry) -> BoardRegistry:
        board = current.by_name(name)
        if board is None:
            raise BoardRegistryError(f"board {name!r} does not exist")
        updated = BoardEntry(board.name, board.workspace, board.domain_id, not restore)
        return BoardRegistry(
            current.selected,
            tuple(updated if item.name == name else item for item in current.boards),
        )

    registry = update_registry(root, change)
    result = registry.by_name(name)
    assert result is not None
    return result


def resolve_board_workspace(
    cwd: Path | None = None, *, explicit_name: str | None = None
) -> Path | None:
    current = (cwd or Path.cwd()).resolve()
    root = discover_registry_root(current)
    if root is None:
        if explicit_name is not None:
            raise BoardRegistryError("no local board registry exists in this workspace")
        return None
    registry = read_registry(root)
    name = explicit_name if explicit_name is not None else registry.selected
    if name is None:
        return None
    board = registry.by_name(name)
    if board is None:
        raise BoardRegistryError(f"selected board {name!r} is missing")
    if board.archived:
        raise BoardRegistryError(f"selected board {name!r} is archived")
    validate_board_pin(board)
    return Path(board.workspace)


def validate_board_pin(board: BoardEntry) -> str:
    """Read the pinned workspace Genesis and require its exact registered ID."""
    workspace = Path(board.workspace)
    aig_directory = workspace / ".aig"
    store_path = aig_directory / "store.db"
    if aig_directory.is_symlink() or store_path.is_symlink():
        raise BoardRegistryError("board workspace Store path may not be a symlink")
    if not store_path.is_file():
        raise BoardRegistryError("board workspace Store is unavailable")
    try:
        store = SQLiteStore.snapshot_from_path(store_path)
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise BoardRegistryError("board workspace Store is unavailable") from exc
    try:
        try:
            manifest = load_genesis(store)
        except LookupError as exc:
            raise BoardRegistryError("board workspace has no Genesis domain") from exc
        except RuntimeError as exc:
            raise BoardRegistryError("board workspace Genesis is invalid") from exc
        if manifest.id != board.domain_id:
            raise BoardRegistryError(
                "board workspace Genesis no longer matches its pin"
            )
        return manifest.id
    finally:
        store.close()
