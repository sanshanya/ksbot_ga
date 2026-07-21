"""Token-load ratchet for repository totals and common change surfaces."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PART = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\s]")
MODULES = {
    "core.handler": "src/ga_core/ga_handler.py",
    "core.runtime": "src/ga_core/ga_runtime.py",
    "core.gate": "src/ga_core/gate.py",
    "core.config": "src/ga_core/config.py src/ga_core/__init__.py",
    "core.skills": "src/ga_core/skills.py",
    "wps.protocol": "src/ga_wps/protocol.py",
    "wps.client": "src/ga_wps/client.py",
    "wps.callback": "src/ga_wps/callback.py",
    "wps.history": "src/ga_wps/history.py",
    "wps.service": "src/ga_wps/app.py src/ga_wps/__init__.py",
    "wps.config": "src/ga_wps/config.py",
    "wps.approval": "src/ga_wps/approval.py",
    "wps.bridge": "bridge/**/*.mjs bridge/package.json",
    "wps.skill-cli": "skills/wps-chat/scripts/**/*.py",
    "tooling": "scripts/**/*.py",
    "config": "config/**/*.md config/**/*.yaml .env.example pyproject.toml",
    "examples": "examples/**/*.py",
    "tests.core.handler": "tests/test_ga_handler.py tests/test_ga_upstream_seam.py",
    "tests.core.runtime": "tests/test_ga_session.py",
    "tests.core.gate": "tests/test_gate.py",
    "tests.core.config": "tests/test_config.py",
    "tests.wps.protocol": "tests/test_wps_protocol.py",
    "tests.wps.client": "tests/test_wps_client.py",
    "tests.wps.callback": "tests/test_wps_callback.py",
    "tests.wps.config": "tests/test_wps_config.py",
    "tests.wps.service": "tests/test_dispatch.py",
    "tests.wps.history": "tests/test_wps_skill.py tests/test_wps_live.py",
    "tests.wps.approval": "tests/test_approval.py",
    "tests.wps.bridge": "tests/test_wps_bridge.py",
    "tests.tooling": "tests/test_fetch_ga.py tests/test_configure_ga_local.py",
    "docs": "README.md docs/**/*.md vendor/README.md",
    "skills": "skills/**/*.md",
}
TRACKED = "src/**/*.py bridge/**/*.mjs bridge/package.json skills/**/*.py scripts/**/*.py tests/**/*.py examples/**/*.py config/**/*.md config/**/*.yaml docs/**/*.md skills/**/*.md README.md vendor/README.md .env.example pyproject.toml"
GROUPS = {
    "production": "core.handler core.runtime core.gate core.config core.skills wps.protocol wps.client wps.callback wps.history wps.service wps.config wps.approval wps.bridge wps.skill-cli config skills",
    "tests": "tests.core.handler tests.core.runtime tests.core.gate tests.core.config tests.wps.protocol tests.wps.client tests.wps.callback tests.wps.config tests.wps.service tests.wps.history tests.wps.approval tests.wps.bridge tests.tooling",
    "guidance": "docs",
    "tooling": "tooling examples",
}
SURFACES = {
    "ga-handler": "core.handler core.gate tests.core.handler tests.core.gate",
    "ga-session": "core.runtime core.handler tests.core.runtime",
    "wps-ingress": "wps.bridge wps.protocol wps.callback wps.config tests.wps.bridge tests.wps.protocol tests.wps.callback tests.wps.config",
    "wps-client": "wps.client wps.protocol tests.wps.client",
    "wps-history": "wps.history wps.client wps.protocol wps.skill-cli tests.wps.history",
    "wps-service": "wps.service wps.config wps.protocol wps.history wps.approval tests.wps.service tests.wps.approval",
    "k8s-write": "core.handler core.gate wps.service wps.approval tests.core.handler tests.core.gate tests.wps.service tests.wps.approval",
}
LIMITS = {
    "total": 59998,
    "groups": dict(zip(GROUPS, map(int, "33815 18195 2265 5723".split()))),
    "surfaces": dict(
        zip(SURFACES, map(int, "11508 5567 8585 5336 10776 16326 22407".split()))
    ),
}


def tokens(text: str) -> int:
    return sum(
        (len(value) + 3) // 4 if value[0].isascii() and (value[0].isalnum() or value[0] == "_") else 1
        for match in PART.finditer(text)
        if (value := match.group())
    )


def scan(root: Path):
    totals, assigned = {}, set()
    for name, spec in MODULES.items():
        paths = {
            path
            for pattern in spec.split()
            for path in root.glob(pattern)
            if path.is_file() and "node_modules" not in path.parts
        } - assigned
        assigned.update(paths)
        totals[name] = sum(tokens(path.read_text(encoding="utf-8", errors="replace")) for path in paths)
    tracked = {
        path
        for pattern in TRACKED.split()
        for path in root.glob(pattern)
        if path.is_file() and "node_modules" not in path.parts
    }
    unassigned = sorted(path.relative_to(root).as_posix() for path in tracked - assigned)
    def aggregate(mapping):
        return {
            name: sum(totals[module] for module in spec.split())
            for name, spec in mapping.items()
        }

    return totals, aggregate(GROUPS), aggregate(SURFACES), unassigned


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    modules, groups, surfaces, unassigned = scan(args.root)
    total = sum(modules.values())
    failures = [f"unassigned:{path}" for path in unassigned]
    failures += [f"group:{name}" for name, limit in LIMITS["groups"].items() if groups[name] > limit]
    failures += [f"surface:{name}" for name, limit in LIMITS["surfaces"].items() if surfaces[name] > limit]
    failures += ["total"] if total > LIMITS["total"] else []
    result = {
        "module_totals": modules,
        "groups": groups,
        "task_surfaces": surfaces,
        "total": total,
        "limits": LIMITS,
        "unassigned": unassigned,
        "failures": failures,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for name, value in groups.items():
            print(f"{name:10} {value:6}")
        print(f"total      {total:6}/{LIMITS['total']}")
        for name, value in surfaces.items():
            print(f"{name:12} {value:6}")
        if failures:
            print("FAIL:", ", ".join(failures))
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
