from pathlib import Path
from types import SimpleNamespace

import pytest

from rustflow import installer, skills
from rustflow.config import Config


@pytest.fixture
def knowledge_clone(monkeypatch):
    real_run = installer.subprocess.run

    def run(argv, **kwargs):
        if argv[:2] != ["git", "clone"]:
            return real_run(argv, **kwargs)
        assert argv[4] == "https://github.com/rtmachines/rust-skills"
        root = Path(argv[-1])
        (root / "rules").mkdir(parents=True)
        (root / "SKILL.md").write_text("---\nname: rust-skills\n---\nRead rules/ownership.md\n" + "x" * 39409)
        (root / "rules/ownership.md").write_text("Borrow before cloning")
        (root / "LICENSE").write_text("MIT")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(installer.subprocess, "run", run)


def test_default_knowledge_installed_and_preloaded(tmp_path, knowledge_clone):
    installer.Installer(tmp_path, ci=False, out=lambda *_: None).run()
    section = Config.load(tmp_path).data["skills"]
    for provider, base in skills.SKILL_BASE.items():
        root = tmp_path / base / "rust-skills"
        assert (root / "rules/ownership.md").read_text() == "Borrow before cloning"
        assert (root / "LICENSE").read_text() == "MIT"
        for role in ("planner", "implementer", "fixer", "reviewer_a", "auditor"):
            assert skills.resolve(section, role, tmp_path, provider) == [root / "SKILL.md"]
    assert "  - rust-skills\n" in (tmp_path / ".claude/agents/rustflow-reviewer.md").read_text()


def test_knowledge_preserved_unless_forced(tmp_path, knowledge_clone):
    inst = installer.Installer(tmp_path, ci=False, out=lambda *_: None)
    inst.run()
    path = tmp_path / ".agents/skills/rust-skills/rules/ownership.md"
    path.write_text("custom")
    inst.run()
    assert path.read_text() == "custom"
    installer.Installer(tmp_path, force=True, ci=False, out=lambda *_: None).run()
    assert path.read_text() == "Borrow before cloning"


def test_cli_offline_init(tmp_path, monkeypatch):
    from rustflow.cli import main
    real_run = installer.subprocess.run

    def no_clone(argv, **kwargs):
        assert argv[:2] != ["git", "clone"]
        return real_run(argv, **kwargs)

    monkeypatch.setattr(installer.subprocess, "run", no_clone)
    assert main(["init", "--repo", str(tmp_path), "--no-rust-skills", "--no-ci"]) == 0
    assert (tmp_path / ".agents/skills/rustflow-plan/SKILL.md").is_file()
    assert not (tmp_path / ".agents/skills/rust-skills").exists()


def test_cli_clone_failure_is_reported(tmp_path, monkeypatch, capsys):
    from rustflow.cli import main
    real_run = installer.subprocess.run

    def failed_clone(argv, **kwargs):
        if argv[:2] == ["git", "clone"]:
            return SimpleNamespace(returncode=1, stderr="network unavailable")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(installer.subprocess, "run", failed_clone)
    assert main(["init", "--repo", str(tmp_path), "--no-ci"]) == 2
    assert "network unavailable" in capsys.readouterr().err
    assert not (tmp_path / ".agents/skills/rust-skills").exists()
