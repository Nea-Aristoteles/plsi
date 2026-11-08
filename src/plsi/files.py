"""JSON on disk: canonical hashing, atomic writes, the instance lock, jsonl logs, capped input reading."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import sys
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from plsi.errors import InstanceLocked, NotFound, ValidationError, warn

MAX_INPUT_BYTES = 50 * 1024 * 1024
LOCK_TIMEOUT_S = 10.0


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def sha256_file(path: Path) -> str:
    """sha256 hex of a file of any size, read in 1 MiB chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def hash_json(obj: Any) -> str:
    """'sha256:<hex>' over canonical_json(obj). Used for map_hash, state_hash, action/context/input hashes."""
    return "sha256:" + sha256_hex(canonical_json(obj))


def hash12(prefixed_hash: str) -> str:
    return prefixed_hash.removeprefix("sha256:")[:12]


def atomic_write_bytes(path: Path, data: bytes, private: bool = False) -> None:
    """Write via a temp file and rename; `private` files (secrets) are created owner-only (0o600) where the OS
    supports it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600 if private else 0o666)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str, private: bool = False) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), private)


def atomic_write_json(path: Path, obj: Any, private: bool = False) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n", private)


_HELD: set[Path] = set()  # lock files this process holds, so its own reads never wait on them


def holding(directory: Path) -> bool:
    """True while this process holds `lock(directory)`."""
    return (directory / ".lock") in _HELD


@contextlib.contextmanager
def lock(directory: Path, timeout: float = LOCK_TIMEOUT_S) -> Iterator[None]:
    """Exclusive `<directory>/.lock` via O_EXCL; retries until timeout, then InstanceLocked. NOT re-entrant:
    never call EventStore.append* while holding it for the same instance."""
    path = directory / ".lock"
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise InstanceLocked(f"{directory.name} is locked by another process", hint=f"remove {path} if no plsi process is running") from None
            time.sleep(0.05)
    _HELD.add(path)
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        yield
    finally:
        _HELD.discard(path)
        path.unlink(missing_ok=True)


def append_jsonl(path: Path, obj: Any) -> None:
    append_jsonl_many(path, [obj])


def append_jsonl_many(path: Path, objs: Iterable[Any]) -> list[tuple[int, int]]:
    """One bytes write plus fsync for all lines. A torn previous tail is terminated first so these lines stay intact.
    Returns (byte offset, byte length without the newline) of each line written."""
    lines = [(canonical_json(obj)).encode("utf-8") for obj in objs]
    if not lines:
        return []
    data = b"\n".join(lines) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab+") as fh:
        start = fh.seek(0, os.SEEK_END)
        if start > 0:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                data, start = b"\n" + data, start + 1
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    placed, pos = [], start
    for line in lines:
        placed.append((pos, len(line)))
        pos += len(line) + 1
    return placed


def read_jsonl(path: Path) -> list[Any]:
    """All parseable lines; unparseable (torn) lines are skipped with a TORN_LINE warning."""
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for n, raw in enumerate(fh, 1):
            if not raw.strip():
                continue
            try:
                out.append(loads_json(raw))
            except ValueError:
                warn("TORN_LINE", f"{path.name} line {n} is not valid JSON and was ignored")
    return out


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name} is not allowed")


def loads_json(text: str) -> Any:
    return json.loads(text, parse_constant=_reject_constant)


def read_input_bytes(path: str | Path, max_bytes: int = MAX_INPUT_BYTES) -> bytes:
    """Read a user input file ('-' = stdin), capped at max_bytes."""
    if str(path) == "-":
        data = sys.stdin.buffer.read(max_bytes + 1)
    else:
        p = Path(path)
        if not p.is_file():
            raise NotFound(f"input file not found: {p.name}", code="FILE_NOT_FOUND")
        with open(p, "rb") as fh:
            data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValidationError(f"input exceeds {max_bytes} bytes", code="INPUT_TOO_LARGE")
    return data.removeprefix(b"\xef\xbb\xbf")


def decode_input(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"input is not UTF-8: {exc.reason}", code="INPUT_NOT_UTF8") from None


def parse_input_json(text: str) -> Any:
    try:
        return loads_json(text)
    except ValueError as exc:
        raise ValidationError(f"invalid JSON: {exc}", code="INVALID_JSON") from None


def read_input_text(path: str | Path, max_bytes: int = MAX_INPUT_BYTES) -> str:
    return decode_input(read_input_bytes(path, max_bytes))


def load_json(path: str | Path) -> Any:
    """Parse a JSON input file ('-' = stdin). Rejects NaN/Infinity; capped at 50 MB."""
    return parse_input_json(read_input_text(path))
