"""Audit deterministic estimated LLM tokens by functional module.

CJK characters and punctuation count as one token; ASCII identifier/text runs count as
one token per four characters. This is a stable repository metric, not provider billing.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_PART = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\s]")
MODULE_LIMIT = 30_000
MODULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("core.runtime", ("src/ga_core/ga_runtime.py",)),
    ("core.gate", ("src/ga_core/gate.py",)),
    ("core.config-skills", ("src/ga_core/config.py", "src/ga_core/skills.py", "src/ga_core/__init__.py")),
    ("wps.protocol", ("src/ga_wps/wps.py",)),
    ("wps.history", ("src/ga_wps/history.py",)),
    ("wps.service", ("src/ga_wps/app.py", "src/ga_wps/config.py", "src/ga_wps/__init__.py")),
    ("wps.approval", ("src/ga_wps/approval.py",)),
    ("wps.bridge", ("bridge/**/*.mjs", "bridge/package.json")),
    ("wps.ui", ("src/ga_wps/ui*.py",)),
    ("wps.skill-cli", ("skills/wps-chat/scripts/**/*.py",)),
    ("tooling", ("scripts/**/*.py",)),
    ("config", ("config/**/*.md", "config/**/*.yaml", ".env.example", "pyproject.toml")),
    ("examples", ("examples/**/*.py",)),
    ("tests.core", ("tests/test_config.py", "tests/test_ga_*.py", "tests/test_gate.py", "tests/test_ui_agent.py")),
    ("tests.wps", ("tests/test_approval.py", "tests/test_dispatch.py", "tests/test_wps*.py")),
    ("tests.tooling", ("tests/test_fetch_ga.py", "tests/test_configure_ga_local.py")),
    ("docs", ("README.md", "docs/**/*.md", "vendor/README.md")),
    ("skills", ("skills/**/*.md",)),
)
TRACKED = (
    "src/**/*.py", "bridge/**/*.mjs", "bridge/package.json", "skills/**/*.py",
    "scripts/**/*.py", "tests/**/*.py", "examples/**/*.py", "config/**/*.md",
    "config/**/*.yaml", "docs/**/*.md", "skills/**/*.md", "README.md",
    "vendor/README.md", ".env.example", "pyproject.toml",
)


def estimate_tokens(text: str) -> int:
    return sum(
        (len(part) + 3) // 4
        if part[0].isascii() and (part[0].isalnum() or part[0] == "_")
        else 1
        for match in _PART.finditer(text)
        if (part := match.group())
    )


def scan(root: Path) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    modules: dict[str, dict[str, int]] = {}
    assigned: set[Path] = set()
    for name, patterns in MODULES:
        files: dict[str, int] = {}
        for pattern in patterns:
            for path in root.glob(pattern):
                if path.is_file() and "node_modules" not in path.parts and path not in assigned:
                    assigned.add(path)
                    files[path.relative_to(root).as_posix()] = estimate_tokens(
                        path.read_text(encoding="utf-8", errors="replace")
                    )
        modules[name] = dict(sorted(files.items()))
    tracked = {
        path
        for pattern in TRACKED
        for path in root.glob(pattern)
        if path.is_file() and "node_modules" not in path.parts
    }
    unassigned = {
        path.relative_to(root).as_posix(): estimate_tokens(
            path.read_text(encoding="utf-8", errors="replace")
        )
        for path in sorted(tracked - assigned)
    }
    return modules, unassigned


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit estimated LLM tokens by module.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--limit", type=int, default=MODULE_LIMIT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    modules, unassigned = scan(args.root)
    totals = {name: sum(files.values()) for name, files in modules.items()}
    result = {
        "estimator": "cjk/punctuation=1; ascii-run=ceil(chars/4)",
        "module_limit": args.limit,
        "modules": modules,
        "module_totals": totals,
        "total": sum(totals.values()),
        "unassigned": unassigned,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Estimated LLM tokens by functional module (not provider billing tokens)")
        for name, files in modules.items():
            total = totals[name]
            print(f"\n=== {name} {'PASS' if total <= args.limit else 'FAIL'}: {total}/{args.limit} ===")
            for path, count in files.items():
                print(f"{count:6d}  {path}")
        if unassigned:
            print("\n=== Unassigned maintained files (FAIL) ===")
            for path, count in unassigned.items():
                print(f"{count:6d}  {path}")
        print(f"\nTOTAL estimated tokens: {result['total']}")
    if unassigned or any(total > args.limit for total in totals.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
