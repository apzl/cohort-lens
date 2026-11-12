"""In-memory Grid that routes ServerApp messages to the real ClientApp handlers.

This is the local backend's transport: the same ``server_app`` and
``client_app`` handler code runs in one process, each emulated node holding its
own snapshot. It is an execution aid, not a Flower runtime: there is no
network, isolation, node authentication or timeout behaviour.
"""

from __future__ import annotations

import hashlib
import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np

from cohortlens_shared.statistics import generate_synthetic_site, load_site_dataset

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_DIRS = (_REPO_ROOT / "apps" / "cohort-analytics", _REPO_ROOT / "apps" / "cohort-agent")


def import_app_module(name: str) -> ModuleType:
    """Import an app module, adding the repository app directories if needed.

    App directories are appended (not prepended) so the authoritative
    ``shared/cohortlens_shared`` keeps precedence over the staged copies.
    """
    try:
        return importlib.import_module(name)
    except ImportError:
        for directory in _APP_DIRS:
            if directory.is_dir() and str(directory) not in sys.path:
                sys.path.append(str(directory))
        return importlib.import_module(name)


@dataclass(frozen=True)
class SiteSnapshot:
    """One emulated node's local arrays and non-sensitive aliases."""

    site_alias: str
    snapshot_alias: str
    design: np.ndarray
    outcome: np.ndarray
    sha256: str | None = None


def synthetic_sites() -> list[SiteSnapshot]:
    """Three generated synthetic sites, identical to the ClientApp fallback."""
    sites = []
    for index in range(3):
        design, outcome = generate_synthetic_site(index)
        alias = f"site-{index + 1}"
        sites.append(SiteSnapshot(alias, f"synthetic-{alias}-v1", design, outcome))
    return sites


def npz_sites(directory: Path, snapshot_prefix: str) -> list[SiteSnapshot]:
    """Load ``site-{1,2,3}.npz`` from a prepared snapshot directory."""
    sites = []
    for index in range(1, 4):
        path = directory / f"site-{index}.npz"
        design, outcome = load_site_dataset(path)
        digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        sites.append(
            SiteSnapshot(f"site-{index}", f"{snapshot_prefix}-site-{index}", design, outcome, digest)
        )
    return sites


class InMemoryGrid:
    """Minimal ``Grid`` for ``run_phase1`` and friends over in-process sites."""

    def __init__(self, sites: list[SiteSnapshot], *, first_node_id: int = 101) -> None:
        self._client = import_app_module("cohortlens_analytics.client_app")
        self._sites = {first_node_id + offset: site for offset, site in enumerate(sites)}
        self.messages_sent: list[str] = []

    def get_node_ids(self) -> list[int]:
        """Node ids of the emulated SuperNodes."""
        return sorted(self._sites)

    def _handle(self, message: Any) -> Any:
        site = self._sites[message.metadata.dst_node_id]
        kind = message.metadata.message_type
        handler = {
            "query.preflight": self._client.handle_preflight,
            "query.associate": self._client.handle_association,
        }.get(kind)
        if handler is None:
            raise ValueError(f"unsupported message type: {kind}")
        return handler(
            message,
            site.site_alias,
            design=site.design,
            outcome=site.outcome,
            snapshot_alias=site.snapshot_alias,
        )

    def send_and_receive(self, messages: list[Any], *, timeout: float | None = None) -> list[Any]:
        """Deliver each message to its node's real handler and collect replies."""
        replies = []
        for message in messages:
            self.messages_sent.append(message.metadata.message_type)
            replies.append(self._handle(message))
        return replies
