from __future__ import annotations

from types import SimpleNamespace

import ga_core.ga_runtime as ga_runtime
from ga_core.ga_runtime import GaChatSession, GaModules, GaSessionFactory


class FakeBaseHandler:
    def __init__(self, parent, last_history=None, cwd=".") -> None:
        self.parent = parent
        self.history_info = list(last_history or [])
        self.working = {}
        self.cwd = cwd

    def turn_end_callback(self, response, tool_calls, tool_results, turn, next_prompt, exit_reason):
        self.history_info.append("[Agent] done")
        return next_prompt


class FakeAgent:
    def __init__(self) -> None:
        self.history = []
        self.extra_sys_prompts = []
        self.llmclient = SimpleNamespace(
            backend=SimpleNamespace(extra_sys_prompt=""), log_path=""
        )
        self.log_path = "fake.log"


class FakeGate:
    def review(self, code, code_type, cwd):
        return SimpleNamespace(decision="allow", message="")


class FakeApprovals:
    pass


def _make_loop(content: str):
    """Factory for fake agent_runner_loop generators that yield content via turn_end_callback."""

    def loop(client, system_prompt, user_input, handler, tools_schema, **kwargs):
        response = SimpleNamespace(content=content)
        handler.turn_end_callback(response, [], [], 1, "", {})
        yield content

    return loop


def _modules(content: str) -> GaModules:
    return GaModules(
        agent_loop=SimpleNamespace(agent_runner_loop=_make_loop(content), StepOutcome=object),
        agentmain=SimpleNamespace(
            GenericAgent=FakeAgent,
            TOOLS_SCHEMA=[],
            get_system_prompt=lambda: "base prompt",
        ),
        ga=SimpleNamespace(
            GenericAgentHandler=FakeBaseHandler,
            smart_format=lambda text, max_str_len=200: text[:max_str_len],
        ),
    )


def _make_session(tmp_path, content: str) -> GaChatSession:
    return GaChatSession(
        modules=_modules(content),
        workspace=tmp_path / "chat",
        shared_prompt="shared",
        gate=FakeGate(),
        approval_sink=FakeApprovals(),
        max_turns=5,
    )


# TEST-CONTRACT: req=SESSION-01 | rejects=upstream loop output loses summary or history | gap=no end-to-end session run | revert=remove turn_end_callback super call | mock=fake_loop (agent_loop boundary)=>summary strip + history writeback run real
def test_session_assembles_upstream_loop_and_preserves_summary(tmp_path) -> None:
    session = _make_session(tmp_path, "Final answer<summary>done</summary>")
    text, files = session.run(
        chat_id="chat-1",
        user_id="user-1",
        display_name="Alice",
        user_text="Hello",
    )
    assert text == "Final answer"
    assert files == ()
    assert session.agent.history[-1] == "[Agent] done"


# TEST-CONTRACT: req=SESSION-02 | rejects=[[attach:]] marker leaks into user reply and file is not delivered | gap=no attachment extraction test | revert=remove _extract_attachments call | mock=fake_loop (agent_loop boundary)=>_extract_attachments runs real
def test_extract_attachments_delivers_files_from_response(tmp_path) -> None:
    session = _make_session(tmp_path, "Here is the report [[attach:artifacts/report.txt]]")
    artifact = session.artifacts / "report.txt"
    artifact.write_text("deliverable content", encoding="utf-8")

    text, files = session.run(
        chat_id="chat-1",
        user_id="user-1",
        display_name="Alice",
        user_text="Generate a report",
    )
    assert "[[attach:" not in text
    assert len(files) == 1
    assert files[0].name == "report.txt"
    assert files[0].read_text(encoding="utf-8") == "deliverable content"


# TEST-CONTRACT: req=SESSION-WORKSPACE-01 | rejects=different chat ids share one sanitized workspace | gap=no collision case | revert=remove chat-id digest from GaSessionFactory.create | mock=GaChatSession constructor boundary=>workspace naming runs real
def test_factory_workspace_name_keeps_readability_without_collisions(tmp_path, monkeypatch) -> None:
    class CapturedSession:
        def __init__(self, **kwargs) -> None:
            self.workspace = kwargs["workspace"]

    monkeypatch.setattr(ga_runtime, "GaChatSession", CapturedSession)
    factory = object.__new__(GaSessionFactory)
    factory.modules = object()
    factory.runtime_root = tmp_path
    factory.shared_prompt = ""
    factory.gate = FakeGate()
    factory.approval_sink = FakeApprovals()
    factory.max_turns = 1

    first = factory.create("team/chat")
    second = factory.create("team:chat")

    assert first.workspace != second.workspace
    assert first.workspace.parent == tmp_path / "chats"
    assert first.workspace.name.startswith("team_chat-")
    assert len(first.workspace.name.rsplit("-", 1)[1]) == 12
