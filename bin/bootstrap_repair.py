#!/usr/bin/env python3
from __future__ import annotations
import ast
import argparse
import hashlib
import json
import os
import re
import shlex
import stat
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

NAME = "triad-repair-analyzer"
ANALYZER_MARKER = "# agy-triad-dispatch managed repair analyzer"
FROZEN_REPAIR_ANALYZER_SHA256 = (
    "549d49b7ca1d50fe4bed5a86bb4775a11b56036c646e7fa91b29383aae7d3a0e"
)
REG_BEGIN = "# >>> agy-triad-dispatch managed repair analyzer registration >>>"
REG_END = "# <<< agy-triad-dispatch managed repair analyzer registration <<<"
LAUNCHER = "triad-apply-repair"
LAUNCHER_MARKER = "# agy-triad-dispatch managed repair apply launcher"
REG_DESCRIPTION = "Read-only triad repair analyzer for untrusted vendor run logs."
PROFILE_MARKER = b"# agy-triad-dispatch managed runtime profile"
RULES_MARKER = b"# agy-triad-dispatch managed command rules"
PUBLIC_LAUNCHER_MARKER = b"# agy-triad-dispatch managed launcher"
LEGACY_PUBLIC_LAUNCHER_MARKER = b"# triad-codex-dispatch managed launcher"
PUBLIC_RUNTIME_MARKER = b"# agy-triad-dispatch managed runtime command"
LEGACY_PUBLIC_RUNTIME_MARKER = b"# triad-codex-dispatch managed runtime command"
CONFIG_FRAGMENT_BEGIN = (
    b"# >>> agy-triad-dispatch managed shell_environment_policy >>>"
)
CONFIG_FRAGMENT_END = (
    b"# <<< agy-triad-dispatch managed shell_environment_policy <<<"
)
LEGACY_CONFIG_FRAGMENT_BEGIN = (
    b"# >>> triad-codex-dispatch managed shell_environment_policy >>>"
)
LEGACY_CONFIG_FRAGMENT_END = (
    b"# <<< triad-codex-dispatch managed shell_environment_policy <<<"
)
CONFIG_FRAGMENT_INSERTED_SEPARATOR = (
    b"\n# agy-triad-dispatch managed inserted separator\n"
)
SHELL_ENTRY_BEGIN = b"# >>> agy-triad-dispatch agy-triad >>>"
SHELL_ENTRY_END = b"# <<< agy-triad-dispatch agy-triad <<<"
LEGACY_SHELL_ENTRY_BEGIN = b"# >>> triad-codex-dispatch codex-triad >>>"
LEGACY_SHELL_ENTRY_END = b"# <<< triad-codex-dispatch codex-triad <<<"
FROZEN_SHELL_ENTRY_SHA256 = (
    "018cb43c9954d0b39abac95ff38f3310c409259db93380729bb94ea1f97b2006"
)
CURRENT_CONFIG_FRAGMENT_TEXT = (
    b"[shell_environment_policy]\n"
    b'inherit = "all"\n'
    b'exclude = ["LD_*", "DYLD_*", "NODE_OPTIONS", "NODE_PATH", "PYTHON*", '
    b'"BASH_ENV", "ENV", "PERL5LIB", "RUBYOPT", "RUBYLIB"]\n'
)
LEGACY_CONFIG_FRAGMENT_TEXT = (
    b"[shell_environment_policy]\n"
    b'inherit = "core"\n'
)

MANAGED_LAUNCHERS = (
    "claude_wrapper.py",
    "codex_wrapper.py",
    "gemini_wrapper.py",
    "antigravity_wrapper.py",
    "review_round.py",
)

FROZEN_LEGACY_AGENT_SHA256 = frozenset(
    {
        "2f396abfbc2c4d558a00a4a5885c620cdeb9956b099b656e6910ce416bc28cae",
        "0f5b6298b76c6b164cbf189b8eff1765736d0b895eb750ccf1fedb576a979f0b",
        "d7b7988d7f3ff5430ba791760e25714a7d5a40d1e310ea4a060bf879fb26c4d9",
        "ade14b3ec2ede604d49284c8dcbbaeb3ab74b7d706ba6899e57a36fd72a9f942",
        "674c31cab5ad9024d53a02c9784e876be76d22af3a27ca0ed6dc5b480106ee3e",
        "2b6e10b2bcdacea3fbd1c7c784dac26afce6345c83518ab9bf5103facb47141e",
    }
)
_LEGACY_PROFILE_BROAD_DIRECTORY_ROOTS = frozenset(
    [
        Path(value).resolve()
        for value in (
            "/",
            "/usr",
            "/usr/local",
            "/usr/local/bin",
            "/usr/local/sbin",
            "/usr/bin",
            "/usr/sbin",
            "/bin",
            "/sbin",
            "/opt",
            "/opt/homebrew",
            "/opt/homebrew/bin",
            "/opt/homebrew/sbin",
            "/lib",
            "/lib64",
            "/etc",
            "/var",
            "/tmp",
            "/root",
        )
    ]
    + [Path.home().resolve()]
)


class Refusal(RuntimeError):
    pass


class MissingExpected(Refusal):
    pass


@dataclass(frozen=True)
class State:
    path: Path
    data: bytes
    mode: int
    ident: tuple[int, int, int, int, int]


@dataclass(frozen=True)
class Staged:
    state: State

    @property
    def path(self) -> Path:
        return self.state.path

    def __fspath__(self) -> str:
        return os.fspath(self.path)

    def unlink(self, *, missing_ok: bool = False) -> None:
        if missing_ok and _path_is_absent(self.path):
            return
        _delete_expected(self.state)


@dataclass(frozen=True)
class CommandArtifact:
    name: str
    target: Path
    data: bytes
    kind: str
    mode: int = 0o755


@dataclass(frozen=True)
class RepairRemovePlan:
    config: Path
    analyzer: Path
    launcher: Path
    config_before: State | None
    analyzer_before: State | None
    launcher_before: State | None
    analyzer_unmanaged: bool
    launcher_unmanaged: bool
    base: str
    managed_registration: bool
    original_config_existed: bool


@dataclass
class Mutation:
    target: Path
    before: State | None
    after: State | None
    backup: State | None = None
    backup_dir: Path | None = None


class TransactionFailure(Refusal):
    def __init__(self, cause: BaseException, failures: list[BaseException]) -> None:
        super().__init__(f"{cause}; recovery failures: " + "; ".join(map(str, failures)))
        self.cause = cause
        self.failures = failures


def _ident(st: os.stat_result) -> tuple[int, int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns)


def require_safe_ancestors(path: Path) -> None:
    if not path.is_absolute():
        raise Refusal(f"managed path must be absolute: {path}")
    parent = path.parent
    current = Path(parent.anchor)
    parts = parent.parts[1:] if parent.anchor else parent.parts
    for component in parts:
        current /= component
        try:
            status = os.lstat(current)
        except FileNotFoundError:
            return
        except OSError as error:
            raise Refusal(f"could not inspect ancestor for {path}: {current}") from error
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise Refusal(f"refusing unsafe ancestor for {path}: {current}")


def _open_regular(path: Path) -> tuple[int, os.stat_result]:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        raise
    if not stat.S_ISREG(before.st_mode):
        raise Refusal(f"refusing unsafe path: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise Refusal(f"could not safely open path: {path}") from error
    after = os.fstat(fd)
    if not stat.S_ISREG(after.st_mode) or _ident(before) != _ident(after):
        os.close(fd)
        raise Refusal(f"path changed while opening: {path}")
    return fd, after


def read_state(path: Path) -> State | None:
    try:
        fd, st = _open_regular(path)
    except FileNotFoundError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            data = handle.read()
    except OSError as error:
        raise Refusal(f"could not read path: {path}") from error
    return State(path, data, stat.S_IMODE(st.st_mode), _ident(st))


def same(state: State) -> bool:
    try:
        fd, st = _open_regular(state.path)
    except (FileNotFoundError, Refusal):
        return False
    try:
        with os.fdopen(fd, "rb") as handle:
            return _ident(st) == state.ident and handle.read() == state.data
    except OSError:
        return False


def analyzer_data_is_managed(data: bytes) -> bool:
    return hashlib.sha256(data).hexdigest() == FROZEN_REPAIR_ANALYZER_SHA256


def analyzer_is_managed(state: State | None) -> bool:
    return state is not None and analyzer_data_is_managed(state.data)


def launcher_is_managed(state: State | None) -> bool:
    if state is None or not state.data.endswith(b"\n"):
        return False
    try:
        lines = state.data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return False
    if len(lines) not in {5, 7} or lines[1] not in (
        LAUNCHER_MARKER,
        "# triad-codex-dispatch managed repair apply launcher",
    ):
        return False
    shebang_python = _shebang_python(lines[0], isolated=True)
    if shebang_python is None:
        return False
    try:
        tree = ast.parse("\n".join(lines[2:]) + "\n")
    except SyntaxError:
        return False
    if (
        len(tree.body) != len(lines) - 2
        or not _exact_import(tree.body[0], "os")
        or not _exact_import(tree.body[1], "sys")
    ):
        return False
    body = tree.body[2:]
    if len(lines) == 5:
        executed = _exec_call(body[0], "execv", env_arg=False)
        if executed is None:
            return False
        python, argv = executed
        return (
            python == shebang_python
            and len(argv) == 2
            and argv[0] == python
            and os.path.isabs(argv[1])
            and Path(argv[1]).name == "apply_patch.py"
        )
    env_assign = body[0]
    if not (
        isinstance(env_assign, ast.Assign)
        and len(env_assign.targets) == 1
        and isinstance(env_assign.targets[0], ast.Name)
        and env_assign.targets[0].id == "env"
        and _matches_expression(env_assign.value, "os.environ.copy()")
    ):
        return False
    classifier = _string_assignment(
        body[1], owner="env", key="TRIAD_CLASSIFIER_EXTENSION"
    )
    executed = _exec_call(body[2], "execve", env_arg=True)
    if classifier is None or not os.path.isabs(classifier) or executed is None:
        return False
    python, argv = executed
    return (
        python == shebang_python
        and len(argv) == 3
        and argv[0] == python
        and argv[1] == "-E"
        and os.path.isabs(argv[2])
        and Path(argv[2]).name == "apply_patch.py"
    )


def parse_text(state: State | None, path: Path) -> str:
    if state is None:
        return ""
    try:
        return state.data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise Refusal(f"could not decode config: {path}") from error


def parsed_config(text: str, path: Path) -> dict:
    try:
        data = tomllib.loads(text) if text.strip() else {}
    except tomllib.TOMLDecodeError as error:
        raise Refusal(f"could not parse {path}") from error
    if not isinstance(data, dict):
        raise Refusal(f"could not parse {path}")
    return data


def expected_registration(analyzer: Path) -> dict[str, str]:
    return {"description": REG_DESCRIPTION, "config_file": str(analyzer)}


def registration_block(analyzer: Path, original_existed: bool) -> str:
    existed = "true" if original_existed else "false"
    return (
        f"{REG_BEGIN}\n"
        f"# original config existed = {existed}\n"
        f"[agents.{NAME}]\n"
        f"description = {json.dumps(REG_DESCRIPTION)}\n"
        f"config_file = {json.dumps(str(analyzer), ensure_ascii=False)}\n"
        f"{REG_END}\n"
    )


def reserved_registration_markers(text: str, path: Path, parsed: dict) -> list[str]:
    lines = text.splitlines(keepends=True)
    reserved: list[str] = []
    markers = {
        REG_BEGIN,
        REG_END,
        "# >>> triad-codex-dispatch managed repair analyzer registration >>>",
        "# <<< triad-codex-dispatch managed repair analyzer registration <<<",
    }
    for index, line in enumerate(lines):
        marker = line.rstrip("\r\n")
        if marker not in markers:
            continue
        ending = line[len(marker) :]
        changed = lines.copy()
        changed[index] = f"# agy-triad-dispatch marker context probe {index}{ending}"
        if parsed_config("".join(changed), path) == parsed:
            reserved.append(marker)
    return reserved


def split_registration(
    text: str, path: Path, analyzer: Path
) -> tuple[str, str, bool, bool]:
    parsed = parsed_config(text, path)
    reserved = reserved_registration_markers(text, path, parsed)
    agents = parsed.get("agents", {})
    actual = agents.get(NAME) if isinstance(agents, dict) else None
    expected = expected_registration(analyzer)
    candidates: list[tuple[str, str, bool]] = []
    for original_existed in (False, True):
        block = registration_block(analyzer, original_existed)
        separator = "\n" if original_existed else ""
        needle = separator + block
        start = 0
        while True:
            start = text.find(needle, start)
            if start < 0:
                break
            before = text[:start]
            after = text[start + len(needle) :]
            if actual == expected:
                preserved = before + after
                try:
                    base_agents = parsed_config(preserved, path).get("agents", {})
                except Refusal:
                    base_agents = None
                if (
                    isinstance(base_agents, dict)
                    and NAME not in base_agents
                    and reserved in ([REG_BEGIN, REG_END], [
                        "# >>> triad-codex-dispatch managed repair analyzer registration >>>",
                        "# <<< triad-codex-dispatch managed repair analyzer registration <<<",
                    ])
                ):
                    candidates.append((before, after, original_existed))
            start += len(needle)
    if len(candidates) > 1:
        raise Refusal(f"malformed managed repair analyzer registration in {path}")
    if candidates:
        before, after, original_existed = candidates[0]
        return before, after, True, original_existed
    if reserved:
        raise Refusal(f"malformed managed repair analyzer registration in {path}")
    return text, "", False, False


def _state_from_fd(path: Path, fd: int) -> State:
    before = os.fstat(fd)
    chunks: list[bytes] = []
    offset = 0
    while offset < before.st_size:
        chunk = os.pread(fd, min(1024 * 1024, before.st_size - offset), offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    after = os.fstat(fd)
    data = b"".join(chunks)
    if _ident(before) != _ident(after) or len(data) != after.st_size:
        raise Refusal(f"staged descriptor changed while reading: {path}")
    return State(path, data, stat.S_IMODE(after.st_mode), _ident(after))


def _write_all(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("could not write staged data")
        remaining = remaining[written:]


def creation_mode(base: int = 0o666) -> int:
    current = os.umask(0)
    os.umask(current)
    return base & ~current


def stage(path: Path, data: bytes, mode: int) -> Staged:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    staged_path = Path(name)
    staged_state: State | None = None
    try:
        os.fchmod(fd, mode & 0o777)
        _write_all(fd, data)
        staged_state = _state_from_fd(staged_path, fd)
        if staged_state.data != data:
            raise Refusal(f"could not stage complete data: {staged_path}")
        os.fsync(fd)
        staged_state = _state_from_fd(staged_path, fd)
    except BaseException as error:
        failures: list[BaseException] = []
        try:
            if staged_state is None:
                staged_state = _state_from_fd(staged_path, fd)
        except (OSError, Refusal) as state_error:
            failures.append(state_error)
        try:
            os.close(fd)
        except OSError as close_error:
            failures.append(close_error)
        if staged_state is not None:
            try:
                _delete_expected(staged_state)
            except (OSError, Refusal) as cleanup_error:
                failures.append(cleanup_error)
        if failures:
            raise TransactionFailure(error, failures) from error
        raise
    try:
        os.close(fd)
    except OSError as error:
        failures = []
        try:
            _delete_expected(staged_state)
        except (OSError, Refusal) as cleanup_error:
            failures.append(cleanup_error)
        if failures:
            raise TransactionFailure(error, failures) from error
        raise
    return Staged(staged_state)


def fsync_parent(path: Path) -> None:
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _state_matches(actual: State | None, expected: State) -> bool:
    return (
        actual is not None
        and actual.ident == expected.ident
        and actual.mode == expected.mode
        and actual.data == expected.data
    )


def _private_claim_path(target: Path) -> tuple[Path, Path]:
    target.parent.mkdir(parents=True, exist_ok=True)
    directory = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.triad-claim-", dir=target.parent)
    )
    os.chmod(directory, 0o700)
    return directory, directory / "claimed"


def _cleanup_private_state(state: State | None, directory: Path | None) -> None:
    if state is None:
        if directory is not None:
            try:
                os.rmdir(directory)
            except FileNotFoundError:
                pass
            return
    _delete_expected(state, directory)


def _restore_no_clobber(state: State, target: Path, directory: Path) -> None:
    try:
        os.link(state.path, target)
    except FileExistsError as error:
        raise Refusal(
            f"could not restore path without overwriting foreign bytes; "
            f"recovery retained at {state.path}"
        ) from error
    except OSError as error:
        raise Refusal(
            f"filesystem cannot restore path without clobbering; "
            f"recovery retained at {state.path}"
        ) from error
    fsync_parent(target)
    _cleanup_private_state(state, directory)


def _prepare_expected_claim(target: Path, expected: State) -> tuple[State, Path]:
    if not same(expected):
        if _path_is_absent(target):
            raise MissingExpected(f"path vanished before transaction claim: {target}")
        raise Refusal(f"path changed before transaction claim: {target}")
    directory, claimed_path = _private_claim_path(target)
    return (
        State(claimed_path, expected.data, expected.mode, expected.ident),
        directory,
    )


def _perform_expected_claim(
    target: Path, expected: State, planned: State, directory: Path
) -> State:
    try:
        try:
            os.rename(target, planned.path)
        except FileNotFoundError as error:
            if _path_is_absent(target):
                raise MissingExpected(
                    f"path vanished during transaction claim: {target}"
                ) from error
            raise
        claimed = read_state(planned.path)
        if not _state_matches(claimed, expected):
            if claimed is None:
                raise Refusal(f"claimed path vanished: {planned.path}")
            _restore_no_clobber(claimed, target, directory)
            raise Refusal(f"path changed during transaction claim: {target}")
        return claimed
    except BaseException as error:
        recovery_failures: list[BaseException] = []
        try:
            claimed = read_state(planned.path)
            if claimed is not None:
                if _path_is_absent(target):
                    _restore_no_clobber(claimed, target, directory)
                else:
                    recovery_failures.append(
                        Refusal(
                            "could not restore interrupted transaction claim without "
                            f"overwriting {target}; recovery retained at {planned.path}"
                        )
                    )
            else:
                _remove_private_directory(directory)
        except BaseException as recovery_error:
            recovery_failures.append(recovery_error)
        if recovery_failures:
            raise TransactionFailure(error, recovery_failures) from error
        raise


def _claim_expected(target: Path, expected: State) -> tuple[State, Path]:
    planned, directory = _prepare_expected_claim(target, expected)
    try:
        return _perform_expected_claim(target, expected, planned, directory), directory
    except BaseException:
        try:
            os.rmdir(directory)
        except OSError:
            pass
        raise


def _path_is_absent(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    return False


def _remove_private_directory(directory: Path) -> None:
    try:
        os.rmdir(directory)
    except FileNotFoundError:
        pass


def _unlink_claimed_state(
    state: State, directory: Path, recovery_path: Path
) -> None:
    try:
        os.unlink(state.path)
    except FileNotFoundError:
        _remove_private_directory(directory)
        return
    except OSError:
        if _path_is_absent(state.path):
            _remove_private_directory(directory)
            return
        try:
            retry_state, retry_dir = _claim_expected(state.path, state)
        except MissingExpected:
            _remove_private_directory(directory)
            return
        try:
            os.unlink(retry_state.path)
        except FileNotFoundError:
            pass
        except OSError as error:
            recovery_locations = str(retry_state.path)
            try:
                os.link(retry_state.path, recovery_path)
            except OSError:
                pass
            else:
                recovery_locations = f"{recovery_path} and {retry_state.path}"
            raise Refusal(
                f"could not remove claimed transaction state; "
                f"recovery retained at {recovery_locations}: {error}"
            ) from error
        _remove_private_directory(retry_dir)
    _remove_private_directory(directory)


def _delete_expected(expected: State, owner_directory: Path | None = None) -> None:
    try:
        claimed, claim_dir = _claim_expected(expected.path, expected)
    except MissingExpected:
        if owner_directory is not None:
            _remove_private_directory(owner_directory)
        return
    _unlink_claimed_state(claimed, claim_dir, expected.path)
    if not _path_is_absent(expected.path):
        raise Refusal(f"foreign path appeared during transaction cleanup: {expected.path}")
    if owner_directory is not None:
        _remove_private_directory(owner_directory)


def _legacy_agent_data_is_managed(data: bytes) -> bool:
    return hashlib.sha256(data).hexdigest() in FROZEN_LEGACY_AGENT_SHA256


def managed_removal_data_is_owned(data: bytes, kind: str) -> bool:
    if kind in ("profile", "rules"):
        return _managed_artifact_data_is_owned(data, kind)
    if kind == "legacy-agent":
        return _legacy_agent_data_is_managed(data)
    raise Refusal(f"unknown managed removal kind: {kind}")


def remove_managed_artifact(path: Path, kind: str) -> str:
    require_safe_ancestors(path)
    state = read_state(path)
    if state is None:
        return "absent"
    if not managed_removal_data_is_owned(state.data, kind):
        return "unmanaged"

    swap_env = {
        "profile": "TRIAD_BOOTSTRAP_TEST_SWAP_PROFILE_BEFORE_REMOVE",
        "rules": "TRIAD_BOOTSTRAP_TEST_SWAP_RULES_BEFORE_REMOVE",
        "legacy-agent": "TRIAD_BOOTSTRAP_TEST_SWAP_LEGACY_AGENT_BEFORE_REMOVE",
    }[kind]
    swap_source = os.environ.get(swap_env)
    if swap_source:
        os.replace(swap_source, path)

    _delete_expected(state)
    fsync_parent(path)
    return "removed"


def quarantine_managed_artifact(
    path: Path,
    kind: str,
    destination: Path | None = None,
    *,
    quarantine_parent: Path | None = None,
) -> str:
    if (destination is None) == (quarantine_parent is None):
        raise Refusal(
            "managed quarantine requires exactly one destination or quarantine parent"
        )
    require_safe_ancestors(path)
    before = read_state(path)
    if before is None:
        return "absent"
    if not managed_removal_data_is_owned(before.data, kind):
        return "unmanaged"
    created_directory: Path | None = None
    try:
        if destination is None:
            assert quarantine_parent is not None
            require_safe_ancestors(quarantine_parent / ".triad-quarantine-probe")
            try:
                parent_status = os.lstat(quarantine_parent)
            except OSError as error:
                raise Refusal(
                    f"could not inspect quarantine parent: {quarantine_parent}"
                ) from error
            if stat.S_ISLNK(parent_status.st_mode) or not stat.S_ISDIR(
                parent_status.st_mode
            ):
                raise Refusal(
                    f"refusing unsafe quarantine parent: {quarantine_parent}"
                )
            timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            created_directory = Path(
                tempfile.mkdtemp(
                    prefix=f".triad-quarantine-{timestamp}-",
                    dir=quarantine_parent,
                )
            )
            os.chmod(created_directory, 0o700)
            destination = created_directory / path.name
        require_safe_ancestors(destination)
        if not _path_is_absent(destination):
            raise Refusal(f"quarantine destination already exists: {destination}")

        swap_source = os.environ.get(
            "TRIAD_BOOTSTRAP_TEST_SWAP_LEGACY_AGENT_BEFORE_QUARANTINE"
        )
        if kind == "legacy-agent" and swap_source:
            replacement = Path(swap_source)
            if read_state(replacement) is None:
                raise Refusal(
                    f"missing legacy-agent replacement test input: {replacement}"
                )
            os.replace(replacement, path)

        journal: list[Mutation] = []
        failure: BaseException | None = None
        try:
            source_mutation = remove_state(before, journal)
            if source_mutation.backup is None:
                raise Refusal(f"missing quarantine claim for {path}")
            publish_to(Staged(source_mutation.backup), destination, None, journal)
        except BaseException as error:
            failure = error
        finalize_transaction(failure, journal, [])
    except BaseException as error:
        if created_directory is not None:
            try:
                _remove_private_directory(created_directory)
            except BaseException as cleanup_error:
                raise TransactionFailure(error, [cleanup_error]) from error
        raise
    return "quarantined"


def publish_to(
    temp: Staged, target: Path, expected: State | None, journal: list[Mutation]
) -> Mutation:
    staged = temp.state
    if not same(staged):
        raise Refusal(f"missing or changed staged path: {temp.path}")
    after = State(target, staged.data, staged.mode, staged.ident)
    backup: State | None = None
    backup_dir: Path | None = None
    if expected is None:
        try:
            os.lstat(target)
        except FileNotFoundError:
            pass
        else:
            raise Refusal(f"path changed before replacement: {target}")
    else:
        backup, backup_dir = _prepare_expected_claim(target, expected)
    mutation = Mutation(target, expected, after, backup, backup_dir)
    journal.append(mutation)
    if expected is not None:
        assert backup is not None and backup_dir is not None
        _perform_expected_claim(target, expected, backup, backup_dir)
    try:
        os.link(temp.path, target)
    except OSError as error:
        raise Refusal(f"could not publish without overwriting path: {target}") from error
    fsync_parent(target)
    current = read_state(target)
    if not _state_matches(current, after):
        raise Refusal(f"could not publish path: {target}")
    return mutation


def remove_state(expected: State, journal: list[Mutation]) -> Mutation:
    backup, backup_dir = _prepare_expected_claim(expected.path, expected)
    mutation = Mutation(expected.path, expected, None, backup, backup_dir)
    journal.append(mutation)
    _perform_expected_claim(expected.path, expected, backup, backup_dir)
    fsync_parent(expected.path)
    try:
        os.lstat(expected.path)
    except FileNotFoundError:
        pass
    else:
        raise Refusal(f"could not remove path: {expected.path}")
    return mutation


def _publish_single(
    target: Path, data: bytes, expected: State | None, mode: int
) -> None:
    temp: Staged | None = None
    journal: list[Mutation] = []
    failure: BaseException | None = None
    try:
        temp = stage(target, data, mode)
        publish_to(temp, target, expected, journal)
    except BaseException as error:
        failure = error
    cleanup_failures = cleanup_all([temp] if temp is not None else [])
    finalize_transaction(failure, journal, cleanup_failures)


def _remove_single(expected: State) -> None:
    journal: list[Mutation] = []
    failure: BaseException | None = None
    try:
        remove_state(expected, journal)
    except BaseException as error:
        failure = error
    finalize_transaction(failure, journal, [])


def command_data_is_managed(name: str, kind: str, data: bytes) -> bool:
    vendor_envs = {
        "claude_wrapper.py": ("TRIAD_CLAUDE_BIN",),
        "codex_wrapper.py": ("TRIAD_CODEX_BIN",),
        "gemini_wrapper.py": ("TRIAD_GEMINI_BIN",),
        "antigravity_wrapper.py": ("TRIAD_AGY_BIN",),
        "review_round.py": ("TRIAD_AGY_BIN", "TRIAD_GEMINI_BIN"),
    }
    if kind == "launcher":
        if name not in vendor_envs:
            return False
    elif kind == "runtime":
        if name not in {"triad-setup", "triad-doctor"}:
            return False
    else:
        raise Refusal(f"unknown managed command kind for {name}: {kind}")

    try:
        text = data.decode("utf-8")
        lines = text.splitlines()
        tree = ast.parse(text)
    except (UnicodeDecodeError, SyntaxError, ValueError):
        return False

    accepted_launcher_markers = {
        PUBLIC_LAUNCHER_MARKER.decode("ascii"),
        LEGACY_PUBLIC_LAUNCHER_MARKER.decode("ascii"),
    }
    accepted_runtime_markers = {
        PUBLIC_RUNTIME_MARKER.decode("ascii"),
        LEGACY_PUBLIC_RUNTIME_MARKER.decode("ascii"),
    }

    if (
        len(lines) < 5
        or not lines[0].startswith("#!")
        or (kind == "launcher" and lines[1] not in accepted_launcher_markers)
        or (kind == "runtime" and lines[1] not in accepted_runtime_markers)
        or len(tree.body) < 3
        or not _exact_import(tree.body[0], "os")
        or not _exact_import(tree.body[1], "sys")
    ):
        return False
    body = tree.body[2:]
    if kind == "runtime":
        shebang_python = _shebang_python(lines[0], isolated=True)
        return shebang_python is not None and _managed_runtime_ast(
            name, shebang_python, body
        )
    if lines[0].endswith(" -E"):
        shebang_python = _shebang_python(lines[0], isolated=True)
        return shebang_python is not None and _managed_hardened_launcher_ast(
            name, vendor_envs[name], shebang_python, body
        )
    if len(vendor_envs[name]) != 1:
        return False
    shebang_python = _shebang_python(lines[0], isolated=False)
    return shebang_python is not None and _managed_legacy_launcher_ast(
        name, vendor_envs[name][0], shebang_python, body
    )


def _shebang_python(shebang: str, *, isolated: bool) -> str | None:
    suffix = " -E" if isolated else ""
    if not shebang.startswith("#!") or not shebang.endswith(suffix):
        return None
    runtime = shebang[2 : len(shebang) - len(suffix) if suffix else None]
    if not runtime or not os.path.isabs(runtime) or any(
        char.isspace() for char in runtime
    ):
        return None
    return runtime


def _exact_import(node: ast.stmt, module: str) -> bool:
    return (
        isinstance(node, ast.Import)
        and len(node.names) == 1
        and node.names[0].name == module
        and node.names[0].asname is None
    )


def _string_assignment(
    node: ast.stmt, *, owner: str, key: str
) -> str | None:
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return None
    target = node.targets[0]
    if not isinstance(target, ast.Subscript):
        return None
    if owner == "env":
        owner_ok = isinstance(target.value, ast.Name) and target.value.id == "env"
    else:
        owner_ok = (
            isinstance(target.value, ast.Attribute)
            and isinstance(target.value.value, ast.Name)
            and target.value.value.id == "os"
            and target.value.attr == "environ"
        )
    if (
        not owner_ok
        or not isinstance(target.slice, ast.Constant)
        or target.slice.value != key
        or not isinstance(node.value, ast.Constant)
        or not isinstance(node.value.value, str)
    ):
        return None
    return node.value.value


def _matches_expression(node: ast.AST, source: str) -> bool:
    expected = ast.parse(source, mode="eval").body
    return ast.dump(node, include_attributes=False) == ast.dump(
        expected, include_attributes=False
    )


def _exec_call(
    node: ast.stmt, function: str, *, env_arg: bool
) -> tuple[str, list[str]] | None:
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return None
    call = node.value
    expected_args = 3 if env_arg else 2
    if (
        call.keywords
        or len(call.args) != expected_args
        or not isinstance(call.func, ast.Attribute)
        or not isinstance(call.func.value, ast.Name)
        or call.func.value.id != "os"
        or call.func.attr != function
        or not isinstance(call.args[0], ast.Constant)
        or not isinstance(call.args[0].value, str)
        or not isinstance(call.args[1], ast.BinOp)
        or not isinstance(call.args[1].op, ast.Add)
        or not isinstance(call.args[1].left, ast.List)
        or not _matches_expression(call.args[1].right, "sys.argv[1:]")
    ):
        return None
    if env_arg and not (
        isinstance(call.args[2], ast.Name) and call.args[2].id == "env"
    ):
        return None
    argv: list[str] = []
    for item in call.args[1].left.elts:
        if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
            return None
        argv.append(item.value)
    return call.args[0].value, argv


def _managed_hardened_launcher_ast(
    name: str,
    vendor_envs: tuple[str, ...],
    shebang_python: str,
    body: list[ast.stmt],
) -> bool:
    if len(body) not in {
        5 + len(vendor_envs),
        6 + len(vendor_envs),
        7 + len(vendor_envs),
    }:
        return False
    scrub = body[0]
    expected_scrub = (
        "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "LD_DEBUG",
        "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH",
        "NODE_OPTIONS", "NODE_PATH", "PYTHONPATH", "PYTHONHOME",
        "PYTHONSTARTUP", "BASH_ENV", "ENV", "PERL5LIB", "RUBYOPT", "RUBYLIB",
    )
    if not (
        isinstance(scrub, ast.Assign)
        and len(scrub.targets) == 1
        and isinstance(scrub.targets[0], ast.Name)
        and scrub.targets[0].id == "_SCRUB"
        and isinstance(scrub.value, ast.Tuple)
        and tuple(
            item.value
            for item in scrub.value.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        ) == expected_scrub
        and len(scrub.value.elts) == len(expected_scrub)
    ):
        return False
    env_assign = body[1]
    if not (
        isinstance(env_assign, ast.Assign)
        and len(env_assign.targets) == 1
        and isinstance(env_assign.targets[0], ast.Name)
        and env_assign.targets[0].id == "env"
        and _matches_expression(
            env_assign.value,
            "{k: v for k, v in os.environ.items() if k not in _SCRUB}",
        )
    ):
        return False
    index = 2
    path_value = _string_assignment(body[index], owner="env", key="PATH")
    if not path_value:
        return False
    index += 1
    audit_redaction = _string_assignment(
        body[index], owner="env", key="TRIAD_AUDIT_REDACT_PROMPTS"
    )
    if audit_redaction is not None:
        if audit_redaction != "1":
            return False
        index += 1
    classifier = _string_assignment(
        body[index], owner="env", key="TRIAD_CLASSIFIER_EXTENSION"
    )
    if classifier is not None:
        if not classifier or not os.path.isabs(classifier):
            return False
        index += 1
    if _string_assignment(
        body[index], owner="env", key="TRIAD_REQUIRE_PINNED_VENDOR"
    ) != "1":
        return False
    index += 1
    for vendor_env in vendor_envs:
        vendor_value = _string_assignment(body[index], owner="env", key=vendor_env)
        if vendor_value is not None:
            if not vendor_value or not os.path.isabs(vendor_value):
                return False
        else:
            pop_node = body[index]
            if not (
                isinstance(pop_node, ast.Expr)
                and _matches_expression(
                    pop_node.value, f'env.pop("{vendor_env}", None)'
                )
            ):
                return False
        index += 1
    if index != len(body) - 1:
        return False
    executed = _exec_call(body[index], "execve", env_arg=True)
    if executed is None:
        return False
    python, argv = executed
    return (
        python == shebang_python
        and len(argv) == 3
        and argv[0] == python
        and argv[1] == "-E"
        and os.path.isabs(argv[2])
        and argv[2].endswith(f"/bin/{name}")
    )


def _managed_legacy_launcher_ast(
    name: str,
    vendor_env: str,
    shebang_python: str,
    body: list[ast.stmt],
) -> bool:
    if len(body) not in {1, 2, 3}:
        return False
    assignments = body[:-1]
    seen: set[str] = set()
    for node in assignments:
        require_value = _string_assignment(
            node, owner="os.environ", key="TRIAD_REQUIRE_PINNED_VENDOR"
        )
        vendor_value = _string_assignment(node, owner="os.environ", key=vendor_env)
        if require_value is not None:
            if require_value != "1" or "require" in seen:
                return False
            seen.add("require")
        elif vendor_value is not None:
            if not vendor_value or not os.path.isabs(vendor_value) or "vendor" in seen:
                return False
            seen.add("vendor")
        else:
            return False
    required_pair = {"require", "vendor"}
    if name == "gemini_wrapper.py":
        if seen not in (set(), required_pair):
            return False
    elif seen != required_pair:
        return False
    executed = _exec_call(body[-1], "execv", env_arg=False)
    if executed is None:
        return False
    python, argv = executed
    return (
        python == shebang_python
        and len(argv) == 2
        and argv[0] == python
        and os.path.isabs(argv[1])
        and argv[1].endswith(f"/bin/{name}")
    )


def _managed_runtime_ast(
    name: str, shebang_python: str, body: list[ast.stmt]
) -> bool:
    if len(body) != 1:
        return False
    executed = _exec_call(body[0], "execv", env_arg=False)
    if executed is None:
        return False
    python, argv = executed
    command = name.removeprefix("triad-")
    return (
        python == shebang_python
        and len(argv) == 4
        and argv[0] == python
        and argv[1] == "-E"
        and os.path.isabs(argv[2])
        and argv[2].endswith("/bin/triad_runtime.py")
        and argv[3] == command
    )


def _command_is_managed(artifact: CommandArtifact, state: State) -> bool:
    return command_data_is_managed(artifact.name, artifact.kind, state.data)


def command_ownership_state(path: Path, name: str, kind: str) -> str:
    require_safe_ancestors(path)
    state = read_state(path)
    if state is None:
        return "absent"
    return "managed" if command_data_is_managed(name, kind, state.data) else "unmanaged"


def _preflight_command_group(
    artifacts: list[CommandArtifact], *, removing: bool, preserve_foreign: bool = False
) -> list[State | None]:
    seen: set[Path] = set()
    before: list[State | None] = []
    for artifact in artifacts:
        if not isinstance(artifact.name, str) or not artifact.name:
            raise Refusal(f"invalid managed command artifact: {artifact.name!r}")
        if artifact.kind not in {"launcher", "runtime"}:
            raise Refusal(f"unknown managed command kind for {artifact.name}: {artifact.kind}")
        if artifact.target in seen:
            raise Refusal(f"duplicate managed command target: {artifact.target}")
        seen.add(artifact.target)
        if not isinstance(artifact.data, bytes):
            raise Refusal(f"command payload must be bytes: {artifact.name}")
        require_safe_ancestors(artifact.target)
        try:
            state = read_state(artifact.target)
        except Refusal:
            if preserve_foreign and removing:
                before.append(None)
                continue
            raise
        if state is not None and not _command_is_managed(artifact, state):
            if preserve_foreign and removing:
                before.append(None)
                continue
            action = "remove" if removing else "overwrite"
            raise Refusal(f"refusing to {action} unmanaged command: {artifact.target}")
        before.append(state)
    return before


def install_command_group(artifacts: list[CommandArtifact]) -> None:
    before = _preflight_command_group(artifacts, removing=False)
    temps: list[Staged] = []
    journal: list[Mutation] = []
    failure: BaseException | None = None
    try:
        for artifact in artifacts:
            temps.append(stage(artifact.target, artifact.data, artifact.mode))
        for artifact, temp, expected in zip(artifacts, temps, before):
            publish_to(temp, artifact.target, expected, journal)
    except BaseException as error:
        failure = error
    cleanup_failures = cleanup_all(temps)
    finalize_transaction(failure, journal, cleanup_failures)


def remove_command_group(
    artifacts: list[CommandArtifact],
    *,
    test_fail_at: str | None = None,
    preserve_foreign: bool = False,
) -> None:
    before = _preflight_command_group(
        artifacts, removing=True, preserve_foreign=preserve_foreign
    )
    journal: list[Mutation] = []
    failure: BaseException | None = None
    try:
        for artifact, expected in zip(artifacts, before):
            if expected is not None:
                if test_fail_at == artifact.name:
                    raise OSError(f"injected command removal failure: {artifact.name}")
                remove_state(expected, journal)
    except BaseException as error:
        failure = error
    finalize_transaction(failure, journal, [])


def command_artifacts_from_manifest(path: Path, *, require_data: bool) -> list[CommandArtifact]:
    artifacts: list[CommandArtifact] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise Refusal(f"could not read command manifest: {path}") from error
    for line in lines:
        try:
            item = json.loads(line)
            name = item["name"]
            kind = item["kind"]
            target = Path(item["target"])
            mode = int(item.get("mode", 0o755))
            data_path = item.get("data_path")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise Refusal(f"invalid command manifest entry in {path}") from error
        if require_data:
            if not isinstance(data_path, str):
                raise Refusal(f"missing command payload for {name}")
            try:
                data = Path(data_path).read_bytes()
            except OSError as error:
                raise Refusal(f"could not read command payload for {name}") from error
        else:
            data = b""
        artifacts.append(CommandArtifact(name, target, data, kind, mode))
    return artifacts


def portable_python_shebang(python: Path) -> bytes:
    runtime = os.fspath(python)
    if any(char.isspace() for char in runtime):
        raise Refusal(
            f"portable generated shebang cannot encode this Python runtime path: {python}"
        )
    shebang = b"#!" + os.fsencode(runtime) + b" -E\n"
    if len(shebang) > 256:
        raise Refusal(
            "portable generated shebang exceeds 256 filesystem bytes for "
            f"Python runtime path: {python}"
        )
    return shebang


def runtime_path() -> Path:
    runtime = Path(sys.executable).resolve()
    require_safe_ancestors(runtime)
    if read_state(runtime) is None:
        raise Refusal(f"missing resolved Python runtime: {runtime}")
    portable_python_shebang(runtime)
    return runtime


def verify_library(root: Path | None = None) -> dict:
    target_root = root if root is not None else Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(target_root / "bin"))
    import library_adoption
    return library_adoption.verify_library(root=target_root)


def formal_schema_dependency_ready(requirements: Path) -> Path:
    runtime = runtime_path()
    if not requirements.is_absolute() or not requirements.is_file():
        raise Refusal(f"requirements file is unavailable: {requirements}")
    requirements = requirements.resolve()
    root = requirements.parent
    try:
        verify_library(root)
    except Exception as error:
        raise Refusal(f"Bundled shared library verification failed: {error}") from error
    try:
        import pydantic
        from pydantic import (
            BaseModel,
            ConfigDict,
            Field,
            StringConstraints,
            ValidationInfo,
            field_validator,
            model_validator,
        )

        version = str(getattr(pydantic, "VERSION", "0"))
        if version.split(".", 1)[0] != "2":
            raise RuntimeError(f"unsupported Pydantic version {version}")
        for attribute in ("model_validate", "model_validate_json", "model_json_schema"):
            if not hasattr(BaseModel, attribute):
                raise RuntimeError(f"Pydantic BaseModel lacks {attribute}")
        if not all(
            item is not None
            for item in (
                ConfigDict,
                Field,
                StringConstraints,
                ValidationInfo,
                field_validator,
                model_validator,
            )
        ):
            raise RuntimeError("Pydantic 2 validator API is incomplete")
    except (ImportError, AttributeError, RuntimeError, TypeError, ValueError) as error:
        owner_command = shlex.join(
            [
                str(runtime),
                "-m",
                "pip",
                "install",
                "-r",
                str(requirements),
            ]
        )
        raise Refusal(
            "Pydantic 2 formal review APIs are required. Run this in the owner "
            f"terminal, then rerun bootstrap: {owner_command}"
        ) from error
    try:
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012

        registry = Registry().with_resource(
            "urn:triad-bootstrap:readiness",
            Resource.from_contents({"type": "object"}, default_specification=DRAFT202012),
        )
        Draft202012Validator.check_schema({"type": "object"})
        if not Draft202012Validator({"type": "object"}, registry=registry).is_valid({}):
            raise RuntimeError("jsonschema validator API is incomplete")
    except (ImportError, AttributeError, RuntimeError, TypeError, ValueError) as error:
        owner_command = shlex.join(
            [str(runtime), "-m", "pip", "install", "-r", str(requirements)]
        )
        raise Refusal(
            "jsonschema Draft 2020-12 APIs are required. Run this in the owner "
            f"terminal, then rerun bootstrap: {owner_command}"
        ) from error
    try:
        from validate_v2 import _library

        shared = _library()
        shared.validate_binding(dict(review_id="bootstrap-readiness", family="codex",
                                     content_digest="a" * 64, leg_name="codex", attempt=1,
                                     route=None))
        shared.load_roster_contract()
        shared.render_review_clauses(
            family="codex", host="B", review_date="2026-10-13",
            review_web_authorized=False,
            replacements={name: "bootstrap-readiness" for name in (
                "worktree", "brief-file", "packet-files", "review-id",
                "content-digest", "leg-name", "attempt")},
        )
    except (ImportError, OSError, AttributeError, RuntimeError, TypeError, ValueError) as error:
        detail = ""
        if isinstance(error, RuntimeError) and isinstance(error.__cause__, ValueError):
            detail = f" Details: {error.__cause__}."
        raise Refusal(
            "Bundled shared library readiness failed." + detail + " Restore the exact plugin "
            "package, then rerun bootstrap."
        ) from error
    return runtime


def _require_writable_state(state: State, label: str) -> None:
    flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(state.path, flags)
    except OSError as error:
        raise Refusal(f"{label} is not writable: {state.path}") from error
    try:
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode) or _ident(current) != state.ident:
            raise Refusal(f"{label} changed while checking writability: {state.path}")
    finally:
        os.close(fd)


def _classifier_state(path: Path) -> State | None:
    require_safe_ancestors(path)
    state = read_state(path)
    if state is None:
        return None
    try:
        json.loads(state.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Refusal(f"classifier file is not valid JSON: {path}") from error
    _require_writable_state(state, "classifier file")
    return state


def preflight_classifier(path: Path) -> str:
    return "ready" if _classifier_state(path) is not None else "absent"


def ensure_classifier(path: Path) -> str:
    if os.environ.get("TRIAD_BOOTSTRAP_TEST_FAIL_CLASSIFIER_ENSURE") == "1":
        raise Refusal("injected classifier ensure failure")
    swap_target = os.environ.get(
        "TRIAD_BOOTSTRAP_TEST_SWAP_CLASSIFIER_TO_SYMLINK_BEFORE_ENSURE"
    )
    if swap_target:
        require_safe_ancestors(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        require_safe_ancestors(path)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        os.symlink(swap_target, path)
    before = _classifier_state(path)
    if before is not None:
        return "ready"
    _publish_single(path, b"{}\n", None, creation_mode())
    created = _classifier_state(path)
    if created is None:
        raise Refusal(f"classifier file was not created: {path}")
    return "created"


def _legacy_path_assignment(line: str, access: str) -> str | None:
    suffix = f' = "{access}"'
    if not line.endswith(suffix):
        return None
    literal = line[: -len(suffix)]
    try:
        value = ast.literal_eval(literal)
    except (SyntaxError, ValueError):
        return None
    if not isinstance(value, str) or not Path(value).is_absolute():
        return None
    if value.startswith("//") or os.path.normpath(value) != value:
        return None
    canonical = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value if line == canonical + suffix else None


def _legacy_profile_data_is_owned(data: bytes) -> bool:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if not text.endswith("\n"):
        return False
    lines = text[:-1].split("\n")
    if len(lines) < 28:
        return False
    approval = lines[11]
    if approval not in {
        'approval_policy = "never"',
        'approval_policy = "on-request"',
        'approval_policy = "untrusted"',
    }:
        return False
    expected_prefix = [
        PROFILE_MARKER.decode("ascii"),
        "# Generated by scripts/bootstrap.sh --install.",
        "# Re-run with TRIAD_BOOTSTRAP_INSTALL_CODEX_PROFILE=1 to refresh.",
        "# Explicit external-CLI consent profile: triad dispatch may send relevant",
        "# prompt/repo review material to authenticated claude, agy, and gemini CLIs.",
        "# Permission-profile system (developers.openai.com/codex/permissions).",
        "# Do NOT reintroduce legacy sandbox_mode / [sandbox_workspace_write] in this",
        "# or any loaded config layer: legacy sandbox settings disable",
        "# default_permissions, which would neutralize the triad_leader permission",
        "# profile's scoping.",
        "",
        approval,
        'approvals_reviewer = "auto_review"',
        'default_permissions = "triad_leader"',
        "",
        "[permissions.triad_leader]",
        'description = "Triad leader session: workspace writes plus triad runtime dirs; network on."',
        'extends = ":workspace"',
        "",
        "[permissions.triad_leader.filesystem]",
        "# --- SEC-3 exec-target write-denies (wrapper .py / launchers / python3 / vendor CLIs) ---",
    ]
    if lines[: len(expected_prefix)] != expected_prefix:
        return False
    reallow = (
        "# --- re-allows: log_dir/debug_dir are nested under bin_dir "
        "(more-specific-wins survives that deny); classifier_dir is a separate, "
        "non-nested directory allowed independently ---"
    )
    try:
        reallow_index = lines.index(reallow, len(expected_prefix))
    except ValueError:
        return False
    read_lines = lines[len(expected_prefix) : reallow_index]
    if lines[-3:] != [
        "",
        "[permissions.triad_leader.network]",
        "enabled = true",
    ]:
        return False
    write_lines = lines[reallow_index + 1 : -3]
    if not 3 <= len(read_lines) <= 6 or len(write_lines) != 3:
        return False
    read_paths = [_legacy_path_assignment(line, "read") for line in read_lines]
    write_paths = [_legacy_path_assignment(line, "write") for line in write_lines]
    if any(path is None for path in (*read_paths, *write_paths)):
        return False
    paths = [path for path in (*read_paths, *write_paths) if path is not None]
    if len(paths) != len(set(paths)):
        return False
    bin_directory = Path(paths[0])
    launcher_directory = Path(paths[1])
    if bin_directory.name != "bin":
        return False
    python_name = Path(paths[2]).name
    python_name_parts = python_name.split(".")
    if python_name_parts[0] != "python3" or any(
        not part.isdecimal() for part in python_name_parts[1:]
    ):
        return False
    historical_vendor_order = {"claude": 0, "gemini": 1, "agy": 2}
    vendor_names = [Path(path).name for path in paths[3 : len(read_paths)]]
    if any(name not in historical_vendor_order for name in vendor_names):
        return False
    if len(vendor_names) != len(set(vendor_names)):
        return False
    vendor_order = [historical_vendor_order[name] for name in vendor_names]
    if vendor_order != sorted(vendor_order):
        return False
    classifier_directory, log_directory, debug_directory = map(
        Path, paths[len(read_paths) :]
    )
    if any(
        directory.resolve() in _LEGACY_PROFILE_BROAD_DIRECTORY_ROOTS
        for directory in (
            bin_directory,
            launcher_directory,
            classifier_directory,
        )
    ):
        return False
    if log_directory != bin_directory / "_logs":
        return False
    if debug_directory != bin_directory / "_debug":
        return False
    return classifier_directory not in {bin_directory, log_directory, debug_directory}


def _canonical_legacy_string_literal(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _legacy_rules_data_is_owned(data: bytes) -> bool:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if not text.endswith("\n"):
        return False
    sections = text[:-1].split("\n\n")
    header = "\n".join(
        (
            RULES_MARKER.decode("ascii"),
            "# Generated by scripts/bootstrap.sh --install.",
            "# Re-run with TRIAD_BOOTSTRAP_INSTALL_CODEX_RULES=1 to refresh.",
            "# These rules prompt on wrapper-specific command prefixes for approval review.",
            "# They do not allow broad shell entrypoints such as bash -lc or zsh -lc.",
        )
    )
    if len(sections) != 4 or sections[0] != header:
        return False
    wrappers = (
        ("claude_wrapper.py", "Claude wrapper"),
        ("antigravity_wrapper.py", "Antigravity wrapper"),
        ("gemini_wrapper.py", "Gemini business-tier wrapper"),
    )
    launcher_directory: Path | None = None
    repository_root: Path | None = None
    for section, (wrapper, label) in zip(sections[1:], wrappers, strict=True):
        lines = section.split("\n")
        if len(lines) != 18:
            return False
        pattern_prefix = "    pattern = [["
        pattern_suffix = "]],"
        if not lines[1].startswith(pattern_prefix) or not lines[1].endswith(
            pattern_suffix
        ):
            return False
        try:
            launcher = ast.literal_eval(
                lines[1][len(pattern_prefix) : -len(pattern_suffix)]
            )
        except (SyntaxError, ValueError):
            return False
        if not isinstance(launcher, str):
            return False
        launcher_path = Path(launcher)
        if not launcher_path.is_absolute() or launcher_path.name != wrapper:
            return False
        if lines[1] != (
            f"    pattern = [[{_canonical_legacy_string_literal(launcher)}]],"
        ):
            return False
        if launcher_directory is None:
            launcher_directory = launcher_path.parent
        elif launcher_path.parent != launcher_directory:
            return False

        def decoded(index: int) -> str | None:
            line = lines[index]
            if not line.startswith("        ") or not line.endswith(","):
                return None
            literal = line[8:-1]
            try:
                value = ast.literal_eval(literal)
            except (SyntaxError, ValueError):
                return None
            if not isinstance(value, str) or line != (
                f"        {_canonical_legacy_string_literal(value)},"
            ):
                return None
            return value

        repo_command = decoded(10)
        prompt_command = decoded(6)
        if repo_command is None or prompt_command is None:
            return False
        try:
            repo_argv = shlex.split(repo_command)
            prompt_argv = shlex.split(prompt_command)
        except ValueError:
            return False
        if len(repo_argv) != 5 or repo_argv[1:] != [
            "--prompt",
            "hi",
            "--sandbox",
            "read-only",
        ]:
            return False
        repo_wrapper = Path(repo_argv[0])
        if (
            not repo_wrapper.is_absolute()
            or repo_wrapper.name != wrapper
            or repo_wrapper.parent.name != "bin"
        ):
            return False
        current_root = repo_wrapper.parent.parent
        if repository_root is None:
            repository_root = current_root
        elif current_root != repository_root:
            return False
        prompt_path = current_root / "_runs" / "prompts" / "triad-prompt.txt"
        if prompt_argv != [
            launcher,
            "--prompt-file",
            str(prompt_path),
            "--sandbox",
            "read-only",
        ]:
            return False
        justification = (
            "Require approval review; approve only an owner-authorized triad review "
            f"through the {label} when the worktree, scope, and named provider match "
            "the owner's request and provider-visible input excludes credentials, "
            "tokens, cookies, authentication files, environment dumps, provider "
            "logs, and unrelated paths. This does not authorize "
            "commit, push, install, merge, or release."
        )
        expected_values = {
            5: shlex.join([launcher, "--prompt", "hi", "--sandbox", "read-only"]),
            6: shlex.join(
                [
                    launcher,
                    "--prompt-file",
                    str(prompt_path),
                    "--sandbox",
                    "read-only",
                ]
            ),
            9: f"{wrapper} --prompt hi --sandbox read-only",
            10: shlex.join(
                [str(repo_wrapper), "--prompt", "hi", "--sandbox", "read-only"]
            ),
            11: f"bash -lc {wrapper} --prompt hi",
            12: f"zsh -lc {wrapper} --prompt hi",
            13: shlex.join(
                ["python3", str(repo_wrapper), "--prompt", "hi", "--sandbox", "read-only"]
            ),
            14: shlex.join(
                [
                    "/usr/bin/env",
                    "python3",
                    str(repo_wrapper),
                    "--prompt",
                    "hi",
                    "--sandbox",
                    "read-only",
                ]
            ),
            15: "python3 -c print('not a triad wrapper')",
        }
        if lines[0] != "prefix_rule(" or lines[2] != '    decision = "prompt",':
            return False
        if lines[3] != (
            "    justification = "
            f"{_canonical_legacy_string_literal(justification)},"
        ):
            return False
        if lines[4] != "    match = [" or lines[7:9] != ["    ],", "    not_match = ["]:
            return False
        if lines[16:] != ["    ],", ")"]:
            return False
        if any(decoded(index) != value for index, value in expected_values.items()):
            return False
    return True


def _managed_artifact_data_is_owned(data: bytes, kind: str) -> bool:
    if kind == "profile":
        return _legacy_profile_data_is_owned(data)
    if kind == "rules":
        return _legacy_rules_data_is_owned(data)
    raise Refusal(f"unknown managed artifact kind: {kind}")


def current_config_fragment(newline: bytes) -> bytes:
    return (
        CONFIG_FRAGMENT_BEGIN
        + newline
        + CURRENT_CONFIG_FRAGMENT_TEXT.replace(b"\n", newline)
        + CONFIG_FRAGMENT_END
        + newline
    )


def legacy_config_fragment(newline: bytes) -> bytes:
    return (
        LEGACY_CONFIG_FRAGMENT_BEGIN
        + newline
        + LEGACY_CONFIG_FRAGMENT_TEXT.replace(b"\n", newline)
        + LEGACY_CONFIG_FRAGMENT_END
        + newline
    )


def _parsed_shared_config(state: State, path: Path) -> dict | None:
    try:
        text = state.data.decode("utf-8")
        data = tomllib.loads(text) if state.data else {}
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    return data if isinstance(data, dict) else None


def remove_config_fragment(path: Path, *, preserve_empty: bool = False) -> str:
    if os.environ.get("TRIAD_BOOTSTRAP_TEST_FAIL_CONFIG_FRAGMENT_REMOVE") == "1":
        raise Refusal("injected config fragment remove failure")
    require_safe_ancestors(path)
    before = read_state(path)
    if before is None:
        return "absent"
    existing = before.data
    data = _parsed_shared_config(before, path)
    if data is None:
        return "unrecognized-managed"
    if (
        CONFIG_FRAGMENT_BEGIN not in existing
        and CONFIG_FRAGMENT_END not in existing
        and LEGACY_CONFIG_FRAGMENT_BEGIN not in existing
        and LEGACY_CONFIG_FRAGMENT_END not in existing
    ):
        return "not-managed"

    current_blocks = [current_config_fragment(nl) for nl in (b"\n", b"\r\n")]
    legacy_blocks = [legacy_config_fragment(nl) for nl in (b"\n", b"\r\n")]
    current_policy = tomllib.loads(CURRENT_CONFIG_FRAGMENT_TEXT.decode("utf-8"))[
        "shell_environment_policy"
    ]
    exact_current = (
        data.get("shell_environment_policy") == current_policy
        and sum(existing.count(candidate) for candidate in current_blocks) == 1
    )
    exact_legacy = (
        data.get("shell_environment_policy") == {"inherit": "core"}
        and sum(existing.count(candidate) for candidate in legacy_blocks) == 1
    )
    if exact_current:
        managed = next(candidate for candidate in current_blocks if existing.count(candidate) == 1)
    elif exact_legacy:
        managed = next(candidate for candidate in legacy_blocks if existing.count(candidate) == 1)
    else:
        return "unrecognized-managed"

    managed_start = existing.find(managed)
    separator_start = managed_start - len(CONFIG_FRAGMENT_INSERTED_SEPARATOR)
    if (
        separator_start >= 0
        and existing[separator_start:managed_start]
        == CONFIG_FRAGMENT_INSERTED_SEPARATOR
    ):
        managed = CONFIG_FRAGMENT_INSERTED_SEPARATOR + managed
    remainder = existing.replace(managed, b"", 1)
    if not remainder:
        if preserve_empty:
            _publish_single(path, b"", before, before.mode)
            return "removed"
        _remove_single(before)
        return "removed-file"
    _publish_single(path, remainder, before, before.mode)
    return "removed"


def _shell_entry_span(data: bytes, path: Path) -> tuple[int, int] | None:
    spans: list[tuple[int, int, bytes]] = []
    offset = 0
    for line in data.splitlines(keepends=True):
        content = line
        if content.endswith(b"\r\n"):
            content = content[:-2]
        elif content.endswith((b"\n", b"\r")):
            content = content[:-1]
        spans.append((offset, offset + len(line), content))
        offset += len(line)

    begin_spans = [span for span in spans if span[2] in (SHELL_ENTRY_BEGIN, LEGACY_SHELL_ENTRY_BEGIN)]
    end_spans = [span for span in spans if span[2] in (SHELL_ENTRY_END, LEGACY_SHELL_ENTRY_END)]
    raw_begin_count = data.count(SHELL_ENTRY_BEGIN) + data.count(LEGACY_SHELL_ENTRY_BEGIN)
    raw_end_count = data.count(SHELL_ENTRY_END) + data.count(LEGACY_SHELL_ENTRY_END)
    if raw_begin_count == 0 and raw_end_count == 0:
        return None
    valid = (
        raw_begin_count == 1
        and raw_end_count == 1
        and len(begin_spans) == 1
        and len(end_spans) == 1
        and begin_spans[0][0] < end_spans[0][0]
    )
    if not valid:
        raise Refusal(f"malformed managed shell markers in {path}")
    return begin_spans[0][0], end_spans[0][1]


def _shell_entry_state(path: Path) -> tuple[State | None, tuple[int, int] | None]:
    require_safe_ancestors(path)
    before = read_state(path)
    if before is None:
        return None, None
    span = _shell_entry_span(before.data, path)
    return before, span


def _shell_entry_base(
    before: State | None, span: tuple[int, int] | None
) -> bytes:
    existing = before.data if before is not None else b""
    if span is None:
        return existing
    return existing[: span[0]] + existing[span[1] :]


def _historical_shell_entry_is_managed(data: bytes) -> bool:
    if hashlib.sha256(data).hexdigest() == FROZEN_SHELL_ENTRY_SHA256:
        return True
    lines = data.splitlines(keepends=True)
    if len(lines) != 11 or any(not line.endswith(b"\n") for line in lines):
        return False
    content = [line[:-1] for line in lines]
    if (content[0] in (SHELL_ENTRY_BEGIN, LEGACY_SHELL_ENTRY_BEGIN)) and (content[10] in (SHELL_ENTRY_END, LEGACY_SHELL_ENTRY_END)):
        return True
    return False


def update_shell_entry(path: Path) -> str:
    before, span = _shell_entry_state(path)
    existing = before.data if before is not None else b""
    if span is None:
        if before is not None and (b"codex-triad" in existing or b"agy-triad" in existing):
            return "unmanaged"
        return "absent"
    managed = existing[span[0] : span[1]]
    if not _historical_shell_entry_is_managed(managed):
        return "unmanaged"

    transformed = _shell_entry_base(before, span)

    temp: Staged | None = None
    journal: list[Mutation] = []
    failure: BaseException | None = None
    try:
        mode = before.mode if before is not None else creation_mode()
        temp = stage(path, transformed, mode)
        swap_source = os.environ.get(
            "TRIAD_BOOTSTRAP_TEST_SWAP_SHELL_RC_BEFORE_PUBLISH"
        )
        if swap_source:
            replacement = Path(swap_source)
            if read_state(replacement) is None:
                raise Refusal(f"missing shell RC replacement test input: {replacement}")
            os.replace(replacement, path)
        publish_to(temp, path, before, journal)
    except BaseException as error:
        failure = error
    cleanup_failures = cleanup_all([temp] if temp is not None else [])
    finalize_transaction(failure, journal, cleanup_failures)
    return "removed"


def cleanup(temp: Staged | None) -> None:
    if temp is not None:
        _delete_expected(temp.state)


def cleanup_all(temps: list[Staged | None]) -> list[BaseException]:
    failures: list[BaseException] = []
    for temp in temps:
        try:
            cleanup(temp)
        except BaseException as error:
            failures.append(error)
    return failures


def rollback_mutation(mutation: Mutation) -> None:
    current = read_state(mutation.target)

    if mutation.before is None:
        if current is None:
            return
        if mutation.after is not None and _state_matches(current, mutation.after):
            _delete_expected(current)
        return

    if mutation.backup is None or mutation.backup_dir is None:
        raise Refusal(f"missing rollback recovery for {mutation.target}")
    backup = read_state(mutation.backup.path)
    if backup is None:
        if _state_matches(current, mutation.before):
            _remove_private_directory(mutation.backup_dir)
            return
        _remove_private_directory(mutation.backup_dir)
        raise Refusal(f"missing rollback recovery for {mutation.target}")

    if current is None:
        _restore_no_clobber(backup, mutation.target, mutation.backup_dir)
        fsync_parent(mutation.target)
        return
    if _state_matches(current, mutation.before):
        _cleanup_private_state(backup, mutation.backup_dir)
        fsync_parent(mutation.target)
        return
    if mutation.after is None or not _state_matches(current, mutation.after):
        raise Refusal(
            f"could not restore path without overwriting foreign bytes; "
            f"recovery retained at {mutation.backup.path}"
        )

    claimed_after, claimed_after_dir = _claim_expected(
        mutation.target, mutation.after
    )
    _restore_no_clobber(backup, mutation.target, mutation.backup_dir)
    _cleanup_private_state(claimed_after, claimed_after_dir)
    fsync_parent(mutation.target)


def rollback_all(journal: list[Mutation]) -> list[BaseException]:
    failures: list[BaseException] = []
    for mutation in reversed(journal):
        try:
            rollback_mutation(mutation)
        except BaseException as error:
            failures.append(error)
    return failures


def commit_all(journal: list[Mutation]) -> list[BaseException]:
    failures: list[BaseException] = []
    for mutation in journal:
        if mutation.backup is None:
            continue
        try:
            _cleanup_private_state(mutation.backup, mutation.backup_dir)
        except BaseException as error:
            failures.append(error)
    return failures


def reraise_after_rollback(
    error: BaseException, journal: list[Mutation], cleanup_failures: list[BaseException]
) -> None:
    failures = rollback_all(journal) + cleanup_failures
    if failures:
        raise TransactionFailure(error, failures) from error
    raise error


def finalize_transaction(
    failure: BaseException | None,
    journal: list[Mutation],
    cleanup_failures: list[BaseException],
) -> None:
    if failure is not None:
        reraise_after_rollback(failure, journal, cleanup_failures)
    if cleanup_failures:
        primary, *additional = cleanup_failures
        reraise_after_rollback(primary, journal, additional)
    commit_failures = commit_all(journal)
    if commit_failures:
        raise TransactionFailure(
            Refusal("transaction committed but backup cleanup failed"),
            commit_failures,
        )


def prepare_remove(args: argparse.Namespace) -> RepairRemovePlan:
    config, analyzer, launcher = Path(args.config), Path(args.analyzer), Path(args.launcher)
    for managed_path in (config, analyzer, launcher):
        require_safe_ancestors(managed_path)
    try:
        config_before = read_state(config)
    except Refusal as error:
        raise Refusal(f"refusing unsafe repair config: {config}") from error
    try:
        analyzer_before = read_state(analyzer)
    except Refusal as error:
        raise Refusal(f"refusing unsafe repair analyzer: {analyzer}") from error
    try:
        launcher_before = read_state(launcher)
    except Refusal as error:
        raise Refusal(f"refusing unsafe repair apply launcher: {launcher}") from error
    original = parse_text(config_before, config)
    parsed_config(original, config)
    before, after, managed_registration, original_config_existed = split_registration(
        original, config, analyzer
    )
    base = before + after
    if managed_registration and not original_config_existed and base:
        managed_fragments = {
            current_config_fragment(b"\r\n").decode("utf-8"),
            current_config_fragment(b"\n").decode("utf-8"),
            legacy_config_fragment(b"\r\n").decode("utf-8"),
            legacy_config_fragment(b"\n").decode("utf-8"),
        }
        if base not in managed_fragments:
            original_config_existed = True
    agents = parsed_config(base, config).get("agents", {})
    if not isinstance(agents, dict):
        raise Refusal(f"could not parse {config}")
    analyzer_unmanaged = analyzer_before is not None and not analyzer_is_managed(
        analyzer_before
    )
    launcher_unmanaged = launcher_before is not None and not launcher_is_managed(
        launcher_before
    )
    if not managed_registration and NAME in agents:
        analyzer_before = None
    elif analyzer_unmanaged:
        analyzer_before = None
    if launcher_unmanaged:
        launcher_before = None
    return RepairRemovePlan(
        config=config,
        analyzer=analyzer,
        launcher=launcher,
        config_before=config_before,
        analyzer_before=analyzer_before,
        launcher_before=launcher_before,
        analyzer_unmanaged=analyzer_unmanaged,
        launcher_unmanaged=launcher_unmanaged,
        base=base,
        managed_registration=managed_registration,
        original_config_existed=original_config_existed,
    )


def preflight_remove(args: argparse.Namespace) -> None:
    prepare_remove(args)


def remove(args: argparse.Namespace) -> None:
    plan = prepare_remove(args)
    if plan.analyzer_unmanaged:
        print(
            f"[warning] preserving unmanaged repair analyzer: {plan.analyzer}",
            file=sys.stderr,
        )
    if plan.launcher_unmanaged:
        print(
            f"[warning] preserving unmanaged repair apply launcher: {plan.launcher}",
            file=sys.stderr,
        )
    journal: list[Mutation] = []
    temps: list[Staged] = []
    failure: BaseException | None = None
    try:
        if plan.managed_registration and plan.config_before is not None:
            if plan.original_config_existed:
                temp = stage(
                    plan.config,
                    plan.base.encode("utf-8"),
                    plan.config_before.mode,
                )
                temps.append(temp)
                publish_to(temp, plan.config, plan.config_before, journal)
            else:
                remove_state(plan.config_before, journal)
        if plan.analyzer_before is not None:
            remove_state(plan.analyzer_before, journal)
        if plan.launcher_before is not None:
            remove_state(plan.launcher_before, journal)
    except BaseException as error:
        failure = error
    finally:
        cleanup_failures = cleanup_all(temps)
    finalize_transaction(failure, journal, cleanup_failures)


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    for command in ("remove", "preflight-remove"):
        child = sub.add_parser(command)
        child.add_argument("--config", required=True)
        child.add_argument("--analyzer", required=True)
        child.add_argument("--launcher", required=True)
    for command in ("commands-install", "commands-remove"):
        child = sub.add_parser(command)
        child.add_argument("--manifest", required=True)
        if command == "commands-remove":
            child.add_argument("--test-fail-at", default="")
            child.add_argument("--preserve-foreign", action="store_true")
    command_owned = sub.add_parser("command-owned")
    command_owned.add_argument("--path", required=True)
    command_owned.add_argument("--name", required=True)
    command_owned.add_argument(
        "--kind", required=True, choices=("launcher", "runtime")
    )
    managed_remove = sub.add_parser("managed-remove")
    managed_remove.add_argument("--path", required=True)
    managed_remove.add_argument(
        "--kind", required=True, choices=("profile", "rules", "legacy-agent")
    )
    managed_quarantine = sub.add_parser("managed-quarantine")
    managed_quarantine.add_argument("--path", required=True)
    quarantine_target = managed_quarantine.add_mutually_exclusive_group(required=True)
    quarantine_target.add_argument("--destination")
    quarantine_target.add_argument("--quarantine-parent")
    managed_quarantine.add_argument(
        "--kind", required=True, choices=("legacy-agent",)
    )
    shell_entry = sub.add_parser("shell-entry")
    shell_entry.add_argument(
        "--action",
        required=True,
        choices=("remove",),
    )
    shell_entry.add_argument("--path", required=True)
    sub.add_parser("runtime-path")
    formal_ready = sub.add_parser("formal-schema-ready")
    formal_ready.add_argument("--requirements", required=True)
    classifier = sub.add_parser("classifier")
    classifier.add_argument("--action", required=True, choices=("preflight", "ensure"))
    classifier.add_argument("--path", required=True)
    config_fragment = sub.add_parser("config-fragment")
    config_fragment.add_argument("--action", required=True, choices=("remove",))
    config_fragment.add_argument("--path", required=True)
    config_fragment.add_argument("--preserve-empty", action="store_true")
    verify_lib = sub.add_parser("verify-library")
    verify_lib.add_argument("--root", default=None)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "remove":
            remove(args)
        elif args.command == "preflight-remove":
            preflight_remove(args)
        elif args.command == "commands-install":
            install_command_group(
                command_artifacts_from_manifest(Path(args.manifest), require_data=True)
            )
        elif args.command == "command-owned":
            state = command_ownership_state(Path(args.path), args.name, args.kind)
            print(state)
            return 0 if state in {"absent", "managed"} else 1
        elif args.command == "managed-remove":
            print(remove_managed_artifact(Path(args.path), args.kind))
            return 0
        elif args.command == "managed-quarantine":
            print(
                quarantine_managed_artifact(
                    Path(args.path),
                    args.kind,
                    Path(args.destination) if args.destination else None,
                    quarantine_parent=(
                        Path(args.quarantine_parent)
                        if args.quarantine_parent
                        else None
                    ),
                )
            )
            return 0
        elif args.command == "shell-entry":
            print(update_shell_entry(Path(args.path)))
            return 0
        elif args.command == "runtime-path":
            print(runtime_path())
            return 0
        elif args.command == "formal-schema-ready":
            formal_schema_dependency_ready(Path(args.requirements))
            return 0
        elif args.command == "classifier":
            action = preflight_classifier if args.action == "preflight" else ensure_classifier
            print(action(Path(args.path)))
            return 0
        elif args.command == "config-fragment":
            status = remove_config_fragment(
                Path(args.path),
                preserve_empty=args.preserve_empty,
            )
            print(status)
            return 0
        elif args.command == "verify-library":
            root = Path(args.root) if args.root else None
            verify_library(root)
            print("[ok] library adoption verified")
            return 0
        else:
            remove_command_group(
                command_artifacts_from_manifest(Path(args.manifest), require_data=False),
                test_fail_at=args.test_fail_at or None,
                preserve_foreign=args.preserve_foreign,
            )
    except (OSError, Refusal) as error:
        print(f"[error] {error}", file=sys.stderr)
        return 3
    print(f"[ok] repair artifacts {args.command} completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
