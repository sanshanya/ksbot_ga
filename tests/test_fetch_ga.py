from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def upstream(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q")
    for key, value in (("user.email", "t@t"), ("user.name", "test")):
        git(source, "config", key, value)

    def commit(name: str, files: dict[str, str]) -> str:
        for file, content in files.items():
            (source / file).write_text(content, encoding="utf-8")
        git(source, "add", ".")
        git(source, "commit", "-qm", name)
        return git(source, "rev-parse", "HEAD")

    old = commit("v1", {"agentmain.py": "# v1\n"})
    new = commit("v2", {"agentmain.py": "# v2\n", "ga.py": "# ga\n"})
    bare = tmp_path / "upstream.git"
    git(tmp_path, "clone", "-q", "--bare", str(source), str(bare))
    spec = importlib.util.spec_from_file_location(
        "fetch_ga", Path(__file__).parents[1] / "scripts/fetch_ga.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = tmp_path / "GenericAgent"
    monkeypatch.setattr(module, "GIT_URL", f"file://{bare.as_posix()}")
    monkeypatch.setattr(module, "TARGET", target)
    return module, target, old, new, bare


def test_revision_uses_head_then_marker(tmp_path: Path, upstream) -> None:
    module, target, _, new, _ = upstream
    module.install_fresh(target, new, None)
    (target / module.MARKER).write_text("stale\n", encoding="utf-8")
    assert module.current_revision(target) == new
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / module.MARKER).write_text("marker\n", encoding="utf-8")
    assert module.current_revision(plain) == "marker"


def test_update_is_clean_detached_and_keeps_history(upstream, capsys) -> None:
    module, target, old, new, _ = upstream
    module.install_fresh(target, old, None)
    module.update_existing(target, new, None, True)
    assert git(target, "rev-parse", "HEAD") == new
    assert git(target, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert not git(target, "status", "--porcelain")
    assert len(git(target, "log", "--oneline").splitlines()) == 2
    module.update_existing(target, new, None, False)
    assert "already pinned" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["local.txt", "ga.py"])
def test_update_rejects_dirty_checkout(upstream, name: str) -> None:
    module, target, _, new, _ = upstream
    module.install_fresh(target, new, None)
    (target / name).write_text("changed\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="local changes"):
        module.update_existing(target, new, None, True)


def test_update_rejects_wrong_origin(upstream) -> None:
    module, target, old, new, _ = upstream
    module.install_fresh(target, new, None)
    git(target, "remote", "set-url", "origin", "https://example.com/other.git")
    with pytest.raises(RuntimeError, match="Unexpected GenericAgent origin"):
        module.update_existing(target, old, None, True)


def test_fresh_install_is_atomic_and_preserves_local_state(upstream, monkeypatch) -> None:
    module, target, _, new, bare = upstream
    files = {
        "agentmain.py": "# archive\n",
        "mykey.py": "# credentials\n",
        "memory/global_mem.txt": "facts\n",
        "memory/vision_api.py": "# vision\n",
    }
    for name, content in files.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(module, "GIT_URL", str(bare.parent / "missing.git"))
    with pytest.raises(Exception):
        module.install_fresh(target, new, None)
    assert (target / "agentmain.py").read_text(encoding="utf-8") == "# archive\n"
    monkeypatch.setattr(module, "GIT_URL", f"file://{bare.as_posix()}")
    module.install_fresh(target, new, None)
    for name, content in list(files.items())[1:]:
        assert (target / name).read_text(encoding="utf-8") == content
