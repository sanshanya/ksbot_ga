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

# Marker and build artifacts kept out of `git status` via .git/info/exclude so the
# upstream checkout stays clean without modifying the upstream-tracked .gitignore.
_LOCAL_EXCLUDES = [MARKER, "__pycache__/", "*.pyc"]

# Files that live inside vendor/GenericAgent but are local operator state (not from
# upstream). They are git-ignored by upstream's own .gitignore, so preserving them
# across an archive→git conversion never dirties the checkout. Without this list,
# install_fresh's atomic swap would delete them along with the backup tree.
_LOCAL_STATE_FILES = [
    "mykey.py",
    "mykey.json",
    "memory/vision_api.py",
    "memory/global_mem.txt",
    "memory/global_mem_insight.txt",
]


def _collect_local_state(source: Path) -> dict[str, bytes]:
    """Read local-only files from an existing checkout so they survive a swap."""
    preserved: dict[str, bytes] = {}
    for rel in _LOCAL_STATE_FILES:
        path = source / rel
        if path.is_file():
            try:
                preserved[rel] = path.read_bytes()
            except OSError:
                pass
    return preserved


def _restore_local_state(dest: Path, preserved: dict[str, bytes]) -> None:
    """Write preserved local files into the new checkout."""
    for rel, data in preserved.items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _git(args: list[str], *, cwd: Path, proxy: str | None = None) -> str:
    full = ["git"]
    if proxy:
        full += ["-c", f"http.proxy={proxy}", "-c", f"https.proxy={proxy}"]
    result = subprocess.run(
        full + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result.stdout.strip()


def current_revision(target: Path) -> str | None:
    """Current pinned revision of the checkout.

    Prefers the live git HEAD when the checkout is a real repo, so the truth is
    always what is actually checked out. Falls back to the ``.ga_revision`` marker
    for directories that are not yet git checkouts (or git is unavailable).
    """
    if (target / ".git").is_dir():
        try:
            head = _git(["rev-parse", "HEAD"], cwd=target)
            if head:
                return head
        except RuntimeError:
            pass
    marker = target / MARKER
    if marker.is_file():
        value = marker.read_text(encoding="utf-8").strip()
        if value:
            return value
    return None


def verify_origin(target: Path) -> None:
    """Ensure the checkout points at the expected upstream.

    A stale ``.git`` from another project would otherwise let ``checkout`` resolve
    a commit from the wrong history. Adds the origin when missing; refuses to
    silently overwrite a mismatched one.
    """
    try:
        actual = _git(["remote", "get-url", "origin"], cwd=target)
    except RuntimeError:
        _git(["remote", "add", "origin", GIT_URL], cwd=target)
        return
    if actual and actual.rstrip("/") != GIT_URL.rstrip("/"):
        raise RuntimeError(
            f"Unexpected GenericAgent origin:\n  actual:   {actual}\n  expected: {GIT_URL}\n"
            "Refusing to update a checkout pointing at a different upstream."
        )


def ensure_clean(target: Path) -> None:
    """Refuse to update a checkout with local modifications.

    Never auto-resets or cleans: a dirty tree usually means someone is mid-debug
    and those edits must be preserved for explicit review.
    """
    status = _git(["status", "--porcelain"], cwd=target)
    if status:
        raise RuntimeError(
            "GenericAgent checkout contains local changes. Review with:\n"
            f"  git -C {target} diff\n"
            "To discard them, run explicitly:\n"
            f"  git -C {target} reset --hard\n"
            f"  git -C {target} clean -fd"
        )


def _ensure_excludes(target: Path) -> None:
    """Idempotently keep marker/build artifacts out of `git status`."""
    exclude = target / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    lines = [ln.strip() for ln in existing.splitlines() if ln.strip()]
    additions = [pat for pat in _LOCAL_EXCLUDES if pat not in lines]
    if additions:
        with exclude.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write("\n".join(additions) + "\n")


def _write_marker(target: Path, revision: str) -> None:
    (target / MARKER).write_text(revision + "\n", encoding="utf-8")


def _clean_pycache(target: Path) -> None:
    for cache in target.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def install_fresh(target: Path, revision: str, proxy: str | None) -> None:
    """First-time install: clone full history (blobless) into a sibling dir, then
    atomically swap it in so a failed download never breaks the current tree.
    """
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    installing = parent / ".GenericAgent.installing"
    backup = parent / ".GenericAgent.backup"
    if installing.exists():
        shutil.rmtree(installing)
    if backup.exists():
        shutil.rmtree(backup)

    # --filter=blob:none keeps the full commit graph (so log/diff/blame/bisect work
    # across upgrades) while delaying blob download until checkout needs them.
    # Deliberately NOT --depth 1: shallow history breaks `git log OLD..NEW`.
    env = os.environ.copy()
    if proxy:
        env["GIT_HTTP_PROXY"] = proxy
        env["GIT_HTTPS_PROXY"] = proxy
    try:
        subprocess.run(
            [
                "git", "clone",
                "--filter=blob:none", "--no-checkout",
                GIT_URL, str(installing),
            ],
            env=env,
            check=True,
        )
        _git(["checkout", "--detach", revision], cwd=installing, proxy=proxy)
        if not (installing / "agentmain.py").is_file():
            raise RuntimeError(f"{installing} is not a GenericAgent checkout (no agentmain.py)")
        _ensure_excludes(installing)
        _write_marker(installing, revision)
    except Exception:
        shutil.rmtree(installing, ignore_errors=True)
        raise

    # Preserve local operator state (mykey.py, memory files) before the swap so an
    # archive→git conversion does not silently delete configured credentials.
    preserved = _collect_local_state(target) if target.exists() else {}

    # Atomic swap: move current aside, install new, then drop the backup.
    if target.exists():
        target.rename(backup)
    try:
        installing.rename(target)
    except Exception:
        if backup.exists() and not target.exists():
            backup.rename(target)
        raise
    _restore_local_state(target, preserved)
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)


def update_existing(
    target: Path, revision: str, proxy: str | None, force: bool
) -> None:
    """Update an already-git checkout in place to the pinned revision."""
    verify_origin(target)
    ensure_clean(target)

    head = _git(["rev-parse", "HEAD"], cwd=target)
    if head == revision and not force:
        print(f"GenericAgent already pinned at {revision}")
        return

    # Fetch full history. If the revision is still unreachable (e.g. a brand-new
    # upstream commit not on the fetched refs), fetch it explicitly by sha.
    _git(["fetch", "origin"], cwd=target, proxy=proxy)
    try:
        _git(["cat-file", "-e", f"{revision}^{{commit}}"], cwd=target)
    except RuntimeError:
        _git(["fetch", "origin", revision], cwd=target, proxy=proxy)

    _git(["checkout", "--detach", revision], cwd=target, proxy=proxy)
    _clean_pycache(target)
    _ensure_excludes(target)
    _write_marker(target, revision)


def _resolve_proxy(cli_proxy: str | None) -> str | None:
    value = cli_proxy or os.environ.get("GA_FETCH_PROXY")
    return value.strip() if value and value.strip() else None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch the pinned GenericAgent checkout as a full git repo."
    )
    parser.add_argument(
        "--proxy",
        help="HTTP proxy, for example http://127.0.0.1:10090; GA_FETCH_PROXY also works",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-fetch and re-checkout even when HEAD already matches GA_REVISION",
    )
    args = parser.parse_args()

    proxy = _resolve_proxy(args.proxy)
    TARGET.parent.mkdir(parents=True, exist_ok=True)

    if (TARGET / ".git").is_dir():
        update_existing(TARGET, REVISION, proxy, args.force)
    else:
        current = current_revision(TARGET)
        if current == REVISION and not args.force:
            print(f"GenericAgent marker says {REVISION} but no .git present; use --force to rebuild as git checkout.")
            return
        install_fresh(TARGET, REVISION, proxy)
        print(f"GenericAgent installed at {REVISION} (full git checkout)")

    print(
        "\nNext steps:\n"
        f"  git -C {TARGET} log --oneline -10\n"
        f"  git -C {TARGET} diff OLD_REV NEW_REV   # after a future GA_REVISION bump\n"
        "  uv run python scripts/probe_ga_contract.py\n"
        "  uv run python -m pytest -q"
    )


if __name__ == "__main__":
    main()
