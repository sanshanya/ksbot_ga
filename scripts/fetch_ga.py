from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "vendor" / "GenericAgent"
REVISION = (ROOT / "GA_REVISION").read_text(encoding="utf-8").strip()
GIT_URL = "https://github.com/lsdefine/GenericAgent.git"
MARKER = ".ga_revision"
LOCAL_EXCLUDES = (MARKER, "__pycache__/", "*.pyc")
LOCAL_STATE = (
    "mykey.py",
    "mykey.json",
    "memory/vision_api.py",
    "memory/global_mem.txt",
    "memory/global_mem_insight.txt",
)


def _git(args: list[str], *, cwd: Path, proxy: str | None = None) -> str:
    command = ["git"]
    if proxy:
        command += ["-c", f"http.proxy={proxy}", "-c", f"https.proxy={proxy}"]
    result = subprocess.run(
        command + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"git {' '.join(args)} failed ({result.returncode}): {detail}")
    return result.stdout.strip()


def current_revision(target: Path) -> str | None:
    if (target / ".git").is_dir():
        try:
            if head := _git(["rev-parse", "HEAD"], cwd=target):
                return head
        except RuntimeError:
            pass
    marker = target / MARKER
    return marker.read_text(encoding="utf-8").strip() or None if marker.is_file() else None


def verify_origin(target: Path) -> None:
    try:
        actual = _git(["remote", "get-url", "origin"], cwd=target)
    except RuntimeError:
        _git(["remote", "add", "origin", GIT_URL], cwd=target)
        return
    if actual.rstrip("/") != GIT_URL.rstrip("/"):
        raise RuntimeError(f"Unexpected GenericAgent origin: {actual}; expected {GIT_URL}")


def ensure_clean(target: Path) -> None:
    if status := _git(["status", "--porcelain"], cwd=target):
        raise RuntimeError(
            "GenericAgent checkout contains local changes. Review or explicitly reset them:\n"
            f"  git -C {target} diff\n  git -C {target} reset --hard\n"
            f"  git -C {target} clean -fd\n{status}"
        )


def _ensure_excludes(target: Path) -> None:
    exclude = target / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    known = {line.strip() for line in existing.splitlines()}
    additions = [item for item in LOCAL_EXCLUDES if item not in known]
    if additions:
        with exclude.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write("\n".join(additions) + "\n")


def _write_marker(target: Path, revision: str) -> None:
    (target / MARKER).write_text(revision + "\n", encoding="utf-8")


def _local_state(target: Path) -> dict[str, bytes]:
    result = {}
    for name in LOCAL_STATE:
        try:
            result[name] = (target / name).read_bytes()
        except OSError:
            pass
    return result


def _restore_state(target: Path, state: dict[str, bytes]) -> None:
    for name, data in state.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def install_fresh(target: Path, revision: str, proxy: str | None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    installing = target.parent / ".GenericAgent.installing"
    backup = target.parent / ".GenericAgent.backup"
    shutil.rmtree(installing, ignore_errors=True)
    shutil.rmtree(backup, ignore_errors=True)
    env = os.environ.copy()
    if proxy:
        env.update(GIT_HTTP_PROXY=proxy, GIT_HTTPS_PROXY=proxy)
    try:
        subprocess.run(
            ["git", "clone", "--filter=blob:none", "--no-checkout", GIT_URL, str(installing)],
            env=env,
            check=True,
        )
        _git(["checkout", "--detach", revision], cwd=installing, proxy=proxy)
        if not (installing / "agentmain.py").is_file():
            raise RuntimeError(f"{installing} is not a GenericAgent checkout")
        _ensure_excludes(installing)
        _write_marker(installing, revision)
    except Exception:
        shutil.rmtree(installing, ignore_errors=True)
        raise

    state = _local_state(target) if target.exists() else {}
    if target.exists():
        target.rename(backup)
    try:
        installing.rename(target)
    except Exception:
        if backup.exists() and not target.exists():
            backup.rename(target)
        raise
    _restore_state(target, state)
    shutil.rmtree(backup, ignore_errors=True)


def update_existing(target: Path, revision: str, proxy: str | None, force: bool) -> None:
    verify_origin(target)
    ensure_clean(target)
    if _git(["rev-parse", "HEAD"], cwd=target) == revision and not force:
        print(f"GenericAgent already pinned at {revision}")
        return
    _git(["fetch", "origin"], cwd=target, proxy=proxy)
    try:
        _git(["cat-file", "-e", f"{revision}^{{commit}}"], cwd=target)
    except RuntimeError:
        _git(["fetch", "origin", revision], cwd=target, proxy=proxy)
    _git(["checkout", "--detach", revision], cwd=target, proxy=proxy)
    for cache in target.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    _ensure_excludes(target)
    _write_marker(target, revision)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch the pinned GenericAgent git checkout.")
    parser.add_argument("--proxy", help="HTTP proxy; GA_FETCH_PROXY is also supported")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    proxy = (args.proxy or os.getenv("GA_FETCH_PROXY") or "").strip() or None
    if (TARGET / ".git").is_dir():
        update_existing(TARGET, REVISION, proxy, args.force)
    elif current_revision(TARGET) == REVISION and not args.force:
        print(f"GenericAgent marker says {REVISION} but no .git exists; use --force to rebuild.")
        return
    else:
        install_fresh(TARGET, REVISION, proxy)
        print(f"GenericAgent installed at {REVISION}")
    print("Run scripts/probe_ga_contract.py and pytest before use.")


if __name__ == "__main__":
    main()
