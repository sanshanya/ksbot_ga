from __future__ import annotations

import re
from pathlib import Path


def build_skill_prompt(skills_root: Path) -> str:
    entries: list[str] = []
    for path in sorted(skills_root.glob("*/SKILL.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        name = path.parent.name
        description = ""
        match = re.search(r"^description:\s*(.+)$", text, flags=re.MULTILINE)
        if match:
            description = match.group(1).strip().strip("'\"")
        entries.append(f"- {name}: {description or 'Capability package'}\n  path: {path}")
    return (
        "\n## Project capability catalog\n"
        "Skills are Markdown capability packages. Their paths expose the available capability "
        "contracts; bundled scripts use GA's existing code_run and file_read surfaces. Skills do "
        "not add model tools or change the Agent loop.\n"
        + ("\n".join(entries) if entries else "- No project skills installed.")
    )
