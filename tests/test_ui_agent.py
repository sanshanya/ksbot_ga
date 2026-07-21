from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ga_core.ga_handler import GaModules
from ga_core.ga_ui import install_ui_agent


class FakeAgent:
    def __init__(self) -> None:
        self.extra_sys_prompts = []
        self.task_dir = None
        self.queries = []

    def put_task(self, query, source="user", images=None):
        self.queries.append((query, source, images))
        return query


class FakeHandler:
    def __init__(self, parent, last_history=None, cwd=".") -> None:
        self.parent = parent
        self.cwd = cwd


class FakeGate:
    pass


@pytest.fixture
def modules() -> GaModules:
    agentmain = SimpleNamespace(
        GenericAgent=FakeAgent,
        GeneraticAgent=FakeAgent,
        GenericAgentHandler=FakeHandler,
    )
    return GaModules(
        agent_loop=SimpleNamespace(StepOutcome=object),
        agentmain=agentmain,
        ga=SimpleNamespace(GenericAgentHandler=FakeHandler),
    )


def _make_agent(modules: GaModules, workspace: Path, shared_prompt: str = "") -> FakeAgent:
    agent_class = install_ui_agent(
        modules, workspace=workspace, shared_prompt=shared_prompt, gate=FakeGate()
    )
    return agent_class()


# TEST-CONTRACT: req=UI-AGENT-01 | rejects=UI agent does not patch GA aliases or set workspace | gap=no install test | revert=remove GeneraticAgent alias assignment | mock=FakeAgent+FakeHandler (GA class boundary)=>none (GA requires live LLM; harness logic runs real)
def test_install_ui_agent_patches_ga_aliases_and_workspace(tmp_path, modules) -> None:
    workspace = tmp_path / "ui"
    agent_class = install_ui_agent(
        modules,
        workspace=workspace,
        shared_prompt="shared prompt",
        gate=FakeGate(),
    )
    assert modules.agentmain.GenericAgent is agent_class
    assert modules.agentmain.GeneraticAgent is agent_class
    agent = agent_class()
    assert agent._harness_workspace == workspace.resolve()
    assert agent.task_dir == str(workspace.resolve())
    assert agent.extra_sys_prompts[0] == "shared prompt"
    assert (workspace / "artifacts").is_dir()


# TEST-CONTRACT: req=UI-APPROVAL-01 | rejects=Approve answer does not clear pending or set approved fingerprint | gap=no approve translation test | revert=remove pending check in put_task | mock=FakeAgent (GA class boundary)=>none (GA requires live LLM; harness logic runs real)
def test_ui_approval_answer_is_translated_for_the_upstream_agent(tmp_path, modules) -> None:
    agent = _make_agent(modules, tmp_path / "ui")
    agent._harness_pending_approval = {"fingerprint": "abc", "review": "delete pod review"}
    agent.put_task("Approve")
    assert agent._harness_pending_approval is None
    assert agent._harness_approved_call == "abc"
    assert "approved" in agent.queries[-1][0].lower()


# TEST-CONTRACT: req=UI-AUDIT-01 | rejects=approval decision (approve/reject) not written to audit jsonl | gap=no audit write test | revert=remove _log_ui_approval call | mock=FakeAgent (GA class boundary)=>none (GA requires live LLM; audit file written by real _log_ui_approval)
@pytest.mark.parametrize(
    "reply, approved", [("同意", True), ("先检查流量", False)]
)
def test_ui_approval_writes_audit_jsonl(tmp_path, modules, reply, approved) -> None:
    workspace = tmp_path / "ui"
    agent = _make_agent(modules, workspace)
    agent._harness_pending_approval = {
        "fingerprint": "deadbeef",
        "review": "This operation may reduce service capacity.",
    }
    agent.put_task(reply)
    record = json.loads((workspace / "approval.jsonl").read_text(encoding="utf-8").strip())
    assert record["mode"] == "tui"
    assert record["approved"] is approved
    assert record["review"] == "This operation may reduce service capacity."
    assert record["feedback"] == ("" if approved else reply)
    assert agent._harness_pending_approval is None
