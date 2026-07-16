from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest


def _load_module():
    path = Path(__file__).parents[1] / "scripts" / "fetch_ga.py"
    spec = importlib.util.spec_from_file_location("fetch_ga", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(args: list[str], *, cwd: Path) -> str:
    result = subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture
def fake_upstream(tmp_path: Path):
    """A local bare repo that mimics lsdefine/GenericAgent with two commits."""
    # source working dir -> commits -> push to bare
    source = tmp_path / "source"
    source.mkdir()
    _git(["init", "-q"], cwd=source)
    _git(["config", "user.email", "t@t"], cwd=source)
    _git(["config", "user.name", "test"], cwd=source)
    (source / "agentmain.py").write_text("# ga v1\n", encoding="utf-8")
    _git(["add", "."], cwd=source)
    _git(["commit", "-q", "-m", "v1"], cwd=source)
    rev_a = _git(["rev-parse", "HEAD"], cwd=source)

    (source / "agentmain.py").write_text("# ga v2\n", encoding="utf-8")
    (source / "ga.py").write_text("# ga\n", encoding="utf-8")
    _git(["add", "."], cwd=source)
    _git(["commit", "-q", "-m", "v2"], cwd=source)
    rev_b = _git(["rev-parse", "HEAD"], cwd=source)

    bare = tmp_path / "upstream.git"
    _git(["clone", "-q", "--bare", str(source), str(bare)], cwd=tmp_path)
    return {"bare": bare, "rev_a": rev_a, "rev_b": rev_b}


@pytest.fixture
def module_with_fake_upstream(fake_upstream, monkeypatch):
    m = _load_module()
    # Use file:// protocol so git does not hardlink (forces real clone path,
    # making --filter=blob:none and --depth actually take effect).
    monkeypatch.setattr(m, "GIT_URL", f"file://{fake_upstream['bare'].as_posix()}")
    monkeypatch.setattr(m, "TARGET", fake_upstream["bare"].parent / "GenericAgent")
    # TARGET lives inside tmp_path; clear any pre-existing dir.
    if m.TARGET.exists():
        shutil.rmtree(m.TARGET)
    return m, fake_upstream


# --- current_revision --------------------------------------------------------


# TEST-CONTRACT: req=GA-REV-01 | rejects=current_revision ignores git HEAD and only uses marker | gap=no git-head priority | revert=remove git rev-parse branch in current_revision | mock=local bare repo (git boundary)=>real git operations
def test_current_revision_prefers_git_head(module_with_fake_upstream):
    m, up = module_with_fake_upstream
    m.install_fresh(m.TARGET, up["rev_b"], proxy=None)
    # Overwrite marker with a stale value to prove git HEAD wins.
    (m.TARGET / m.MARKER).write_text("stale_value\n", encoding="utf-8")
    assert m.current_revision(m.TARGET) == up["rev_b"]


# TEST-CONTRACT: req=GA-REV-02 | rejects=marker file is ignored when no .git exists | gap=no marker fallback | revert=remove marker read in current_revision | mock=none
def test_current_revision_falls_back_to_marker(tmp_path):
    m = _load_module()
    target = tmp_path / "GenericAgent"
    target.mkdir()
    (target / m.MARKER).write_text("deadbeef\n", encoding="utf-8")
    assert m.current_revision(target) == "deadbeef"


# TEST-CONTRACT: req=GA-REV-03 | rejects=current_revision returns stale value when no git and no marker | gap=no None case | revert=raise exception when no git and no marker instead of returning None | mock=none
def test_current_revision_none_when_no_git_no_marker(tmp_path):
    m = _load_module()
    target = tmp_path / "GenericAgent"
    target.mkdir()
    assert m.current_revision(target) is None


# --- install_fresh -----------------------------------------------------------


# TEST-CONTRACT: req=GA-INSTALL-01 | rejects=install_fresh does not checkout correct revision or is not detached HEAD | gap=no revision+detached check | revert=remove checkout --detach | mock=local bare repo (git boundary)=>real git operations
def test_install_fresh_creates_checkout_at_revision(module_with_fake_upstream):
    m, up = module_with_fake_upstream
    m.install_fresh(m.TARGET, up["rev_b"], proxy=None)
    assert (m.TARGET / ".git").is_dir()
    assert (m.TARGET / "agentmain.py").is_file()
    assert _git(["rev-parse", "HEAD"], cwd=m.TARGET) == up["rev_b"]
    # Detached HEAD: --abbrev-ref returns literal "HEAD" when detached.
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=m.TARGET) == "HEAD"


# TEST-CONTRACT: req=GA-INSTALL-02 | rejects=origin points to wrong upstream repo | gap=no origin check | revert=remove origin in clone | mock=local bare repo (git boundary)=>real git operations
def test_install_fresh_sets_correct_origin(module_with_fake_upstream):
    m, up = module_with_fake_upstream
    m.install_fresh(m.TARGET, up["rev_b"], proxy=None)
    origin = _git(["remote", "get-url", "origin"], cwd=m.TARGET)
    assert origin.endswith("upstream.git")


# TEST-CONTRACT: req=GA-INSTALL-03 | rejects=.ga_revision marker causes dirty git status | gap=no marker exclude test | revert=remove exclude entry for .ga_revision | mock=local bare repo (git boundary)=>real git operations
def test_install_fresh_marker_does_not_dirty_status(module_with_fake_upstream):
    m, up = module_with_fake_upstream
    m.install_fresh(m.TARGET, up["rev_b"], proxy=None)
    assert (m.TARGET / m.MARKER).is_file()
    assert _git(["status", "--porcelain"], cwd=m.TARGET) == ""


# TEST-CONTRACT: req=GA-INSTALL-05 | rejects=clone is shallow (only 1 commit) | gap=no history depth check | revert=replace --filter=blob:none with --depth 1 | mock=local bare repo (git boundary)=>real git operations
def test_install_fresh_keeps_full_history(module_with_fake_upstream):
    m, up = module_with_fake_upstream
    m.install_fresh(m.TARGET, up["rev_b"], proxy=None)
    log = _git(["log", "--oneline"], cwd=m.TARGET).splitlines()
    # Both v1 and v2 reachable => not a shallow single-commit clone.
    assert len(log) >= 2
    assert any("v2" in line for line in log)
    assert any("v1" in line for line in log)


# --- update_existing ---------------------------------------------------------


# TEST-CONTRACT: req=GA-UPDATE-01 | rejects=update_existing re-fetches when already at target revision | gap=no skip case | revert=remove HEAD==revision skip | mock=local bare repo (git boundary)=>real git operations
def test_update_existing_skips_when_already_at_revision(module_with_fake_upstream, capsys):
    m, up = module_with_fake_upstream
    m.install_fresh(m.TARGET, up["rev_b"], proxy=None)
    # second call, no force -> skip
    m.update_existing(m.TARGET, up["rev_b"], proxy=None, force=False)
    out = capsys.readouterr().out
    assert "already pinned" in out


# TEST-CONTRACT: req=GA-UPDATE-02 | rejects=update from A to B loses commit A (shallow regression) | gap=no upgrade history check | revert=replace fetch with shallow fetch | mock=local bare repo (git boundary)=>real git operations
def test_update_existing_to_new_revision_keeps_both_commits(module_with_fake_upstream):
    m, up = module_with_fake_upstream
    # Install at the older revision, then upgrade to the newer one.
    m.install_fresh(m.TARGET, up["rev_a"], proxy=None)
    m.update_existing(m.TARGET, up["rev_b"], proxy=None, force=True)
    assert _git(["rev-parse", "HEAD"], cwd=m.TARGET) == up["rev_b"]
    log = _git(["log", "--oneline"], cwd=m.TARGET).splitlines()
    # Both v1 and v2 reachable from HEAD => full history retained (not shallow).
    assert any("v2" in line for line in log)
    assert any("v1" in line for line in log)


# TEST-CONTRACT: req=GA-UPDATE-03 | rejects=update_existing discards local changes silently (untracked or tracked) | gap=no dirty refusal | revert=remove ensure_clean check | mock=local bare repo (git boundary)=>real git operations
@pytest.mark.parametrize("dirty_kind, filename, content", [
    ("untracked", "local_debug.txt", "debug\n"),
    ("tracked", "ga.py", "# tampered\n"),
])
def test_update_existing_refuses_dirty_and_preserves_local_changes(
    module_with_fake_upstream, dirty_kind, filename, content
):
    m, up = module_with_fake_upstream
    m.install_fresh(m.TARGET, up["rev_b"], proxy=None)
    (m.TARGET / filename).write_text(content, encoding="utf-8")
    with pytest.raises(RuntimeError, match="local changes"):
        m.update_existing(m.TARGET, up["rev_b"], proxy=None, force=True)
    # Local change must be preserved (no auto reset/clean).
    assert (m.TARGET / filename).read_text(encoding="utf-8") == content


# TEST-CONTRACT: req=GA-UPDATE-04 | rejects=update_existing overwrites origin silently | gap=no origin mismatch refusal | revert=remove verify_origin check | mock=local bare repo (git boundary)=>real git operations
def test_update_existing_rejects_mismatched_origin(module_with_fake_upstream, tmp_path, monkeypatch):
    m, up = module_with_fake_upstream
    m.install_fresh(m.TARGET, up["rev_b"], proxy=None)
    # Rewrite origin to point elsewhere.
    _git(["remote", "set-url", "origin", "https://example.com/other.git"], cwd=m.TARGET)
    with pytest.raises(RuntimeError, match="Unexpected GenericAgent origin"):
        m.update_existing(m.TARGET, up["rev_a"], proxy=None, force=True)


# --- atomicity ---------------------------------------------------------------


# TEST-CONTRACT: req=GA-ATOMIC-01 | rejects=install_fresh failure destroys existing tree | gap=no atomicity test | revert=let clone failure proceed without preserving old target | mock=local bare repo (git boundary)=>real git operations
def test_install_fresh_failure_preserves_existing_tree(module_with_fake_upstream, monkeypatch):
    m, up = module_with_fake_upstream
    existing = m.TARGET
    existing.mkdir(parents=True)
    (existing / "agentmain.py").write_text("# old archive\n", encoding="utf-8")
    (existing / "keepme.txt").write_text("keep\n", encoding="utf-8")

    # Sabotage clone by pointing GIT_URL at a non-existent path.
    monkeypatch.setattr(m, "GIT_URL", str(up["bare"].parent / "does-not-exist.git"))
    with pytest.raises(Exception):
        m.install_fresh(existing, up["rev_b"], proxy=None)
    # Old tree must still be intact and usable.
    assert (existing / "agentmain.py").read_text(encoding="utf-8") == "# old archive\n"
    assert (existing / "keepme.txt").is_file()
    # No half-installed dir left behind.
    assert not (existing.parent / ".GenericAgent.installing").exists()


# --- local state preservation -----------------------------------------------


# TEST-CONTRACT: req=GA-STATE-01 | rejects=mykey.py and memory files lost during archive→git checkout conversion | gap=no local state preservation | revert=remove _collect/_restore_local_state | mock=local bare repo (git boundary)=>real git operations
def test_install_fresh_preserves_local_state_files(module_with_fake_upstream):
    """mykey.py and memory files must survive an archive→git checkout conversion."""
    m, up = module_with_fake_upstream
    existing = m.TARGET
    existing.mkdir(parents=True)
    (existing / "agentmain.py").write_text("# old archive\n", encoding="utf-8")
    (existing / "mykey.py").write_text("# credentials\n", encoding="utf-8")
    (existing / "memory").mkdir()
    (existing / "memory" / "global_mem.txt").write_text("remembered facts\n", encoding="utf-8")
    (existing / "memory" / "vision_api.py").write_text("# vision config\n", encoding="utf-8")

    m.install_fresh(existing, up["rev_b"], proxy=None)

    # Local state files must be present in the new git checkout.
    assert (existing / "mykey.py").read_text(encoding="utf-8") == "# credentials\n"
    assert (existing / "memory" / "global_mem.txt").read_text(encoding="utf-8") == "remembered facts\n"
    assert (existing / "memory" / "vision_api.py").read_text(encoding="utf-8") == "# vision config\n"
    # The new checkout's upstream file is present (conversion succeeded).
    assert (existing / "agentmain.py").is_file()
    assert (existing / ".git").is_dir()

