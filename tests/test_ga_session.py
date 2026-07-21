from __future__ import annotations

from types import SimpleNamespace

import ga_core.ga_handler as ga_handler
import ga_core.ga_runtime as ga_runtime
from ga_core.ga_handler import GaModules
from ga_core.ga_runtime import GaChatSession, GaSessionFactory


class Handler:
    def __init__(self, parent, last_history=None, cwd=".") -> None:
        self.parent = parent
        self.history_info = list(last_history or [])
        self.working = {}
        self.cwd = cwd

    def turn_end_callback(self, response, *_args):
        self.history_info.append("[Agent] done")
        return ""


class Agent:
    def __init__(self) -> None:
        self.history = []
        self.extra_sys_prompts = []
        self.llmclient = SimpleNamespace(backend=SimpleNamespace(extra_sys_prompt=""), log_path="")
        self.log_path = "fake.log"


class Gate:
    def review(self, *_args):
        return SimpleNamespace(decision="allow", message="")


def modules(captured: list[str], content: str) -> GaModules:
    def loop(_client, _system, user_input, handler, _schema, **_kwargs):
        captured.append(user_input)
        response = SimpleNamespace(content=content)
        handler.turn_end_callback(response, [], [], 1, "", {})
        yield content

    return GaModules(
        agent_loop=SimpleNamespace(agent_runner_loop=loop, StepOutcome=object),
        agentmain=SimpleNamespace(
            GenericAgent=Agent,
            TOOLS_SCHEMA=[],
            get_system_prompt=lambda: "base prompt",
        ),
        ga=SimpleNamespace(
            GenericAgentHandler=Handler,
            smart_format=lambda text, max_str_len=200: text[:max_str_len],
        ),
    )


def test_session_preserves_history_observations_and_artifacts(tmp_path) -> None:
    captured: list[str] = []
    session = GaChatSession(
        modules=modules(
            captured,
            "Final answer [[attach:artifacts/report.txt]]<summary>done</summary>",
        ),
        workspace=tmp_path / "chat",
        shared_prompt="shared",
        gate=Gate(),
        approval_sink=object(),
        max_turns=5,
    )
    native_loop = session.modules.agent_loop.agent_runner_loop

    def loop_with_memory_lock(*args, **kwargs):
        ga_handler._MEMORY_SETTLEMENT_LOCK.acquire()
        args[3]._memory_settlement_locked = True
        yield from native_loop(*args, **kwargs)

    session.modules.agent_loop.agent_runner_loop = loop_with_memory_lock
    artifact = session.artifacts / "report.txt"
    artifact.write_text("deliverable", encoding="utf-8")
    text, files = session.run(
        chat_id="chat-1",
        user_id="user-1",
        display_name="Alice",
        user_text="Read the attachment",
        runtime_observations=("missing.csv failed to download",),
    )
    assert text == "Final answer"
    assert files == (artifact,)
    assert "missing.csv failed to download" in captured[0]
    assert session.agent.history[-1] == "[Agent] done"
    assert ga_handler._MEMORY_SETTLEMENT_LOCK.acquire(timeout=0.1)
    ga_handler._MEMORY_SETTLEMENT_LOCK.release()


def test_factory_workspace_names_are_readable_and_collision_safe(tmp_path, monkeypatch) -> None:
    class CapturedSession:
        def __init__(self, **kwargs) -> None:
            self.workspace = kwargs["workspace"]

    monkeypatch.setattr(ga_runtime, "GaChatSession", CapturedSession)
    factory = object.__new__(GaSessionFactory)
    factory.modules = object()
    factory.runtime_root = tmp_path
    factory.shared_prompt = ""
    factory.gate = Gate()
    factory.approval_sink = object()
    factory.max_turns = 1
    first, second = factory.create("team/chat"), factory.create("team:chat")
    assert first.workspace != second.workspace
    assert first.workspace.parent == tmp_path / "chats"
    assert first.workspace.name.startswith("team_chat-")
    assert len(first.workspace.name.rsplit("-", 1)[1]) == 12
