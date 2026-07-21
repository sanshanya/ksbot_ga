from __future__ import annotations

import importlib
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

from .gate import KubectlAiGate

_INLINE_EVAL_LOCK = threading.Lock()


@dataclass(frozen=True)
class GaModules:
    agent_loop: ModuleType
    agentmain: ModuleType
    ga: ModuleType


def load_ga_modules(ga_root: Path) -> GaModules:
    root = str(ga_root.resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    return GaModules(
        importlib.import_module("agent_loop"),
        importlib.import_module("agentmain"),
        importlib.import_module("ga"),
    )


class ApprovalSink(Protocol):
    def request(
        self,
        *,
        chat_id: str,
        user_id: str,
        display_name: str,
        review: str,
        allow_window: bool = True,
    ) -> tuple[bool, str]: ...


@dataclass(frozen=True)
class ApprovalContext:
    chat_id: str
    user_id: str
    display_name: str
    gate: KubectlAiGate
    approval_sink: ApprovalSink


def _new_agent(modules: GaModules) -> Any:
    try:
        return modules.agentmain.GenericAgent()
    except ZeroDivisionError as exc:
        root = Path(getattr(modules.agentmain, "script_dir", ".")).resolve()
        raise RuntimeError(
            "GenericAgent loaded no LLM session. Configure native_oai_config or "
            f"native_claude_config in {root / 'mykey.py'} with apikey, apibase, and model."
        ) from exc


def _memory_context(modules: GaModules, cwd: str) -> tuple[Path, str]:
    ga_root = Path(
        getattr(modules.ga, "script_dir", getattr(modules.agentmain, "script_dir", "."))
    ).resolve()
    root = ga_root / "memory"
    return root, (
        "[Runtime GA memory paths]\n"
        f"L0: {(root / 'memory_management_sop.md').as_posix()}\n"
        f"L1: {(root / 'global_mem_insight.txt').as_posix()}\n"
        f"L2: {(root / 'global_mem.txt').as_posix()}\n"
        f"L3 root: {root.as_posix()}\n"
        f"Current tool cwd remains {Path(cwd).resolve().as_posix()}. "
        "Relative memory paths in native GA instructions refer to the paths above."
    )


def _rewrite_memory_paths(text: str, root: Path) -> str:
    base = root.as_posix()
    for old, new in (
        ("./memory/memory_management_sop.md", f"{base}/memory_management_sop.md"),
        ("../memory/global_mem_insight.txt", f"{base}/global_mem_insight.txt"),
        ("../memory/global_mem.txt", f"{base}/global_mem.txt"),
        ("../memory/", f"{base}/"),
        ("[Memory] (../memory)", f"[Memory] ({base})"),
    ):
        text = text.replace(old, new)
    return text


def make_handler_class(modules: GaModules) -> type:
    base = modules.ga.GenericAgentHandler
    outcome = modules.agent_loop.StepOutcome

    class RuntimeAgentHandler(base):
        def do_ask_user(self, args: dict[str, Any], response: Any):
            question = str(args.get("question") or "Please provide more information.")
            candidates = args.get("candidates") or []
            self.parent._last_response = question + (
                "\n" + "\n".join(f"- {item}" for item in candidates) if candidates else ""
            )
            yield "Waiting for the user's answer.\n"
            return outcome(
                {
                    "status": "INTERRUPT",
                    "intent": "HUMAN_INTERVENTION",
                    "data": {"question": question, "candidates": candidates},
                },
                next_prompt="",
                should_exit=True,
            )

        def do_start_long_term_update(self, args: dict[str, Any], response: Any):
            native = yield from super().do_start_long_term_update(args, response)
            root, paths = _memory_context(modules, self.cwd)
            sop = root / "memory_management_sop.md"
            data = (
                "This is the native L0 memory SOP:\n"
                + _rewrite_memory_paths(
                    str(modules.ga.file_read(str(sop), show_linenos=False)), root
                )
                if sop.is_file()
                else f"Memory Management SOP not found at {sop}. Do not update memory."
            )
            prompt = _rewrite_memory_paths(str(getattr(native, "next_prompt", "") or ""), root)
            return outcome(
                data,
                next_prompt=f"{paths}\n\n{prompt}",
                should_exit=bool(getattr(native, "should_exit", False)),
            )

        def _run_code(self, args: dict[str, Any], response: Any):
            inline = args.get("inline_eval", False)
            inline = inline is True or str(inline).strip().lower() in {"1", "true", "yes", "on"}
            if str(args.get("type", "python")) in {"python", "py"} and inline:
                with _INLINE_EVAL_LOCK:
                    return (yield from super().do_code_run(args, response))
            return (yield from super().do_code_run(args, response))

        def do_code_run(self, args: dict[str, Any], response: Any):
            code_type = str(args.get("type", "python"))
            code = args.get("code") or args.get("script") or self._extract_code_block(
                response, code_type
            )
            context: ApprovalContext | None = getattr(self.parent, "_approval_context", None)
            if not code or not context:
                return (yield from self._run_code(args, response))
            code = str(code)
            verdict = context.gate.review(code, code_type, self.cwd)
            if verdict.decision == "allow":
                return (yield from self._run_code(args, response))
            anchor = self._get_anchor_prompt(skip=args.get("_index", 0) > 0)
            if verdict.decision == "model_fixable":
                return outcome(
                    {"status": "skipped", "reason": verdict.message},
                    next_prompt=anchor
                    + "\n[System] The kubectl gate did not execute the command. "
                    + f"Resolve this missing context: {verdict.message}",
                )
            review = verdict.message
            if verdict.source != "ai_gate":
                facts = [
                    verdict.message,
                    "[Gate evidence: semantic review did not complete]",
                    f"source: {verdict.source}",
                    f"code_type: {code_type}",
                    f"cwd: {Path(self.cwd).resolve()}",
                    f"original code:\n```{code_type}\n{code}\n```",
                ]
                probe = verdict.probe
                for key in ("current_context", "current_namespace", "kubeconfig_env", "error"):
                    if value := str(getattr(probe, key, "") or ""):
                        facts.append(f"{key}: {value}")
                review = "\n".join(facts)
            yield f"[Approval] {review}\n"
            approved, feedback = context.approval_sink.request(
                chat_id=context.chat_id,
                user_id=context.user_id,
                display_name=context.display_name,
                review=review,
                allow_window=verdict.source == "ai_gate",
            )
            if approved:
                return (yield from self._run_code(args, response))
            prompt = anchor + "\n[System] The requested Kubernetes operation was not executed."
            prompt += (
                " The task requester replied with this feedback: "
                f"{feedback!r}. Continue from it and do not retry unchanged."
                if feedback
                else " It was not approved before the timeout. Do not retry unchanged."
            )
            return outcome(
                {"status": "rejected", "feedback": feedback}, next_prompt=prompt
            )

        def turn_end_callback(
            self,
            response: Any,
            tool_calls: list[dict[str, Any]],
            tool_results: list[dict[str, Any]],
            turn: int,
            next_prompt: str,
            exit_reason: dict[str, Any],
        ) -> str:
            result = super().turn_end_callback(
                response, tool_calls, tool_results, turn, next_prompt, exit_reason
            )
            if (not result or exit_reason) and not getattr(self.parent, "_last_response", ""):
                content = str(getattr(response, "content", "") or "")
                self.parent._last_response = re.sub(
                    r"<(?:thinking|summary)>[\s\S]*?</(?:thinking|summary)>",
                    "",
                    content,
                    flags=re.IGNORECASE,
                ).strip()
            return result

    RuntimeAgentHandler.__name__ = "RuntimeAgentHandler"
    return RuntimeAgentHandler
