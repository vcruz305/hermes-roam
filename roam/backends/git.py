"""Git backend: a remote branch is the store, HEAD.json is the mutable head."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, NoReturn

from ..errors import BackendError, BlobExists, ConfigError, HeadConflict
from ..textsafe import untrusted
from . import validate_key

DEFAULT_TRANSFER_TIMEOUT = 600.0
PROBE_CAP = 15.0
INTERNAL_CALL_TIMEOUT = 120.0
MAX_HEAD_BYTES = 1024 * 1024
MAX_PUSH_ATTEMPTS = 3

_SSH_DEFAULT = (
    "ssh -o BatchMode=yes -o ConnectTimeout=10 "
    "-o ServerAliveInterval=10 -o ServerAliveCountMax=3"
)
_HTTP_LOW_SPEED_LIMIT = "1000"
_HTTP_LOW_SPEED_TIME = "20"
# Commands that talk to the remote; every other git command works on the
# local mirror only. ls-remote is the reachability probe and is capped
# separately at PROBE_CAP.
_NETWORK_COMMANDS = frozenset({"fetch", "push"})

_HEAD_FILE = "HEAD.json"
_BUNDLE_DIR = "bundles"
_AUTHOR = "Hermes Roam"
_AUTHOR_EMAIL = "roam@localhost"

_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# scheme://<credentials>@host: the credential run is everything up to the
# LAST '@' of the surrounding non-space run, so passwords containing '@' or
# '/' are still caught. scp-style user@host:path has no scheme:// and never
# matches. Over-redaction is safe; a leak never is.
_URL_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*)://(\S*)")
_AUTH_MARKERS = (
    "terminal prompts disabled",
    "authentication failed",
    "permission denied",
    "could not read username",
    "host key verification failed",
    "could not read from remote repository",
)
_RACE_MARKERS = (
    "non-fast-forward",
    "behind its remote counterpart",
    "fetch first",
    "stale info",
)


def _redact(text: str) -> str:
    """Drop every URL's authority, query string, and fragment.

    The whole userinfo (user AND password) is removed however many ``@`` or
    ``/`` characters the password contains; query strings and fragments
    (signed URLs, path tokens) are stripped from every URL. Text without a
    ``scheme://`` (scp-style ``user@host:path`` remotes) is left untouched.
    When an ``@`` appears inside a query string or fragment the structure
    is untrustworthy, so the whole URL collapses to ``scheme://***``.
    Over-redaction is safe; a leak never is.
    """

    def _cut(head: str, tail: str, cut: int) -> str:
        if cut >= 0:
            tail = tail[:cut]
        return f"{head}{tail}"

    def _scrub(match: re.Match[str]) -> str:
        scheme = match.group(1)
        tail = match.group(2)
        at = tail.rfind("@")
        cut = min(
            (i for i in (tail.find("?"), tail.find("#")) if i >= 0),
            default=-1,
        )
        if at < 0:
            return _cut(f"{scheme}://", tail, cut)
        if cut < 0 or at < cut:
            # a real authority separator; the query (if any) is past it
            host_part = _cut("", tail[at + 1 :], cut - (at + 1) if cut >= 0 else -1)
            return f"{scheme}://***@{host_part}"
        # the last '@' sits inside the query/fragment: no structure to trust
        return f"{scheme}://***"

    return _URL_RE.sub(_scrub, text)


def _ssh_option_names(tokens: list[str]) -> set[str]:
    """The -o option names an ssh command line already carries, token-wise."""
    names: set[str] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "-o" and index + 1 < len(tokens):
            names.add(tokens[index + 1].split("=", 1)[0].lower())
            index += 2
            continue
        if token.startswith("-o") and len(token) > 2:
            names.add(token[2:].split("=", 1)[0].lower())
        index += 1
    return names


_SSH_REQUIRED_OPTIONS = {
    "batchmode": "BatchMode=yes",
    "connecttimeout": "ConnectTimeout=10",
    "serveraliveinterval": "ServerAliveInterval=10",
    "serveralivecountmax": "ServerAliveCountMax=3",
}


def _fail_fast_ssh(command: str) -> str:
    """Ensure an ssh command line never waits on a human or a dead network.

    Detection is token-wise: a command that merely CONTAINS "BatchMode" in a
    path or argument no longer bypasses the pin. A command that cannot be
    parsed still gets every missing option appended — over-pinning is safe.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    present = _ssh_option_names(tokens)
    missing = [
        f"-o {value}"
        for option, value in _SSH_REQUIRED_OPTIONS.items()
        if option not in present
    ]
    if not missing:
        return command
    return f"{command} {' '.join(missing)}"


def _git_env(extra: dict[str, str] | None = None, ssh_command: str = "") -> dict[str, str]:
    """Environment for every git call: no prompts, no stalled transports.

    The ssh transport is pinned to BatchMode (no human can be asked for a
    passphrase or a host-key decision), ConnectTimeout (a blackholed host
    fails in seconds), and ServerAliveInterval/CountMax (a connection that
    stops responding is detected and dropped instead of hanging until the
    transfer cap). A GIT_SSH_COMMAND or core.sshCommand the user configured
    is respected, with every missing option appended token-wise. The http
    transport aborts transfers slower than the low-speed floor; released
    git has no http.connectTimeout (an unmerged upstream proposal), so the
    backend's reachability probe and deadline are what bound a blackholed
    https connect.
    """

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_HTTP_LOW_SPEED_LIMIT"] = _HTTP_LOW_SPEED_LIMIT
    env["GIT_HTTP_LOW_SPEED_TIME"] = _HTTP_LOW_SPEED_TIME
    base = env.get("GIT_SSH_COMMAND") or ssh_command or _SSH_DEFAULT
    env["GIT_SSH_COMMAND"] = _fail_fast_ssh(base)
    if extra:
        env.update(extra)
    return env


class GitBackend:
    """Remote git repository as the object store.

    HEAD.json on the configured branch is the mutable head; its version token
    is the commit sha of the remote branch tip at the time of the last fetch.
    Bundle objects live under ``bundles/`` on the same branch. Every git call
    is a ``subprocess.run`` argv list with no terminal prompts; a remote that
    needs a password fails fast instead of hanging.

    R8 deadlines: every public method takes ``deadline`` — one ABSOLUTE
    time.monotonic() budget per user operation, shared by all of the
    method's subprocess calls and retries; an exhausted budget fails closed
    without starting another command. Each method first runs a reachability
    probe (ls-remote) capped at PROBE_CAP (15 s), so a blackholed remote
    fails in seconds. Actual transfers (fetch/push) are then capped by
    whichever is smaller: the remaining operation budget or the transfer
    timeout (``transfer_timeout``, 600 s default) — a legitimate slow
    transfer that keeps making progress is allowed that whole window, while
    stalled transports are dropped by the ServerAlive and low-speed pins.
    """

    name = "git"

    def __init__(
        self,
        remote: str,
        branch: str,
        mirror_dir: Path,
        *,
        transfer_timeout: float = DEFAULT_TRANSFER_TIMEOUT,
    ) -> None:
        if not remote:
            raise ConfigError("git backend needs git_remote: set the repository URL in the roam config")
        if not branch:
            raise ConfigError("git backend needs git_branch: set the branch name in the roam config")
        if not _BRANCH_RE.fullmatch(branch):
            raise ConfigError(f"invalid git branch name {branch!r}: use letters, digits, . _ -")
        if shutil.which("git") is None:
            raise ConfigError("the git backend needs the git CLI installed")
        if transfer_timeout <= 0:
            raise ConfigError(f"the git transfer timeout must be positive, got {transfer_timeout}")
        self.remote = remote
        self.branch = branch
        self.mirror_dir = Path(mirror_dir)
        self.transfer_timeout = float(transfer_timeout)
        self._ssh_command = ""
        self._active_deadline: float | None = None
        self._ensure_mirror()
        self._sweep_stale_locks()
        self._ssh_command = self._core_ssh_command()

    def __repr__(self) -> str:
        return f"GitBackend(branch={self.branch!r}, mirror_dir={str(self.mirror_dir)!r})"

    # ------------------------------------------------------------------ plumbing

    @contextmanager
    def _bounded(self, deadline: float | None):
        """Run one public operation under its absolute operation deadline."""

        previous = self._active_deadline
        self._active_deadline = deadline
        try:
            yield
        finally:
            self._active_deadline = previous

    def _run(
        self,
        argv: list[str],
        *,
        timeout: float = INTERNAL_CALL_TIMEOUT,
        cap: float | None = None,
        stdin: bytes | None = None,
        stdout_handle: BinaryIO | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> tuple[int, bytes, str]:
        """Run one git command. stdout is raw bytes; stderr is decoded, redacted text.

        Transfers (fetch/push) are capped by the smallest of the remaining
        operation budget and the transfer timeout; an explicit ``cap`` (the
        reachability probe) is bounded by that, the transfer timeout, and
        itself. Local commands keep the internal per-call timeout.
        """

        deadline = self._active_deadline
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise BackendError(
                f"git {argv[1]} exceeded its operation deadline; "
                "the remote is unreachable or too slow"
            )
        if argv[1] in _NETWORK_COMMANDS or cap is not None:
            transport = self.transfer_timeout if cap is None else min(cap, self.transfer_timeout)
            if remaining is not None:
                transport = min(transport, remaining)
            timeout = transport
        elif remaining is not None:
            # local commands still respect whatever is left of the budget
            timeout = min(timeout, remaining)
        env = _git_env(env_extra, self._ssh_command)
        try:
            if stdout_handle is None:
                proc = subprocess.run(
                    argv,
                    cwd=self.mirror_dir,
                    capture_output=True,
                    timeout=timeout,
                    env=env,
                    input=stdin,
                )
            else:
                proc = subprocess.run(
                    argv,
                    cwd=self.mirror_dir,
                    stdout=stdout_handle,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    env=env,
                )
        except FileNotFoundError as exc:
            raise ConfigError("the git backend needs the git CLI installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise BackendError(
                f"git {argv[1]} timed out after {timeout:.0f}s (the operation "
                "deadline or the git transfer cap was reached); the remote is "
                "unreachable, stalled, or the transfer is too slow"
            ) from exc
        except OSError as exc:
            raise BackendError(f"git {argv[1]} could not be executed: {exc}") from exc
        stdout = proc.stdout if isinstance(proc.stdout, bytes) else b""
        stderr = (
            _redact(proc.stderr.decode("utf-8", "replace"))
            if isinstance(proc.stderr, bytes)
            else ""
        )
        return proc.returncode, stdout, stderr

    def _fail(self, argv: list[str], code: int, err: str) -> NoReturn:
        """Raise the right BackendError for a failed git command. Never returns.

        ``err`` arrives already redacted (``_run`` scrubs stderr at source);
        argv fragments are redacted here too. The remote-controlled tail
        enters the message only through textsafe.untrusted(): one line,
        control characters collapsed, capped, labelled as remote text.
        """

        low = err.lower()
        if any(marker in low for marker in _AUTH_MARKERS):
            raise BackendError(
                "git could not authenticate against the remote; set up an SSH key or a "
                "credential helper for it and retry"
            )
        tail = ""
        for line in reversed(err.strip().splitlines()):
            if line.strip():
                tail = _redact(line.strip())
                break
        raise BackendError(
            f"git {_redact(argv[1])} failed (exit {code}): {untrusted(tail)}"
        ) from None

    def _core_ssh_command(self) -> str:
        """The user's core.sshCommand, if any; respected with BatchMode appended."""

        code, out, _ = self._run(["git", "config", "--get", "core.sshCommand"])
        if code == 0:
            return out.decode("utf-8", "replace").strip()
        return ""

    def _sweep_stale_locks(self) -> None:
        """Remove stale ``*.lock`` files under the mirror's .git (R8).

        A git call killed by a deadline leaves its ref lock behind and every
        later fetch or push fails on it. Sweeping at init is safe: the
        engine holds the home lock for the whole operation and the mirror is
        private to this device, so no live git process can hold one of
        these locks when a backend is constructed.
        """
        git_dir = self.mirror_dir / ".git"
        if not git_dir.is_dir():
            return
        for lock in git_dir.rglob("*.lock"):
            if not lock.is_file():
                continue
            try:
                lock.unlink()
            except OSError:
                pass  # best effort: an unremovable lock surfaces as a git error

    def _probe(self) -> None:
        """Fast reachability probe (R8): ls-remote capped at PROBE_CAP.

        A blackholed remote fails here in seconds, whatever the long
        transfer budget behind it; only a remote that answers the probe
        gets to the transfer phase, and stalls there are caught by the
        ServerAlive and low-speed transport pins.
        """
        code, _, err = self._run(["git", "ls-remote", "--heads", "origin"], cap=PROBE_CAP)
        if code != 0:
            self._fail(["git", "ls-remote"], code, err)

    def _ensure_mirror(self) -> None:
        if not (self.mirror_dir / ".git").exists():
            self.mirror_dir.mkdir(parents=True, exist_ok=True)
            code, _, err = self._run(["git", "init", "-q"])
            if code != 0:
                self._fail(["git", "init"], code, err)
        code, out, _ = self._run(["git", "remote", "get-url", "origin"])
        current = out.decode("utf-8", "replace").strip() if code == 0 else ""
        if current == self.remote:
            return
        if current:
            code, _, err = self._run(["git", "remote", "set-url", "origin", self.remote])
        else:
            code, _, err = self._run(["git", "remote", "add", "origin", self.remote])
        if code != 0:
            self._fail(["git", "remote"], code, err)

    def _fetch(self) -> None:
        """Fetch the branch; a branch that does not exist yet is not an error."""

        spec = f"+refs/heads/{self.branch}:refs/remotes/origin/{self.branch}"
        code, _, err = self._run(["git", "fetch", "origin", spec])
        if code == 0:
            return
        if "couldn't find remote ref" in err:
            return
        self._fail(["git", "fetch"], code, err)

    def _tip(self) -> str | None:
        code, out, err = self._run(
            ["git", "rev-parse", "--verify", f"refs/remotes/origin/{self.branch}"]
        )
        if code == 0:
            return out.decode().strip()
        if "needed a single revision" in err.lower() or "bad revision" in err.lower():
            return None
        self._fail(["git", "rev-parse"], code, err)

    def _path_exists(self, tip: str, path: str) -> bool:
        code, _, err = self._run(["git", "cat-file", "-e", f"{tip}:{path}"])
        if code == 0:
            return True
        if "does not exist in" in err:
            return False
        self._fail(["git", "cat-file"], code, err)

    def _object_size(self, tip: str, path: str) -> int | None:
        code, out, err = self._run(["git", "cat-file", "-s", f"{tip}:{path}"])
        if code == 0:
            return int(out.decode().strip())
        if "does not exist in" in err:
            return None
        self._fail(["git", "cat-file"], code, err)

    def _write_blob(self, data: bytes) -> str:
        code, out, err = self._run(["git", "hash-object", "-w", "--stdin"], stdin=data)
        if code != 0:
            self._fail(["git", "hash-object"], code, err)
        return out.decode().strip()

    def _write_blob_file(self, source: Path) -> str:
        code, out, err = self._run(["git", "hash-object", "-w", "--no-filters", "--", str(source)])
        if code != 0:
            self._fail(["git", "hash-object"], code, err)
        return out.decode().strip()

    def _commit_with(self, base: str | None, entries: dict[str, str], message: str) -> str:
        """Build a commit of ``base``'s tree plus ``entries`` (path -> blob sha)."""

        descriptor, index_path = tempfile.mkstemp(prefix="roam-index-")
        os.close(descriptor)
        os.unlink(index_path)  # read-tree starts from a missing index, not an empty one
        index_env = {"GIT_INDEX_FILE": index_path}
        try:
            if base:
                code, _, err = self._run(["git", "read-tree", base], env_extra=index_env)
                if code != 0:
                    self._fail(["git", "read-tree"], code, err)
            for path, blob in entries.items():
                code, _, err = self._run(
                    ["git", "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"],
                    env_extra=index_env,
                )
                if code != 0:
                    self._fail(["git", "update-index"], code, err)
            code, out, err = self._run(["git", "write-tree"], env_extra=index_env)
            if code != 0:
                self._fail(["git", "write-tree"], code, err)
            tree = out.decode().strip()
            argv = ["git", "commit-tree", tree]
            if base:
                argv += ["-p", base]
            argv += ["-m", message]
            identity = {
                "GIT_AUTHOR_NAME": _AUTHOR,
                "GIT_AUTHOR_EMAIL": _AUTHOR_EMAIL,
                "GIT_COMMITTER_NAME": _AUTHOR,
                "GIT_COMMITTER_EMAIL": _AUTHOR_EMAIL,
            }
            code, out, err = self._run(argv, env_extra=identity)
            if code != 0:
                self._fail(["git", "commit-tree"], code, err)
            return out.decode().strip()
        finally:
            if os.path.exists(index_path):
                os.unlink(index_path)

    def _blob_path(self, key: str) -> str:
        """Store path for a bundle key: raw keys land under bundles/."""
        raw = validate_key(key)
        if raw.startswith(f"{_BUNDLE_DIR}/"):
            raw = raw[len(_BUNDLE_DIR) + 1 :]
        return f"{_BUNDLE_DIR}/{raw}"

    def _head_blob(self, tip: str) -> str | None:
        """Blob sha of HEAD.json at ``tip``; None when it cannot be read."""
        code, out, err = self._run(["git", "rev-parse", f"{tip}:{_HEAD_FILE}"])
        if code == 0:
            return out.decode().strip()
        low = err.lower()
        if (
            "does not exist" in low
            or "bad revision" in low
            or "unknown revision" in low
            or "invalid object name" in low
        ):
            return None
        self._fail(["git", "rev-parse"], code, err)

    def _push(self, commit: str, *, lease: str | None) -> None:
        argv = ["git", "push", "origin", f"{commit}:refs/heads/{self.branch}"]
        if lease is not None:
            argv.append(f"--force-with-lease={self.branch}:{lease}")
        code, _, err = self._run(argv)
        if code == 0:
            return
        low = err.lower()
        if any(marker in low for marker in _RACE_MARKERS) or "--force-with-lease" in low:
            raise HeadConflict(
                "git push was rejected: the remote branch changed since it was read; "
                "pull before pushing again"
            )
        self._fail(["git", "push"], code, err)

    # ------------------------------------------------------------------ protocol

    def get_head(self, *, deadline: float | None = None) -> tuple[bytes, str] | None:
        with self._bounded(deadline):
            self._probe()
            return self._get_head()

    def _get_head(self) -> tuple[bytes, str] | None:
        self._fetch()
        tip = self._tip()
        if tip is None or not self._path_exists(tip, _HEAD_FILE):
            return None
        size = self._object_size(tip, _HEAD_FILE)
        if size is None:
            return None
        if size > MAX_HEAD_BYTES:
            raise BackendError(
                f"head {_HEAD_FILE} is {size} bytes, over the {MAX_HEAD_BYTES} byte limit"
            )
        code, out, err = self._run(["git", "show", f"{tip}:{_HEAD_FILE}"])
        if code != 0:
            self._fail(["git", "show"], code, err)
        if len(out) != size:
            raise BackendError(f"head {_HEAD_FILE} changed while it was downloaded")
        return out, tip

    def put_head(
        self, data: bytes, expected: str | None, *, deadline: float | None = None
    ) -> str:
        with self._bounded(deadline):
            self._probe()
            return self._put_head(data, expected)

    def _put_head(self, data: bytes, expected: str | None) -> str:
        self._fetch()
        tip = self._tip()
        if expected is None:
            if tip is not None and self._path_exists(tip, _HEAD_FILE):
                raise HeadConflict("the remote already has a head; pull before pushing again")
        else:
            # The branch tip may have moved under commits that did not touch the
            # head — a push of this device's own bundle does exactly that. The
            # CAS is on the head CONTENT: compare the HEAD.json blob at the tip
            # we read with the one at the current tip; only a head that is
            # byte-identical lets the push proceed, and the push itself is
            # leased to the current tip so any concurrent movement still fails.
            current_head = self._head_blob(tip) if tip is not None else None
            read_head = self._head_blob(expected)
            if current_head != read_head:
                raise HeadConflict("the remote head moved since it was read; pull before pushing again")
        blob = self._write_blob(data)
        commit = self._commit_with(tip, {_HEAD_FILE: blob}, "hermes-roam: update HEAD")
        # Lease to the tip we just fetched: a concurrent device moving the
        # branch between our fetch and this push makes the lease fail.
        self._push(commit, lease=tip)
        return commit

    def put_blob(self, key: str, source: Path, *, deadline: float | None = None) -> None:
        with self._bounded(deadline):
            self._probe()
            self._put_blob(key, source)

    def _put_blob(self, key: str, source: Path) -> None:
        path = self._blob_path(key)
        src = Path(source)
        if not src.is_file() or src.is_symlink():
            raise BackendError(f"bundle source must be a regular file: {src}")
        blob = self._write_blob_file(src)
        for _ in range(MAX_PUSH_ATTEMPTS):
            self._fetch()
            tip = self._tip()
            if tip is not None and self._path_exists(tip, path):
                raise BlobExists(f"bundle already exists: {key}")
            commit = self._commit_with(tip, {path: blob}, f"hermes-roam: store {key}")
            code, _, err = self._run(["git", "push", "origin", f"{commit}:refs/heads/{self.branch}"])
            if code == 0:
                return
            low = err.lower()
            if not any(marker in low for marker in _RACE_MARKERS):
                self._fail(["git", "push"], code, err)
            # lost a race: fetch again and rebuild our single commit on the new tip
        raise BackendError(
            f"git push kept losing the race for {key}; "
            f"tried {MAX_PUSH_ATTEMPTS} times, pull and retry"
        )

    def get_blob(
        self,
        key: str,
        dest: Path,
        *,
        max_bytes: int,
        deadline: float | None = None,
    ) -> None:
        with self._bounded(deadline):
            self._probe()
            self._get_blob(key, dest, max_bytes=max_bytes)

    def _get_blob(self, key: str, dest: Path, *, max_bytes: int) -> None:
        path = self._blob_path(key)
        self._fetch()
        tip = self._tip()
        if tip is None:
            raise BackendError(f"bundle not found: {key}")
        size = self._object_size(tip, path)
        if size is None:
            raise BackendError(f"bundle not found: {key}")
        if size > max_bytes:
            raise BackendError(f"bundle {key} is {size} bytes, over the {max_bytes} byte limit")
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp.open("xb") as handle:
                code, _, err = self._run(["git", "show", f"{tip}:{path}"], stdout_handle=handle)
            if code != 0:
                self._fail(["git", "show"], code, err)
            if temp.stat().st_size != size:
                raise BackendError(f"bundle {key} changed while it was downloaded")
            descriptor = os.open(temp, os.O_WRONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    def blob_exists(self, key: str, *, deadline: float | None = None) -> bool:
        with self._bounded(deadline):
            self._probe()
            return self._blob_exists(key)

    def _blob_exists(self, key: str) -> bool:
        path = self._blob_path(key)
        self._fetch()
        tip = self._tip()
        if tip is None:
            return False
        return self._path_exists(tip, path)

    def list_blobs(self, prefix: str, *, deadline: float | None = None) -> list[str]:
        """Sorted store keys under ``prefix`` (0.4 history/restore): the tree
        of the configured branch at the fetched tip, filtered to the prefix."""
        with self._bounded(deadline):
            self._probe()
            return self._list_blobs(prefix)

    def _list_blobs(self, prefix: str) -> list[str]:
        if not isinstance(prefix, str) or not prefix:
            raise ConfigError(f"invalid blob prefix {prefix!r}: prefixes must be non-empty strings")
        normalized = prefix.rstrip("/")
        try:
            validate_key(normalized)
        except ConfigError:
            raise ConfigError(
                f"invalid blob prefix {prefix!r}: use a relative POSIX prefix such as 'bundles/'"
            ) from None
        self._fetch()
        tip = self._tip()
        if tip is None:
            return []
        code, out, err = self._run(
            ["git", "ls-tree", "--name-only", "-r", "-z", tip, "--", normalized]
        )
        if code != 0:
            self._fail(["git", "ls-tree"], code, err)
        keys: list[str] = []
        for name in out.decode("utf-8", "replace").split("\0"):
            if not name or not name.startswith(f"{normalized}/"):
                continue
            try:
                validate_key(name)
            except ConfigError:
                continue
            keys.append(name)
        return sorted(set(keys))

    def ping(self, *, deadline: float | None = None) -> str:
        """Reachability probe: ls-remote capped at PROBE_CAP (R8)."""
        with self._bounded(deadline):
            return self._ping()

    def _ping(self) -> str:
        code, out, err = self._run(
            ["git", "ls-remote", "--heads", "origin"], cap=PROBE_CAP
        )
        if code != 0:
            self._fail(["git", "ls-remote"], code, err)
        heads = {
            line.split("\t")[1]
            for line in out.decode("utf-8", "replace").splitlines()
            if "\t" in line
        }
        present = f"refs/heads/{self.branch}" in heads
        state = "present" if present else "not created yet"
        return f"git backend ready: remote reachable, branch {self.branch} {state}"
