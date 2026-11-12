"""Model transports for the local backend: scripted policy or an explicit endpoint."""

from __future__ import annotations

import os
from dataclasses import dataclass

from cohortlens_shared.investigator import ModelTransport, OpenAIResponsesTransport

from .scripted_policy import POLICY_KIND, POLICY_LABEL, ScriptedPolicyTransport


@dataclass(frozen=True)
class InvestigatorClient:
    """A transport plus the labels recorded wherever its output appears."""

    transport: ModelTransport
    model: str
    policy_kind: str
    policy_label: str


def scripted_client() -> InvestigatorClient:
    """The deterministic scripted policy; never presented as model output."""
    return InvestigatorClient(ScriptedPolicyTransport(), POLICY_LABEL, POLICY_KIND, POLICY_LABEL)


def openai_compatible_client(base_url: str, model: str, api_key_env: str) -> InvestigatorClient:
    """A user-supplied OpenAI-compatible Responses endpoint.

    Endpoint, model name and key variable must all be given explicitly; nothing
    is read from provider-default environment variables.
    """
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise ValueError(f"environment variable {api_key_env} is not set")
    from openai import OpenAI

    client = OpenAI(base_url=base_url, api_key=api_key, max_retries=0)
    return InvestigatorClient(OpenAIResponsesTransport(client), model, "MODEL", _label(model))


def _label(model: str) -> str:
    """Model names become ledger identifiers; keep them identifier-safe."""
    cleaned = "".join(ch if ch.isalnum() or ch in "._:-" else "-" for ch in model).strip("-")
    return cleaned[:128] or "unnamed-model"
