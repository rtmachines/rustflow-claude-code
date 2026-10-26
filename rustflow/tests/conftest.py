import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_agents" / "fake_agent.py"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture(scope="session")
def shared_target(tmp_path_factory):
    return tmp_path_factory.mktemp("cargo-target")


@pytest.fixture
def fake_bin(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    for name in ("claude", "codex"):
        shutil.copy(FAKE, d / f"{name}.py")
    return d


@pytest.fixture
def fake_defaults(fake_bin):
    """Default configuration with only the provider executables replaced by local fakes."""
    from rustflow.config import load_defaults
    data = load_defaults()
    for provider in ("claude", "codex"):
        data["providers"][provider]["bin"] = str(fake_bin / f"{provider}.py")
    return data


@pytest.fixture
def rust_repo(tmp_path, shared_target, monkeypatch, fake_bin):
    if shutil.which("cargo") is None:
        pytest.skip("cargo not available")
    repo = tmp_path / "demo"
    subprocess.run(["cargo", "new", "--lib", "--vcs", "none", "-q", str(repo)], check=True)
    (repo / "src" / "lib.rs").write_text("//! demo crate\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text("target/\n", encoding="utf-8")
    from rustflow.installer import Installer
    Installer(repo, ci=False, out=lambda *_: None, python_cmd=sys.executable).run()
    cfg = (repo / "rustflow.toml").read_text(encoding="utf-8")
    # TOML literal strings + forward slashes: valid on Windows and Linux
    cfg = cfg.replace('bin = "claude"', f"bin = '{(fake_bin / 'claude.py').as_posix()}'")
    cfg = cfg.replace('bin = "codex"', f"bin = '{(fake_bin / 'codex.py').as_posix()}'")
    (repo / "rustflow.toml").write_text(cfg, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    monkeypatch.setenv("CARGO_TARGET_DIR", str(shared_target))
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "fake.log"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("RUSTFLOW_ROLE", raising=False)
    return repo


def read_log(repo: Path):
    import json
    path = repo.parent / "fake.log"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
