from __future__ import annotations

import importlib
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

from .gate import KubectlAiGate, call_fingerprint

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
        agent_loop=importlib.import_module("agent_loop"),
        agentmain=importlib.import_module("agentmain"),
        ga=importlib.import_module("ga"),
    )

class ApprovalSink(Protocol):
    def request(
        self, *, chat_id: str, user_id: str, display_name: str,
        review: str, allow_window: bool = True,
    ) -> tuple[bool, str]: ...


@dataclass
class ApprovalContext:
    chat_id: str = ""
    user_id: str = ""
    display_name: str = ""
    gate: KubectlAiGate | None = None
    approval_sink: ApprovalSink | None = None

def _missing_model_config_message(ga_root: Path) -> str:
    root = ga_root.resolve()
    return (
        "GenericAgent did not load any LLM session. Configure at least one model in "
        f"{root / 'mykey.py'}. Copy mykey_template.py to mykey.py, then enable one "
        "native_oai_config or native_claude_config containing apikey, apibase, and model. "
        "GenericAgent currently turns a missing or invalid model configuration into an empty "
        "llmclients list; this wrapper stops with this explicit message instead."
    )

def _ga_memory_paths(modules: GaModules, handler_cwd: str) -> tuple[Path, str]:
    ga_root = Path(
        getattr(modules.ga, "script_dir", getattr(modules.agentmain, "script_dir", "."))
    ).resolve()
    memory_root = ga_root / "memory"
    path_map = (
        "[Runtime GA memory paths]\n"
        f"L0: {(memory_root / 'memory_management_sop.md').as_posix()}\n"
        f"L1: {(memory_root / 'global_mem_insight.txt').as_posix()}\n"
        f"L2: {(memory_root / 'global_mem.txt').as_posix()}\n"
        f"L3 root: {memory_root.as_posix()}\n"
        f"Current tool cwd remains {Path(handler_cwd).resolve().as_posix()}. "
        "Relative memory paths in the native GA instructions refer to the absolute paths above."
    )
    return memory_root, path_map


def _rewrite_native_memory_paths(text: str, memory_root: Path) -> str:
    root = memory_root.as_posix()
    replacements = (
        ("./memory/memory_management_sop.md", f"{root}/memory_management_sop.md"),
        ("../memory/global_mem_insight.txt", f"{root}/global_mem_insight.txt"),
        ("../memory/global_mem.txt", f"{root}/global_mem.txt"),
        ("../memory/", f"{root}/"),
        ("[Memory] (../memory)", f"[Memory] ({root})"),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    return text

def _new_agent_or_raise(modules: GaModules) -> Any:
    try:
        return modules.agentmain.GenericAgent()
    except ZeroDivisionError as exc:
        ga_root = Path(getattr(modules.agentmain, "script_dir", "."))
        raise RuntimeError(_missing_model_config_message(ga_root)) from exc

def _make_handler_class(modules: GaModules) -> type:
    base = modules.ga.GenericAgentHandler
    step_outcome = modules.agent_loop.StepOutcome

    class RuntimeAgentHandler(base):
        def __init__(self, parent: Any, last_history=None, cwd: str = "./temp") -> None:
            workspace = getattr(parent, "_harness_workspace", None)
            super().__init__(parent, last_history, str(workspace or cwd))

        def do_ask_user(self, args: dict[str, Any], response: Any):
            question = str(args.get("question") or "Please provide more information.")
            candidates = args.get("candidates") or []
            rendered = question
            if candidates:
                rendered += "\n" + "\n".join(f"- {item}" for item in candidates)
            self.parent._last_response = rendered
            yield "Waiting for the user's answer.\n"
            return step_outcome(
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
            memory_root, path_map = _ga_memory_paths(modules, self.cwd)
            l0_path = memory_root / "memory_management_sop.md"
            if l0_path.is_file():
                l0 = str(modules.ga.file_read(str(l0_path), show_linenos=False))
                data = "This is the native L0 memory SOP:\n" + _rewrite_native_memory_paths(
                    l0, memory_root
                )
            else:
                data = f"Memory Management SOP not found at {l0_path}. Do not update memory."
            next_prompt = _rewrite_native_memory_paths(
                str(getattr(native, "next_prompt", "") or ""), memory_root
            )
            return step_outcome(
                data,
                next_prompt=f"{path_map}\n\n{next_prompt}",
                should_exit=bool(getattr(native, "should_exit", False)),
            )

        def _run_upstream_code(self, args: dict[str, Any], response: Any):
            inline = args.get("inline_eval", False)
            inline = inline is True or str(inline).strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            if str(args.get("type", "python")) in {"python", "py"} and inline:
                with _INLINE_EVAL_LOCK:
                    return (yield from super().do_code_run(args, response))
            return (yield from super().do_code_run(args, response))

        def do_code_run(self, args: dict[str, Any], response: Any):
            code_type = str(args.get("type", "python"))
            code = args.get("code") or args.get("script")
            if not code:
                code = self._extract_code_block(response, code_type)
            if not code:
                return (yield from self._run_upstream_code(args, response))
            code = str(code)
            context: ApprovalContext | None = getattr(self.parent, "_approval_context", None)
            fingerprint = call_fingerprint("code_run", code_type, self.cwd, code)
            if getattr(self.parent, "_harness_approved_call", None) == fingerprint:
                self.parent._harness_approved_call = None
                return (yield from self._run_upstream_code(args, response))
            if not context or not context.gate:
                return (yield from self._run_upstream_code(args, response))
            verdict = context.gate.review(code, code_type, self.cwd)
            if verdict.decision == "allow":
                return (yield from self._run_upstream_code(args, response))
            if verdict.decision == "model_fixable":
                next_prompt = self._get_anchor_prompt(skip=args.get("_index", 0) > 0)
                next_prompt += (
                    "\n[System] The kubectl gate did not execute the command. "
                    f"Follow this review and resolve the missing context: {verdict.message}"
                )
                return step_outcome(
                    {"status": "skipped", "reason": verdict.message}, next_prompt=next_prompt
                )
            review = verdict.message
            if getattr(verdict, "source", "") != "ai_gate":
                review_lines = [
                    verdict.message,
                    "[Gate evidence: semantic review did not complete]",
                    f"source: {getattr(verdict, 'source', '')}",
                    f"code_type: {code_type}",
                    f"cwd: {Path(self.cwd).resolve()}",
                    f"original code:\n```{code_type}\n{code}\n```",
                ]
                probe = getattr(verdict, "probe", None)
                for key in ("current_context", "current_namespace", "kubeconfig_env", "error"):
                    value = str(getattr(probe, key, "") or "")
                    if value:
                        review_lines.append(f"{key}: {value}")
                review = "\n".join(review_lines)
            if context.approval_sink:
                yield f"[Approval] {review}\n"
                approved, feedback = context.approval_sink.request(
                    chat_id=context.chat_id,
                    user_id=context.user_id,
                    display_name=context.display_name,
                    review=review,
                    allow_window=getattr(verdict, "source", "") == "ai_gate",
                )
                if approved:
                    return (yield from self._run_upstream_code(args, response))
                next_prompt = self._get_anchor_prompt(skip=args.get("_index", 0) > 0)
                next_prompt += "\n[System] The requested Kubernetes operation was not executed."
                if feedback:
                    next_prompt += (
                        " The task requester replied with this feedback: "
                        f"{feedback!r}. Continue from that feedback and do not retry unchanged."
                    )
                else:
                    next_prompt += (
                        " It was not approved before the timeout. Do not retry unchanged."
                    )
                return step_outcome(
                    {"status": "rejected", "feedback": feedback}, next_prompt=next_prompt
                )
            self.parent._harness_pending_approval = {
                "fingerprint": fingerprint,
                "review": review,
            }
            question = (
                f"{review}\n\n"
                "回复“同意”后执行；其他任何回复都会取消操作，并作为意见交给模型。"
            )
            yield f"[Approval] {question}\n"
            return step_outcome(
                {
                    "status": "INTERRUPT",
                    "intent": "HUMAN_INTERVENTION",
                    "data": {"question": question, "candidates": ["同意", "输入修改意见"]},
                },
                next_prompt="",
                should_exit=True,
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
            if (not result or exit_reason) and not getattr(
                self.parent, "_last_response", ""
            ):
                content = str(getattr(response, "content", "") or "")
                content = re.sub(
                    r"<thinking>[\s\S]*?</thinking>", "", content, flags=re.IGNORECASE
                )
                content = re.sub(
                    r"<summary>[\s\S]*?</summary>", "", content, flags=re.IGNORECASE
                )
                self.parent._last_response = content.strip()
            return result

    RuntimeAgentHandler.__name__ = "RuntimeAgentHandler"
    return RuntimeAgentHandler

