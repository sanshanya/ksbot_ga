from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path

from ga_core.config import CoreSettings
from ga_core.ga_runtime import install_ui_agent, load_ga_modules
from ga_core.gate import KubectlAiGate
from ga_core.skills import build_skill_prompt


def prepare_ui() -> tuple[CoreSettings, object]:
    """Install HarnessAgent into the current process before loading an upstream GA UI."""
    project_root = Path(os.getenv("GA_WPS_PROJECT_ROOT", Path.cwd())).resolve()
    settings = CoreSettings.from_env(project_root)
    settings = settings.resolve_gate_config()
    settings.validate()
    if not settings.ga_root.joinpath("agentmain.py").is_file():
        raise RuntimeError(
            f"GenericAgent not found at {settings.ga_root}. Run scripts/fetch_ga.py first."
        )
    modules = load_ga_modules(settings.ga_root)
    gate = KubectlAiGate(
        inventory_path=settings.cluster_config,
        base_url=settings.gate_base_url,
        api_key=settings.gate_api_key,
        model=settings.gate_model,
        timeout=settings.gate_timeout_seconds,
        probe_timeout=settings.kubectl_probe_timeout_seconds,
    )
    prompt = (project_root / "config" / "system_prompt.md").read_text(encoding="utf-8")
    prompt += build_skill_prompt(project_root / "skills")
    workspace = Path(
        os.getenv("GA_WPS_UI_WORKSPACE", settings.runtime_root / "local-ui")
    ).resolve()
    install_ui_agent(
        modules,
        workspace=workspace,
        shared_prompt=prompt,
        gate=gate,
    )
    return settings, modules


def main_tui() -> None:
    settings, _ = prepare_ui()
    script = settings.ga_root / "frontends" / "tui_v3.py"
    if not script.is_file():
        raise RuntimeError(f"GA TUI not found: {script}")
    runpy.run_path(str(script), run_name="__main__")


def streamlit_entry() -> None:
    settings, _ = prepare_ui()
    script = settings.ga_root / "frontends" / "stapp2.py"
    if not script.is_file():
        raise RuntimeError(f"GA Streamlit UI not found: {script}")
    runpy.run_path(str(script), run_name="__main__")


def main_streamlit() -> None:
    wrapper = Path(__file__).with_name("ui_streamlit.py")
    raise SystemExit(
        subprocess.call([sys.executable, "-m", "streamlit", "run", str(wrapper)])
    )
