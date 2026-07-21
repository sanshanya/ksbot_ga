from __future__ import annotations

from pathlib import Path

from .gate import KubectlAiGate
from .ga_handler import (
    ApprovalContext,
    GaModules,
    _make_handler_class,
    _missing_model_config_message,
)

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

