from __future__ import annotations

import copy
import hashlib
import importlib
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

from .gate import KubectlAiGate, call_fingerprint


_ATTACHMENT_RE = re.compile(r"\[\[attach:([^\]]+)\]\]")
_INLINE_EVAL_LOCK = threading.Lock()


def _log_ui_approval(
    workspace: Path, pending: dict, approved: bool, feedback: str = ""
) -> None:
    import json
    import time

    record = {
        "timestamp": int(time.time()),
        "mode": "tui",
        "approved": approved,
        "review": pending.get("review", ""),
        "feedback": feedback,
    }
    audit_path = workspace / "approval.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


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
            if context.approval_sink:
                yield f"[Approval] {verdict.message}\n"
                approved, feedback = context.approval_sink.request(
                    chat_id=context.chat_id,
                    user_id=context.user_id,
                    display_name=context.display_name,
                    review=verdict.message,
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
                "review": verdict.message,
            }
            question = (
                f"{verdict.message}\n\n"
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


def install_ui_agent(
    modules: GaModules,
    *,
    workspace: Path,
    shared_prompt: str,
    gate: KubectlAiGate,
) -> type:
    """Patch GA entrypoints in-memory so upstream UIs create our compatible Agent."""
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "artifacts").mkdir(exist_ok=True)
    handler_class = _make_handler_class(modules)
    base = modules.agentmain.GenericAgent

    class HarnessAgent(base):
        def __init__(self) -> None:
            try:
                super().__init__()
            except ZeroDivisionError as exc:
                ga_root = Path(getattr(modules.agentmain, "script_dir", "."))
                raise RuntimeError(_missing_model_config_message(ga_root)) from exc
            self._harness_workspace = workspace
            self._harness_pending_approval = None
            self._harness_approved_call = None
            self._approval_context = ApprovalContext(
                chat_id="local-ui",
                user_id="local-operator",
                display_name="Local operator",
                gate=gate,
            )
            self.task_dir = str(workspace)
            self.extra_sys_prompts.append(shared_prompt)
            self.extra_sys_prompts.append(
                f"\nLocal UI workspace: {workspace}. "
                f"Place deliverable files under {workspace / 'artifacts'}."
            )

        def put_task(self, query, source="user", images=None):
            pending = getattr(self, "_harness_pending_approval", None)
            if pending:
                feedback = str(query or "").strip()
                approved = feedback.casefold() in {"同意", "approve"}
                self._harness_pending_approval = None
                _log_ui_approval(
                    self._harness_workspace, pending, approved, "" if approved else feedback
                )
                if approved:
                    self._harness_approved_call = pending["fingerprint"]
                    query = (
                        "The operator approved the previously reviewed Kubernetes operation. "
                        "Retry the exact command once now and report the actual result."
                    )
                else:
                    query = (
                        "The reviewed Kubernetes operation was not executed. The operator replied: "
                        f"{feedback!r}. Continue from this feedback and do not retry unchanged."
                    )
            return super().put_task(query, source=source, images=images)

    HarnessAgent.__name__ = "HarnessAgent"
    modules.agentmain.GenericAgentHandler = handler_class
    modules.agentmain.GenericAgent = HarnessAgent
    modules.agentmain.GeneraticAgent = HarnessAgent
    return HarnessAgent


class GaChatSession:
    """One GenericAgent instance and backend history per chat."""

    def __init__(
        self,
        *,
        modules: GaModules,
        workspace: Path,
        shared_prompt: str,
        gate: KubectlAiGate,
        approval_sink: ApprovalSink,
        max_turns: int,
    ) -> None:
        self.modules = modules
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.downloads = self.workspace / "downloads"
        self.artifacts = self.workspace / "artifacts"
        self.downloads.mkdir(exist_ok=True)
        self.artifacts.mkdir(exist_ok=True)
        self.shared_prompt = shared_prompt
        self.gate = gate
        self.approval_sink = approval_sink
        self.max_turns = max_turns
        self.lock = threading.Lock()
        self._first_run = True
        self._handler_class = _make_handler_class(modules)
        self.agent = _new_agent_or_raise(modules)
        self.agent.verbose = False
        self.agent.task_dir = str(self.workspace)
        self.agent.extra_sys_prompts = []
        self._tools_schema = self._filtered_tools_schema(modules.agentmain.TOOLS_SCHEMA)

    @staticmethod
    def _filtered_tools_schema(schema: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return copy.deepcopy(schema)

    def run(
        self,
        *,
        chat_id: str,
        user_id: str,
        display_name: str,
        user_text: str,
        bootstrap_context: str = "",
        attachment_paths: tuple[Path, ...] = (),
    ) -> tuple[str, tuple[Path, ...]]:
        with self.lock:
            return self._run_locked(
                chat_id=chat_id,
                user_id=user_id,
                display_name=display_name,
                user_text=user_text,
                bootstrap_context=bootstrap_context,
                attachment_paths=attachment_paths,
            )

    def _run_locked(
        self,
        *,
        chat_id: str,
        user_id: str,
        display_name: str,
        user_text: str,
        bootstrap_context: str,
        attachment_paths: tuple[Path, ...],
    ) -> tuple[str, tuple[Path, ...]]:
        self.agent._approval_context = ApprovalContext(
            chat_id=chat_id,
            user_id=user_id,
            display_name=display_name,
            gate=self.gate,
            approval_sink=self.approval_sink,
        )
        self.agent._last_response = ""
        prompt = self._build_user_prompt(user_text, attachment_paths)
        initial_content = prompt
        if self._first_run and bootstrap_context:
            initial_content = (
                "The runtime pre-ran a capability documented in a project Skill. "
                "The observation states its source and how to refresh it with GA base tools. "
                f"The final block is the current user request.\n\n<bootstrap_observation>\n"
                f"{bootstrap_context}\n</bootstrap_observation>\n\n"
                f"<current_request>\n{prompt}\n</current_request>"
            )
        self._first_run = False

        short = self.modules.ga.smart_format(prompt.replace("\n", " "), max_str_len=200)
        self.agent.history.append(f"[USER]: {short}")
        system_prompt = self.modules.agentmain.get_system_prompt()
        system_prompt += "\n" + self.shared_prompt
        system_prompt += (
            f"\nChat workspace: {self.workspace}\n"
            f"Place user-deliverable files under {self.artifacts}. "
            "To send a file, include [[attach:artifacts/FILE_NAME]] in the final response.\n"
            f"Optional chat-local memory: {self.workspace / 'session_memory.md'}. Use file tools "
            "only for stable facts specific to this chat; it is not a transcript and does not "
            "replace GA global L2/L3/SOP memory.\n"
        )
        system_prompt += "\n".join(getattr(self.agent, "extra_sys_prompts", []))
        system_prompt += str(getattr(self.agent.llmclient.backend, "extra_sys_prompt", ""))

        handler = self._handler_class(self.agent, self.agent.history, str(self.workspace))
        old_handler = getattr(self.agent, "handler", None)
        if old_handler and old_handler.working.get("key_info"):
            handler.working["key_info"] = old_handler.working["key_info"]
            handler.working["related_sop"] = old_handler.working.get("related_sop", "")
            handler.working["passed_sessions"] = old_handler.working.get("passed_sessions", 0) + 1
        self.agent.handler = handler
        self.agent.llmclient.log_path = self.agent.log_path

        generator = self.modules.agent_loop.agent_runner_loop(
            self.agent.llmclient,
            system_prompt,
            prompt,
            handler,
            self._tools_schema,
            max_turns=self.max_turns,
            verbose=False,
            initial_user_content=initial_content,
            yield_info=True,
        )
        chunks: list[str] = []
        self.agent.is_running = True
        self.agent.stop_sig = False
        try:
            while True:
                try:
                    item = next(generator)
                except StopIteration:
                    break
                if isinstance(item, str):
                    chunks.append(item)
                if self.agent.stop_sig:
                    break
        finally:
            generator.close()
            self.agent.is_running = False
            self.agent.stop_sig = False
        self.agent.history = handler.history_info
        final = str(getattr(self.agent, "_last_response", "") or "").strip()
        if not final:
            final = self._fallback_text("".join(chunks))
        text, files = self._extract_attachments(final)
        return text or "Task completed without a user-visible response.", files

    def _build_user_prompt(self, user_text: str, attachment_paths: tuple[Path, ...]) -> str:
        lines = [user_text.strip()]
        if attachment_paths:
            lines.append("\nAttached files were downloaded to:")
            lines.extend(f"- {path}" for path in attachment_paths)
            lines.append("Read them with file_read or code_run when useful.")
        return "\n".join(line for line in lines if line)

    def _extract_attachments(self, text: str) -> tuple[str, tuple[Path, ...]]:
        files: list[Path] = []
        for raw in _ATTACHMENT_RE.findall(text):
            candidate = (self.workspace / raw.strip()).resolve()
            try:
                candidate.relative_to(self.workspace.resolve())
            except ValueError:
                continue
            if candidate.is_file():
                files.append(candidate)
        return _ATTACHMENT_RE.sub("", text).strip(), tuple(dict.fromkeys(files))

    @staticmethod
    def _fallback_text(chunks: str) -> str:
        lines = []
        for line in chunks.splitlines():
            if line.startswith("Tool:") or line.startswith("[Action]"):
                continue
            if line.startswith("[Status]") or line.startswith("[Info]"):
                continue
            lines.append(line)
        return "\n".join(lines).strip()[-12000:]

    def abort(self) -> None:
        self.agent.abort()


class GaSessionFactory:
    def __init__(
        self,
        *,
        ga_root: Path,
        runtime_root: Path,
        shared_prompt: str,
        gate: KubectlAiGate,
        approval_sink: ApprovalSink,
        max_turns: int,
    ) -> None:
        self.modules = load_ga_modules(ga_root)
        self.runtime_root = runtime_root
        self.shared_prompt = shared_prompt
        self.gate = gate
        self.approval_sink = approval_sink
        self.max_turns = max_turns

    def workspace_for(self, chat_id: str) -> Path:
        readable = re.sub(r"[^A-Za-z0-9_.-]", "_", chat_id)[:64] or "chat"
        digest = hashlib.sha256(chat_id.encode("utf-8", errors="replace")).hexdigest()[:12]
        return self.runtime_root / "chats" / f"{readable}-{digest}"

    def create(self, chat_id: str) -> GaChatSession:
        workspace = self.workspace_for(chat_id)
        return GaChatSession(
            modules=self.modules,
            workspace=workspace,
            shared_prompt=self.shared_prompt,
            gate=self.gate,
            approval_sink=self.approval_sink,
            max_turns=self.max_turns,
        )
