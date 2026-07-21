from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ga_core.ga_handler import ApprovalContext, make_handler_class, load_ga_modules
from ga_core.gate import GateDecision

ROOT = Path(__file__).parents[1]
GA_ROOT = ROOT / "vendor" / "GenericAgent"
pytestmark = pytest.mark.skipif(
    not (GA_ROOT / "agentmain.py").is_file(),
    reason="run scripts/fetch_ga.py to exercise the pinned GA seam",
)


class AllowGate:
    def review(self, code: str, code_type: str, cwd: str) -> GateDecision:
        return GateDecision("allow", "read-only", "test")


class Parent:
    def __init__(self, workspace: Path) -> None:
        self._approval_context = ApprovalContext("c", "u", "Alice", AllowGate(), SimpleNamespace())
        self._last_response = ""
        self.verbose = False
        self.extrakeyinfo = None
        self.intervene = None
        self.task_dir = str(workspace)
        self.llmclient = SimpleNamespace(backend=SimpleNamespace(history=[], maxlen_multiplier=1.0))

    def get_ctx_multiplier(self) -> float:
        return 1.0


def exhaust(generator):
    try:
        while True:
            next(generator)
    except StopIteration as stop:
        return stop.value


def test_pinned_handler_executes_inline_eval_and_ask_user(tmp_path: Path) -> None:
    modules = load_ga_modules(GA_ROOT)
    handler_class = make_handler_class(modules)
    assert issubclass(handler_class, modules.ga.GenericAgentHandler)
    handler = handler_class(Parent(tmp_path), [], str(tmp_path))

    ask = exhaust(handler.do_ask_user({"question": "continue?"}, SimpleNamespace(content="")))
    assert isinstance(ask, modules.agent_loop.StepOutcome)
    assert ask.should_exit is True

    result = exhaust(
        handler.do_code_run(
            {"type": "python", "code": "1 + 1", "inline_eval": True},
            SimpleNamespace(content=""),
        )
    )
    assert isinstance(result, modules.agent_loop.StepOutcome)
    assert "2" in str(result.data)


def test_pinned_abort_sets_stop_signal() -> None:
    modules = load_ga_modules(GA_ROOT)
    agent = object.__new__(modules.agentmain.GenericAgent)
    agent.is_running = True
    agent.stop_sig = False
    agent.handler = SimpleNamespace(code_stop_signal=[])
    agent.abort()
    assert agent.stop_sig is True
    assert agent.handler.code_stop_signal
