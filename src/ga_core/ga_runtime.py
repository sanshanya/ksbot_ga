from __future__ import annotations

import copy
import hashlib
import re
import threading
from pathlib import Path
from typing import Any

from .gate import KubectlAiGate
from .ga_handler import (
    ApprovalContext,
    ApprovalSink,
    GaModules,
    _make_handler_class,
    _new_agent_or_raise,
    load_ga_modules,
)

_ATTACHMENT_RE = re.compile(r"\[\[attach:([^\]]+)\]\]")

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
        runtime_observations: tuple[str, ...] = (),
    ) -> tuple[str, tuple[Path, ...]]:
        with self.lock:
            return self._run_locked(
                chat_id=chat_id,
                user_id=user_id,
                display_name=display_name,
                user_text=user_text,
                bootstrap_context=bootstrap_context,
                attachment_paths=attachment_paths,
                runtime_observations=runtime_observations,
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
        runtime_observations: tuple[str, ...],
    ) -> tuple[str, tuple[Path, ...]]:
        self.agent._approval_context = ApprovalContext(
            chat_id=chat_id,
            user_id=user_id,
            display_name=display_name,
            gate=self.gate,
            approval_sink=self.approval_sink,
        )
        self.agent._last_response = ""
        prompt = self._build_user_prompt(user_text, attachment_paths, runtime_observations)
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

    def _build_user_prompt(
        self,
        user_text: str,
        attachment_paths: tuple[Path, ...],
        runtime_observations: tuple[str, ...] = (),
    ) -> str:
        lines = [user_text.strip()]
        if attachment_paths:
            lines.append("\nAttached files were downloaded to:")
            lines.extend(f"- {path}" for path in attachment_paths)
            lines.append("Read them with file_read or code_run when useful.")
        if runtime_observations:
            lines.append("\nRuntime observations (facts from the WPS runtime):")
            lines.extend(f"- {item}" for item in runtime_observations)
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
