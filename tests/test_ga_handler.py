from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from ga_core.ga_handler import ApprovalContext, GaModules, make_handler_class


@dataclass
class Outcome:
    data: object
    next_prompt: str | None = None
    should_exit: bool = False


class BaseHandler:
    def __init__(self, parent, last_history=None, cwd=".") -> None:
        self.parent, self.history_info, self.cwd = parent, last_history or [], cwd
        self.working = {}

    def _extract_code_block(self, *_args):
        return None

    def _get_anchor_prompt(self, skip=False):
        return "anchor"

    def do_code_run(self, args, _response):
        self.parent.calls.append(str(args.get("code") or args.get("script")))
        yield "executed"
        return Outcome({"status": "success"}, "next")


class Gate:
    def __init__(self, decision: str, source="ai_gate", probe=None) -> None:
        self.decision, self.source, self.probe = decision, source, probe

    def review(self, *_args):
        return SimpleNamespace(
            decision=self.decision,
            message="test review",
            source=self.source,
            probe=self.probe,
        )


class Approvals:
    def __init__(self, approved: bool, feedback="") -> None:
        self.result, self.kwargs = (approved, feedback), {}

    def request(self, **kwargs):
        self.kwargs = kwargs
        return self.result


def modules(base=BaseHandler, **ga) -> GaModules:
    return GaModules(
        agent_loop=SimpleNamespace(StepOutcome=Outcome),
        agentmain=SimpleNamespace(**({"script_dir": ga.pop("script_dir")} if "script_dir" in ga else {})),
        ga=SimpleNamespace(GenericAgentHandler=base, **ga),
    )


def parent(decision: str, approvals=None, **gate_kwargs):
    approvals = approvals or Approvals(False)
    return SimpleNamespace(
        calls=[],
        _approval_context=ApprovalContext(
            chat_id="c",
            user_id="u",
            display_name="Alice",
            gate=Gate(decision, **gate_kwargs),
            approval_sink=approvals,
        ),
    )


def exhaust(generator):
    items = []
    try:
        while True:
            items.append(next(generator))
    except StopIteration as stop:
        return items, stop.value


def run(handler, code="kubectl delete pod x -n kaic-kis"):
    return exhaust(
        handler.do_code_run({"type": "bash", "code": code}, SimpleNamespace(content=""))
    )


def test_gate_outcomes_control_execution() -> None:
    allowed = parent("allow")
    items, outcome = run(make_handler_class(modules())(allowed, [], "."), "echo ok")
    assert items == ["executed"] and outcome.data["status"] == "success"
    assert allowed.calls == ["echo ok"]

    fixable = parent("model_fixable")
    _, outcome = run(make_handler_class(modules())(fixable, [], "."))
    assert outcome.data["status"] == "skipped" and fixable.calls == []

    approvals = Approvals(False, "先检查流量")
    denied = parent("approval_required", approvals, source="fail_closed")
    _, outcome = run(make_handler_class(modules())(denied, [], "."))
    assert outcome.data["status"] == "rejected" and denied.calls == []
    assert "先检查流量" in outcome.next_prompt
    assert approvals.kwargs["allow_window"] is False


def test_fail_closed_review_contains_original_call_and_environment() -> None:
    probe = SimpleNamespace(
        current_context="qy-online",
        current_namespace="kaic-kis",
        kubeconfig_env="KUBECONFIG=/config/kubeconfig",
        error="",
    )
    approvals = Approvals(False)
    operator = parent("approval_required", approvals, source="fail_closed", probe=probe)
    operator._approval_context.gate.review = lambda *_args: SimpleNamespace(
        decision="approval_required",
        message="AI gate unavailable: connection refused",
        source="fail_closed",
        probe=probe,
    )
    command = "kubectl delete pod api-0 -n kaic-kis"
    run(make_handler_class(modules())(operator, [], "C:/agent/workspace"), command)
    review = approvals.kwargs["review"]
    for value in (command, "code_type: bash", "current_context: qy-online", "kaic-kis", "KUBECONFIG"):
        assert value in review


def test_approved_call_resumes_the_exact_original_once() -> None:
    approvals = Approvals(True)
    operator = parent("approval_required", approvals)
    command = "kubectl scale deploy api --replicas=3 -n kaic-kis"
    items, outcome = run(make_handler_class(modules())(operator, [], "."), command)
    assert items == ["[Approval] test review\n", "executed"]
    assert operator.calls == [command]
    assert outcome.data["status"] == "success"


def test_long_term_memory_paths_and_cross_chat_serialization(tmp_path) -> None:
    root = tmp_path / "ga"
    memory = root / "memory"
    memory.mkdir(parents=True)
    (memory / "memory_management_sop.md").write_text(
        "L2: global_mem.txt\nL3: ../memory/", encoding="utf-8"
    )

    class NativeMemory(BaseHandler):
        def do_start_long_term_update(self, *_args):
            yield "native\n"
            return Outcome("native", "Use ../memory/global_mem.txt and ../memory/")

    custom = modules(
        NativeMemory,
        script_dir=str(root),
        file_read=lambda path, show_linenos=False: Path(path).read_text(encoding="utf-8"),
    )
    handler_class = make_handler_class(custom)
    first = handler_class(SimpleNamespace(), [], str(tmp_path / "first"))
    items, outcome = exhaust(first.do_start_long_term_update({}, SimpleNamespace(content="")))
    assert items == ["native\n"]
    assert (memory / "global_mem.txt").as_posix() in outcome.next_prompt
    assert memory.as_posix() in outcome.data

    second = handler_class(SimpleNamespace(), [], str(tmp_path / "second"))
    finished = []

    def settle_second():
        exhaust(second.do_start_long_term_update({}, SimpleNamespace(content="")))
        finished.append(True)
        second.finish_memory_settlement()

    thread = threading.Thread(target=settle_second)
    thread.start()
    thread.join(0.05)
    assert not finished
    first.finish_memory_settlement()
    thread.join(1)
    assert finished

def test_inline_python_eval_is_serialized() -> None:
    state = {"active": 0, "max": 0}
    lock = threading.Lock()

    class Slow(BaseHandler):
        def do_code_run(self, *_args):
            with lock:
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
            time.sleep(0.05)
            with lock:
                state["active"] -= 1
            yield "executed"
            return Outcome({"status": "success"})

    handler_class = make_handler_class(modules(Slow))
    handlers = [handler_class(parent("allow"), [], ".") for _ in range(2)]
    threads = [
        threading.Thread(
            target=lambda h=h: exhaust(
                h.do_code_run(
                    {"type": "python", "code": "1 + 1", "inline_eval": True},
                    SimpleNamespace(content=""),
                )
            )
        )
        for h in handlers
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert state["max"] == 1
