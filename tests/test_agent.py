"""Agent scaffold invariants."""

from cohortlens_agent.agent_app import MODEL, app


def test_agent_uses_flower_endeavor() -> None:
    assert MODEL == "flower-endeavor"
    assert app is not None

