from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path

from .ga_handler import (
    ApprovalContext,
    ApprovalSink,
    GaModules,
    _new_agent,
    load_ga_modules,
    make_handler_class,
)
from .gate import KubectlAiGate

_ATTACHMENT = re.compile(r"\[\[attach:([^\]]+)\]\]")


class GaChatSession:
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
        self.modules, self.workspace = modules, workspace
        workspace.mkdir(parents=True, exist_ok=True)
        self.downloads, self.artifacts = workspace / "downloads", workspace / "artifacts"
        self.downloads.mkdir(exist_ok=True)
        self.artifacts.mkdir(exist_ok=True)
        self.shared_prompt, self.gate = shared_prompt, gate
        self.approval_sink, self.max_turns = approval_sink, max_turns
        self.lock = threading.Lock()
        self._handler_class = make_handler_class(modules)
        self.agent = _new_agent(modules)
        self.agent.verbose = False
        self.agent.task_dir = str(workspace)
        self.agent.extra_sys_prompts = []

    def run(
        self,
        *,
        chat_id: str,
        user_id: str,
        display_name: str,
        user_text: str,
        attachment_paths: tuple[Path, ...] = (),
        runtime_observations: tuple[str, ...] = (),
    ) -> tuple[str, tuple[Path, ...]]:
        with self.lock:
            self.agent._approval_context = ApprovalContext(
                chat_id, user_id, display_name, self.gate, self.approval_sink
            )
            self.agent._last_response = ""
            prompt = self._prompt(user_text, attachment_paths, runtime_observations)
            short = self.modules.ga.smart_format(prompt.replace("\n", " "), max_str_len=200)
            self.agent.history.append(f"[USER]: {short}")
            system = self.modules.agentmain.get_system_prompt() + "\n" + self.shared_prompt
            system += (
                f"\nChat workspace: {self.workspace}\n"
                f"Place deliverable files under {self.artifacts}; attach them with "
                "[[attach:artifacts/FILE_NAME]].\n"
            )
            system += "\n".join(getattr(self.agent, "extra_sys_prompts", []))
            system += str(getattr(self.agent.llmclient.backend, "extra_sys_prompt", ""))
            handler = self._handler_class(self.agent, self.agent.history, str(self.workspace))
            previous = getattr(self.agent, "handler", None)
            if previous and previous.working.get("key_info"):
                handler.working.update(
                    key_info=previous.working["key_info"],
                    related_sop=previous.working.get("related_sop", ""),
                    passed_sessions=previous.working.get("passed_sessions", 0) + 1,
                )
            self.agent.handler = handler
            self.agent.llmclient.log_path = self.agent.log_path
            chunks: list[str] = []
            runner = self.modules.agent_loop.agent_runner_loop(
                self.agent.llmclient,
                system,
                prompt,
                handler,
                self.modules.agentmain.TOOLS_SCHEMA,
                max_turns=self.max_turns,
                verbose=False,
                initial_user_content=prompt,
                yield_info=True,
            )
            self.agent.is_running, self.agent.stop_sig = True, False
            try:
                for item in runner:
                    if isinstance(item, str):
                        chunks.append(item)
                    if self.agent.stop_sig:
                        break
            finally:
                runner.close()
                handler.finish_memory_settlement()
                self.agent.is_running, self.agent.stop_sig = False, False
            self.agent.history = handler.history_info
            final = str(getattr(self.agent, "_last_response", "") or "").strip()
            if not final:
                final = "\n".join(
                    line
                    for line in "".join(chunks).splitlines()
                    if not line.startswith(("Tool:", "[Action]", "[Status]", "[Info]"))
                ).strip()[-12000:]
            text, files = self._attachments(final)
            return text or "Task completed without a user-visible response.", files

    @staticmethod
    def _prompt(
        text: str, paths: tuple[Path, ...], observations: tuple[str, ...]
    ) -> str:
        parts = [text.strip()]
        if paths:
            parts += ["\nAttached files were downloaded to:", *(f"- {path}" for path in paths)]
            parts.append("Local file surfaces: file_read and code_run.")
        if observations:
            parts += ["\nRuntime observations:", *(f"- {item}" for item in observations)]
        return "\n".join(filter(None, parts))

    def _attachments(self, text: str) -> tuple[str, tuple[Path, ...]]:
        files: list[Path] = []
        root = self.workspace.resolve()
        for raw in _ATTACHMENT.findall(text):
            candidate = (root / raw.strip()).resolve()
            if candidate.is_file() and candidate.is_relative_to(root):
                files.append(candidate)
        return _ATTACHMENT.sub("", text).strip(), tuple(dict.fromkeys(files))

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
        self.runtime_root, self.shared_prompt = runtime_root, shared_prompt
        self.gate, self.approval_sink = gate, approval_sink
        self.max_turns = max_turns

    def workspace_for(self, chat_id: str) -> Path:
        readable = re.sub(r"[^A-Za-z0-9_.-]", "_", chat_id)[:64] or "chat"
        digest = hashlib.sha256(chat_id.encode(errors="replace")).hexdigest()[:12]
        return self.runtime_root / "chats" / f"{readable}-{digest}"

    def create(self, chat_id: str) -> GaChatSession:
        return GaChatSession(
            modules=self.modules,
            workspace=self.workspace_for(chat_id),
            shared_prompt=self.shared_prompt,
            gate=self.gate,
            approval_sink=self.approval_sink,
            max_turns=self.max_turns,
        )
