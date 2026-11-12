"""Append-only, hash-chained JSONL run ledger.

Each line is one entry::

    {"seq": n, "entry_type": T, "recorded_at": ISO-8601, "study_id": S,
     "payload": {...}, "prev_hash": "sha256:...", "entry_hash": "sha256:..."}

``entry_hash`` is the SHA-256 of the canonical JSON of every other field, and
``prev_hash`` is the previous entry's ``entry_hash`` (64 zeros for the first).
Editing, removing, inserting or reordering any line breaks verification.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cohortlens_shared.schemas import sha256_digest

LEDGER_FORMAT = "cohortlens.ledger.v1"
GENESIS_HASH = "sha256:" + "0" * 64
ENTRY_TYPES = frozenset(
    {
        "STUDY_STARTED",
        "CONTRACT_APPROVED",
        "APPROVAL_DECLINED",
        "RUN_SUBMITTED",
        "RESULT_RELEASED",
        "GATE_DECISION",
        "AGENT_INVOKED",
        "AGENT_TOOL_CALL",
        "AGENT_TOOL_RESULT",
        "AGENT_INVALID_TURN",
        "AGENT_PROTOCOL_FALLBACK",
        "AGENT_MODEL_TEXT",
        "AGENT_PROPOSAL",
        "AGENT_FAILED",
        "STUDY_FINISHED",
    }
)
_ENTRY_KEYS = {"seq", "entry_type", "recorded_at", "study_id", "payload", "prev_hash", "entry_hash"}


class LedgerError(ValueError):
    """The ledger is malformed or its hash chain does not verify."""


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def entry_hash(entry: dict[str, Any]) -> str:
    """Hash every field of an entry except its own ``entry_hash``."""
    return sha256_digest(_canonical({key: value for key, value in entry.items() if key != "entry_hash"}))


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Ledger:
    """Writer that appends hash-chained entries and flushes each line."""

    path: Path
    study_id: str
    clock: Callable[[], str] = _utc_now
    _seq: int = field(default=0, init=False)
    _prev: str = field(default=GENESIS_HASH, init=False)

    def __post_init__(self) -> None:
        if self.path.exists() and self.path.stat().st_size > 0:
            raise LedgerError(f"refusing to append to an existing ledger: {self.path.name}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")

    def append(self, entry_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append one entry and return it with its computed hash."""
        if entry_type not in ENTRY_TYPES:
            raise LedgerError(f"unknown ledger entry type: {entry_type}")
        entry: dict[str, Any] = {
            "seq": self._seq,
            "entry_type": entry_type,
            "recorded_at": self.clock(),
            "study_id": self.study_id,
            "payload": json.loads(_canonical(payload)),
            "prev_hash": self._prev,
        }
        entry["entry_hash"] = entry_hash(entry)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(_canonical(entry) + "\n")
        self._seq += 1
        self._prev = entry["entry_hash"]
        return entry


@dataclass(frozen=True)
class LedgerReport:
    """Outcome of verifying a ledger file."""

    entries: int
    head_hash: str
    entry_types: dict[str, int]


def _check_entry(entry: Any, seq: int, prev: str) -> None:
    if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
        raise LedgerError(f"entry {seq}: unexpected or missing fields")
    if entry["seq"] != seq:
        raise LedgerError(f"entry {seq}: sequence number {entry['seq']} out of order")
    if entry["entry_type"] not in ENTRY_TYPES:
        raise LedgerError(f"entry {seq}: unknown entry type {entry['entry_type']!r}")
    if entry["prev_hash"] != prev:
        raise LedgerError(f"entry {seq}: prev_hash does not match the previous entry")
    if entry["entry_hash"] != entry_hash(entry):
        raise LedgerError(f"entry {seq}: entry_hash does not match its contents")


def verify_entries(lines: Iterable[str]) -> LedgerReport:
    """Verify sequence, types and the full hash chain of ledger lines."""
    prev, count, types = GENESIS_HASH, 0, {}
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as error:
            raise LedgerError(f"entry {count}: not valid JSON") from error
        _check_entry(entry, count, prev)
        prev = entry["entry_hash"]
        types[entry["entry_type"]] = types.get(entry["entry_type"], 0) + 1
        count += 1
    if count == 0:
        raise LedgerError("ledger is empty")
    return LedgerReport(entries=count, head_hash=prev, entry_types=dict(sorted(types.items())))


def verify_ledger(path: Path) -> LedgerReport:
    """Verify a ledger file on disk."""
    with path.open(encoding="utf-8") as handle:
        return verify_entries(handle)


def read_entries(path: Path) -> list[dict[str, Any]]:
    """Read entries after verifying the chain."""
    verify_ledger(path)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
