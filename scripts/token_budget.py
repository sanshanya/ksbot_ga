"""Repository and task-surface token ratchet."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PART = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\s]")
MODULES = {
    "core.handler": "src/ga_core/ga_handler.py",
    "core.runtime": "src/ga_core/ga_runtime.py",
    "core.gate": "src/ga_core/gate.py",
    "core.support": "src/ga_core/config.py src/ga_core/skills.py src/ga_core/__init__.py",
    "wps.protocol": "src/ga_wps/protocol.py",
    "wps.client": "src/ga_wps/client.py",
    "wps.callback": "src/ga_wps/callback.py",
    "wps.history": "src/ga_wps/history.py",
    "wps.service": "src/ga_wps/app.py src/ga_wps/__init__.py",
    "wps.config": "src/ga_wps/config.py",
    "wps.approval": "src/ga_wps/approval.py",
    "wps.bridge": "bridge/**/*.mjs bridge/package.json",
    "wps.skill": "skills/wps-chat/scripts/**/*.py",
    "config": "config/**/*.md config/**/*.yaml .env.example pyproject.toml skills/**/*.md",
    "tooling": "scripts/**/*.py examples/**/*.py",
    "test.core.handler": "tests/test_ga_handler.py tests/test_ga_upstream_seam.py",
    "test.core.runtime": "tests/test_ga_session.py",
    "test.core.gate": "tests/test_gate.py",
    "test.core.config": "tests/test_config.py",
    "test.wps.protocol": "tests/test_wps_protocol.py",
    "test.wps.client": "tests/test_wps_client.py",
    "test.wps.callback": "tests/test_wps_callback.py",
    "test.wps.config": "tests/test_wps_config.py",
    "test.wps.service": "tests/test_dispatch.py",
    "test.wps.history": "tests/test_wps_skill.py tests/test_wps_live.py",
    "test.wps.approval": "tests/test_approval.py",
    "test.wps.bridge": "tests/test_wps_bridge.py",
    "test.tooling": "tests/test_fetch_ga.py tests/test_configure_ga_local.py",
    "docs": "README.md docs/**/*.md vendor/README.md",
}
TRACKED = "src/**/*.py bridge/**/*.mjs bridge/package.json skills/**/* scripts/**/*.py tests/**/*.py examples/**/*.py config/**/* docs/**/*.md README.md vendor/README.md .env.example pyproject.toml"
SURFACES = {
    "ga-handler": "core.handler core.gate test.core.handler test.core.gate",
    "ga-session": "core.runtime core.handler test.core.runtime",
    "wps-ingress": "wps.bridge wps.protocol wps.callback wps.config test.wps.bridge test.wps.protocol test.wps.callback test.wps.config",
    "wps-client": "wps.client wps.protocol test.wps.client",
    "wps-history": "wps.history wps.client wps.protocol wps.skill test.wps.history",
    "wps-service": "wps.service wps.config wps.protocol wps.history wps.approval test.wps.service test.wps.approval",
    "k8s-write": "core.handler core.gate wps.service wps.approval test.core.handler test.core.gate test.wps.service test.wps.approval",
}
LIMITS = {
    "total": 60464,
    "groups": {"production": 34296, "tests": 18796, "guidance": 1904, "tooling": 5468},
    "surfaces": dict(zip(SURFACES, map(int, "11796 5865 8562 5336 10776 17132 23436".split()))),
}


def tokens(text: str) -> int:
    return sum(
        (len(v) + 3) // 4 if v[0].isascii() and (v[0].isalnum() or v[0] == "_") else 1
        for match in PART.finditer(text)
        if (v := match.group())
    )


def scan(root: Path):
    totals, assigned = {}, set()
    for name, spec in MODULES.items():
        paths = {
            p for pattern in spec.split() for p in root.glob(pattern)
            if p.is_file() and "node_modules" not in p.parts and "__pycache__" not in p.parts
        } - assigned
        assigned |= paths
        totals[name] = sum(tokens(p.read_text(encoding="utf-8", errors="replace")) for p in paths)
    tracked = {
        p for pattern in TRACKED.split() for p in root.glob(pattern)
        if p.is_file() and "node_modules" not in p.parts and "__pycache__" not in p.parts
    }
    tests = sum(v for k, v in totals.items() if k.startswith("test."))
    groups = {
        "tests": tests,
        "guidance": totals["docs"],
        "tooling": totals["tooling"],
    }
    groups["production"] = sum(totals.values()) - sum(groups.values())
    surfaces = {name: sum(totals[k] for k in spec.split()) for name, spec in SURFACES.items()}
    return totals, groups, surfaces, sorted(p.relative_to(root).as_posix() for p in tracked - assigned)


def main() -> None:
    modules, groups, surfaces, unassigned = scan(Path.cwd())
    total = sum(modules.values())
    failures = [f"unassigned:{p}" for p in unassigned]
    failures += [f"group:{k}" for k, limit in LIMITS["groups"].items() if groups[k] > limit]
    failures += [f"surface:{k}" for k, limit in LIMITS["surfaces"].items() if surfaces[k] > limit]
    if total > LIMITS["total"]:
        failures.append("total")
    result = {"module_totals": modules, "groups": groups, "task_surfaces": surfaces,
              "total": total, "limits": LIMITS, "unassigned": unassigned, "failures": failures}
    if "--json" in sys.argv:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for name, value in groups.items():
            print(f"{name:10} {value:6}")
        print(f"total      {total:6}/{LIMITS['total']}")
        for name, value in surfaces.items():
            print(f"{name:12} {value:6}")
        if failures:
            print("FAIL:", ", ".join(failures))
    raise SystemExit(bool(failures))


if __name__ == "__main__":
    main()
