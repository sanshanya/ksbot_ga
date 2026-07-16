from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from ga_core.ga_runtime import ApprovalContext, GaModules, _make_handler_class


@dataclass
class FakeStepOutcome:
    data: object
    next_prompt: str | None = None
    should_exit: bool = False


class FakeBaseHandler:
    def __init__(self, parent, last_history=None, cwd=".") -> None:
        self.parent = parent
        self.history_info = last_history or []
        self.cwd = cwd
        self.working = {}

    def _extract_code_block(self, response, code_type):
        return None

    def _get_anchor_prompt(self, skip=False):
        return "anchor"

    def do_code_run(self, args, response):
        self.parent.executed = True
        yield "executed"
        return FakeStepOutcome({"status": "success"}, next_prompt="next")


class FixedGate:
    source = "ai_gate"

    def __init__(self, decision: str) -> None:
        self.decision = decision

    def review(self, code: str, code_type: str, cwd: str):
        return SimpleNamespace(decision=self.decision, message="test review", source=self.source)


class FixedApprovals:
    def __init__(self, result: bool, feedback: str = "") -> None:
        self.result, self.kwargs = (result, feedback), {}

    def request(self, **kwargs):
        self.kwargs = kwargs
        return self.result


def exhaust(generator):
    items = []
    try:
        while True:
            items.append(next(generator))
    except StopIteration as stop:
        return items, stop.value


def modules() -> GaModules:
    return GaModules(
        agent_loop=SimpleNamespace(StepOutcome=FakeStepOutcome),
        agentmain=SimpleNamespace(),
        ga=SimpleNamespace(GenericAgentHandler=FakeBaseHandler),
    )


_SENTINEL = object()


def make_parent(
    gate_decision: str, approval_result: bool = False, approval_sink=_SENTINEL, **extra
):
    if approval_sink is _SENTINEL:
        approval_sink = FixedApprovals(approval_result)
    return SimpleNamespace(
        executed=False,
        _approval_context=ApprovalContext(
            chat_id="c",
            user_id="u",
            display_name="Alice",
            gate=FixedGate(gate_decision),
            approval_sink=approval_sink,
        ),
        **extra,
    )


# TEST-CONTRACT: req=HANDLER-01 | rejects=denied production write still executes | gap=no rejection path | revert=remove approval branch in do_code_run | mock=FixedGate+FixedApprovals (gate+approval boundary)=>GATE-AI-01, APPROVAL-01
def test_denied_production_write_does_not_execute() -> None:
    handler_class = _make_handler_class(modules())
    approvals = FixedApprovals(False, "先检查流量")
    parent = make_parent("approval_required", approval_sink=approvals)
    parent._approval_context.gate.source = "fail_closed"
    handler = handler_class(parent, [], ".")
    items, outcome = exhaust(
        handler.do_code_run(
            {"type": "bash", "code": "kubectl delete pod x -n kaic-kis"},
            SimpleNamespace(content=""),
        )
    )
    assert outcome.data["status"] == "rejected"
    assert "先检查流量" in outcome.next_prompt
    assert approvals.kwargs["allow_window"] is False


# TEST-CONTRACT: req=HANDLER-01B | rejects=approved WPS write is regenerated or executed twice | gap=no direct-resume assertion | revert=replace original stack resume with a new model turn | mock=FixedGate+FixedApprovals
def test_wps_approval_executes_original_call_once() -> None:
    calls: list[str] = []

    class CountingBase(FakeBaseHandler):
        def do_code_run(self, args, response):
            calls.append(str(args.get("code") or args.get("script")))
            yield "executed"
            return FakeStepOutcome({"status": "success"}, next_prompt="next")

    custom = GaModules(
        agent_loop=SimpleNamespace(StepOutcome=FakeStepOutcome),
        agentmain=SimpleNamespace(),
        ga=SimpleNamespace(GenericAgentHandler=CountingBase),
    )
    command = "kubectl scale deploy api --replicas=3 -n kaic-kis"
    parent = make_parent("approval_required", approval_sink=FixedApprovals(True))
    handler = _make_handler_class(custom)(parent, [], ".")
    items, outcome = exhaust(
        handler.do_code_run({"type": "bash", "code": command}, SimpleNamespace(content=""))
    )

    assert items == ["[Approval] test review\n", "executed"]
    assert calls == [command]
    assert outcome.data["status"] == "success"


# TEST-CONTRACT: req=HANDLER-02 | rejects=non-production command is blocked by gate | gap=no allow path | revert=remove allow short-circuit in do_code_run | mock=FixedGate (gate boundary)=>GATE-AI-01
def test_non_production_command_uses_upstream_tool() -> None:
    handler_class = _make_handler_class(modules())
    parent = make_parent("allow")
    handler = handler_class(parent, [], ".")
    items, outcome = exhaust(
        handler.do_code_run({"type": "bash", "code": "echo ok"}, SimpleNamespace(content=""))
    )
    assert items == ["executed"]
    assert outcome.data["status"] == "success"


# TEST-CONTRACT: req=HANDLER-03 | rejects=WPS replaces GA global L2/L3/SOP settlement with per-chat memory | gap=custom session_memory override | revert=restore chat-only do_start_long_term_update | mock=pinned GA memory boundary
def test_long_term_update_keeps_ga_global_memory_semantics(tmp_path) -> None:
    ga_root = tmp_path / "ga"
    memory = ga_root / "memory"
    memory.mkdir(parents=True)
    sop = memory / "memory_management_sop.md"
    sop.write_text("L2: global_mem.txt\nL3: ../memory/", encoding="utf-8")
    reads: list[tuple[str, bool]] = []

    def file_read(path: str, show_linenos: bool = True) -> str:
        reads.append((path, show_linenos))
        return Path(path).read_text(encoding="utf-8")

    class NativeMemoryBase(FakeBaseHandler):
        def do_start_long_term_update(self, args, response):
            yield "native memory settlement\n"
            return FakeStepOutcome(
                "native relative lookup result",
                next_prompt="NATIVE MEMORY POLICY\nUse ../memory/global_mem.txt and ../memory/",
            )

    custom = GaModules(
        agent_loop=SimpleNamespace(StepOutcome=FakeStepOutcome),
        agentmain=SimpleNamespace(script_dir=str(ga_root)),
        ga=SimpleNamespace(
            GenericAgentHandler=NativeMemoryBase,
            script_dir=str(ga_root),
            file_read=file_read,
        ),
    )
    chat = tmp_path / "chat"
    handler = _make_handler_class(custom)(
        SimpleNamespace(executed=False, _approval_context=None), [], str(chat)
    )
    items, outcome = exhaust(handler.do_start_long_term_update({}, SimpleNamespace(content="")))

    assert items == ["native memory settlement\n"]
    assert reads == [(str(sop), False)]
    assert "NATIVE MEMORY POLICY" in outcome.next_prompt
    assert (memory / "global_mem.txt").as_posix() in outcome.next_prompt
    assert memory.as_posix() in outcome.data
    assert chat.resolve().as_posix() in outcome.next_prompt
    assert not (chat / "session_memory.md").exists()


# TEST-CONTRACT: req=HANDLER-04 | rejects=local UI write executes without interrupt for operator confirmation | gap=no local-UI interrupt+retry test | revert=remove _harness_pending_approval branch or _harness_approved_call short-circuit in do_code_run | mock=FixedGate (gate boundary)=>GATE-AI-01
def test_local_ui_production_write_interrupts_then_allows_exact_retry() -> None:
    handler_class = _make_handler_class(modules())
    command = "kubectl scale deploy api --replicas=3 -n kaic-kis"
    parent = make_parent("approval_required", approval_sink=None, _harness_approved_call=None)
    handler = handler_class(parent, [], ".")
    items, outcome = exhaust(
        handler.do_code_run({"type": "bash", "code": command}, SimpleNamespace(content=""))
    )
    assert outcome.data["status"] == "INTERRUPT"
    assert outcome.data["data"]["candidates"] == ["同意", "输入修改意见"]
    assert parent._harness_pending_approval["review"] == "test review"

    parent._harness_approved_call = parent._harness_pending_approval["fingerprint"]
    items, outcome = exhaust(
        handler.do_code_run({"type": "bash", "code": command}, SimpleNamespace(content=""))
    )
    assert items == ["executed"]
    assert outcome.data["status"] == "success"
    assert parent._harness_approved_call is None


# TEST-CONTRACT: req=HANDLER-05 | rejects=model_fixable verdict executes code or requests approval | gap=no model_fixable path | revert=remove model_fixable branch in do_code_run | mock=FixedGate (gate boundary)=>GATE-MODELFIX-01
def test_model_fixable_verdict_skips_execution() -> None:
    handler_class = _make_handler_class(modules())
    parent = make_parent("model_fixable")
    handler = handler_class(parent, [], ".")
    items, outcome = exhaust(
        handler.do_code_run(
            {"type": "bash", "code": "kubectl apply -f deploy.yaml -n kaic-kis"},
            SimpleNamespace(content=""),
        )
    )
    assert outcome.data["status"] == "skipped"


# TEST-CONTRACT: req=HANDLER-06 | rejects=parallel inline_eval calls overlap while upstream changes process cwd | gap=no process-wide lock | revert=remove _INLINE_EVAL_LOCK | mock=slow upstream code_run boundary
def test_inline_eval_is_serialized_across_handlers() -> None:
    state = {"active": 0, "max": 0}
    lock = threading.Lock()

    class SlowBase(FakeBaseHandler):
        def do_code_run(self, args, response):
            with lock:
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
            time.sleep(0.05)
            with lock:
                state["active"] -= 1
            yield "executed"
            return FakeStepOutcome({"status": "success"})

    custom = GaModules(
        agent_loop=SimpleNamespace(StepOutcome=FakeStepOutcome),
        agentmain=SimpleNamespace(),
        ga=SimpleNamespace(GenericAgentHandler=SlowBase),
    )
    handler_class = _make_handler_class(custom)
    handlers = [handler_class(make_parent("allow"), [], ".") for _ in range(2)]

    threads = [
        threading.Thread(
            target=lambda handler=handler: exhaust(
                handler.do_code_run(
                    {"type": "python", "code": "1 + 1", "inline_eval": True},
                    SimpleNamespace(content=""),
                )
            )
        )
        for handler in handlers
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert state["max"] == 1
