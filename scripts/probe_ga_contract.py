from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

root = Path(os.getenv("GA_ROOT", Path(__file__).resolve().parents[1] / "vendor" / "GenericAgent"))
sys.path.insert(0, str(root.resolve()))

agent_loop = importlib.import_module("agent_loop")
agentmain = importlib.import_module("agentmain")
ga = importlib.import_module("ga")

assert callable(agent_loop.agent_runner_loop)
assert hasattr(agent_loop, "StepOutcome")
assert hasattr(agentmain, "GenericAgent")
assert hasattr(agentmain, "GeneraticAgent")
assert isinstance(agentmain.TOOLS_SCHEMA, list)
assert hasattr(ga.GenericAgentHandler, "do_code_run")
assert callable(ga.smart_format)
for name in ("run", "put_task", "abort", "list_llms", "next_llm", "get_llm_name"):
    assert hasattr(agentmain.GenericAgent, name), name
assert root.joinpath("frontends", "tui_v3.py").is_file()
assert root.joinpath("frontends", "stapp2.py").is_file()
print("GenericAgent contract probe: PASS")
