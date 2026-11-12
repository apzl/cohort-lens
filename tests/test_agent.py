"""Agent scaffold invariants."""

from cohortlens_agent.agent_app import MODEL, RESULT_FRAME_PREFIX, app
from cohortlens_operator.agent_frames import AGENT_RESULT_FRAME_PREFIX


def test_agent_uses_flower_endeavor() -> None:
    assert MODEL == "flower-endeavor"
    assert app is not None


def test_agent_and_operator_agree_on_the_frame_prefix() -> None:
    assert RESULT_FRAME_PREFIX == AGENT_RESULT_FRAME_PREFIX
