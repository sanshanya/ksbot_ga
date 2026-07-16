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
        entries.append(f"- {name}: {description or 'Reusable operating procedure'}\n  path: {path}")
    return (
        "\n## Project skills\n"
        "Skills are Markdown capability packages. Read the relevant SKILL.md with file_read before "
        "acting. A Skill may bundle scripts; run them through GA's existing code_run/file_read tools. "
        "Skills do not add model tools or change the Agent loop. Copy only task-critical points into "
        "working checkpoint.\n"
        + ("\n".join(entries) if entries else "- No project skills installed.")
    )
