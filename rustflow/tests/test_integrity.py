"""integrity.scan: detection of gate-affecting changes (no cargo is run here)."""
from __future__ import annotations

import pytest

from rustflow import gates, integrity
from rustflow.integrity import scan

from conftest import git

CFG = dict(gates.DEFAULT_GATES)
CARGO = '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n'


def write(repo, rel, text):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def head(repo):
    return gates.tree_fingerprint(repo)


def kinds(changes):
    return sorted((c.kind, c.path) for c in changes)


def only(changes):
    assert len(changes) == 1, changes
    return changes[0]


def run(repo, cfg=CFG):
    return scan(repo, "HEAD", head(repo), cfg)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    write(r, "Cargo.toml", CARGO)
    write(r, "src/lib.rs", "pub fn a() {}\n\n#[test]\nfn t() {}\n")
    write(r, "tests/it.rs", "#[test]\nfn it() {}\n")
    write(r, ".gitignore", "target/\n")
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "Test")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    return r


def test_no_changes(repo):
    assert run(repo) == []


def test_dependency_added_is_not_a_change(repo):
    write(repo, "Cargo.toml", CARGO + '\n[dependencies]\nserde = "1"\n')
    assert kinds(run(repo)) == []


def test_lints_section_change(repo):
    write(repo, "Cargo.toml", CARGO + '\n[lints.clippy]\nall = "allow"\n')
    assert kinds(run(repo)) == [("cargo-lints", "Cargo.toml")]


def test_profile_change(repo):
    write(repo, "Cargo.toml", CARGO + "\n[profile.dev]\nopt-level = 1\n")
    assert kinds(run(repo)) == [("cargo-lints", "Cargo.toml")]


def test_workspace_member_lints(repo):
    write(repo, "crates/a/Cargo.toml", CARGO.replace("demo", "a") + "\n[lints]\nworkspace = true\n")
    write(repo, "crates/b/Cargo.toml", CARGO.replace("demo", "b"))
    got = kinds(run(repo))
    assert ("cargo-lints", "crates/a/Cargo.toml") in got
    assert all(p != "crates/b/Cargo.toml" for _, p in got)


def test_config_files(repo):
    for rel in (".cargo/config.toml", ".config/nextest.toml", "rustflow.toml", "sub/.cargo/config"):
        write(repo, rel, "x = 1\n")
    assert {p for k, p in kinds(run(repo)) if k == "config"} == {
        ".cargo/config.toml", ".config/nextest.toml", "rustflow.toml", "sub/.cargo/config"}


def test_config_rename_detected(repo):
    write(repo, "clippy.toml", "a = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "clippy")
    git(repo, "mv", "clippy.toml", ".clippy.toml")
    assert {p for _, p in kinds(run(repo))} == {"clippy.toml", ".clippy.toml"}


def test_deleted_config_digest(repo):
    write(repo, "deny.toml", "a = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "deny")
    (repo / "deny.toml").unlink()
    assert only(run(repo)).digest == "deleted"


def test_extra_paths(repo):
    write(repo, "ci/check.sh", "true\n")
    assert ("config", "ci/check.sh") in kinds(run(repo, {**CFG, "integrity_extra_paths": ["ci/*.sh"]}))
    assert kinds(run(repo)) == []


def test_runtime_paths_skipped(repo):
    write(repo, ".rustflow/runs/x/.cargo/config.toml", "a = 1\n")
    write(repo, "target/clippy.toml", "a = 1\n")
    assert run(repo) == []


def test_silence_added(repo):
    write(repo, "src/lib.rs", "pub fn a() {}\n\n#[test]\nfn t() {}\n"
          "#[allow(clippy::needless_return)]\nfn b() {}\n    #[ignore]\nfn c() {}\n"
          "#![expect(dead_code)]\n#[cfg_attr(test, allow(unused))]\nfn d() {}\n")
    c = only(run(repo))
    assert (c.kind, c.path, c.provable) == ("silence", "src/lib.rs", True)
    assert "needless_return" in c.detail and c.id == "G-silence-src/lib.rs"


def test_silence_removed_is_not_a_change(repo):
    write(repo, "src/lib.rs", "#[allow(dead_code)]\nfn x() {}\n#[test]\nfn t() {}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "allow")
    write(repo, "src/lib.rs", "fn x() {}\n#[test]\nfn t() {}\n")
    assert kinds(run(repo)) == []


def test_silence_ignores_comments(repo):
    write(repo, "src/lib.rs", "pub fn a() {}\n\n#[test]\nfn t() {}\n"
          "/// #[allow(dead_code)]\n// #[ignore]\n")
    assert kinds(run(repo)) == []


def test_tests_removed(repo):
    write(repo, "tests/it.rs", "fn it() {}\n")
    c = only(run(repo))
    assert (c.kind, c.path, c.provable) == ("tests-removed", "*", False) and "tests/it.rs" in c.detail


def test_commenting_out_a_test_counts_as_removed(repo):
    write(repo, "tests/it.rs", "// #[test]\nfn it() {}\n")
    assert [c.kind for c in run(repo)] == ["tests-removed"]


def test_removed_commented_test_is_not_a_change(repo):
    write(repo, "src/lib.rs", "pub fn a() {}\n// #[test]\nfn t() {}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c")
    write(repo, "src/lib.rs", "pub fn a() {}\nfn t() {}\n")
    assert kinds(run(repo)) == []


def test_test_moved_is_not_removed(repo):
    write(repo, "tests/it.rs", "fn it() {}\n")
    write(repo, "src/lib.rs", "pub fn a() {}\n\n#[test]\nfn t() {}\n#[test]\nfn it() {}\n")
    assert kinds(run(repo)) == []


def test_ids_and_digest_stable(repo):
    write(repo, ".cargo/config.toml", "a = 1\n")
    assert run(repo) == run(repo)
    assert run(repo)[0].id == "G-config-.cargo/config.toml"
    assert run(repo)[0].to_dict()["kind"] == "config"


def test_digest_changes_with_content(repo):
    write(repo, ".cargo/config.toml", "a = 1\n")
    one = only(run(repo))
    write(repo, ".cargo/config.toml", "a = 2\n")
    two = only(run(repo))
    assert one.id == two.id and one.digest != two.digest


def test_sorted_by_id(repo):
    write(repo, "rustflow.toml", "a = 1\n")
    write(repo, ".cargo/config", "a = 1\n")
    ids = [c.id for c in run(repo)]
    assert ids == sorted(ids) and len(ids) == 2


def test_broken_cargo_toml_is_unknown(repo):
    write(repo, "Cargo.toml", "[package")
    assert any(c.kind == "unknown" and not c.provable for c in run(repo))


def test_git_failure_is_unknown(repo):
    assert [c.kind for c in scan(repo, "nie-ma-takiej-rewizji", head(repo), CFG)] == ["unknown"]


def test_without_toml_parser_is_conservative(repo, monkeypatch):
    monkeypatch.setattr(integrity, "_toml", None)
    write(repo, "Cargo.toml", CARGO + '\n[dependencies]\nserde = "1"\n')
    assert kinds(run(repo)) == []
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "dep")
    write(repo, "Cargo.toml", CARGO + '\n[lints.clippy]\nall = "warn"\n\n[dependencies]\nserde = "1"\n')
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "lints")
    write(repo, "Cargo.toml", CARGO + '\n[lints.clippy]\nall = "allow"\n\n[dependencies]\nserde = "1"\n')
    assert kinds(run(repo)) == [("cargo-lints", "Cargo.toml")]


def test_init_copies_integrity(tmp_path):
    from rustflow.installer import Installer
    write(tmp_path, "Cargo.toml", CARGO)
    Installer(tmp_path, with_rust_skills=False, ci=False, out=lambda *_: None, python_cmd="python3").run()
    assert (tmp_path / ".rustflow" / "integrity.py").is_file()
