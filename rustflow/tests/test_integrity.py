"""integrity.scan: detection of gate-affecting changes (no cargo is run here)."""
from __future__ import annotations

import os
import shutil

import pytest

from rustflow import gates, integrity
from rustflow.integrity import ProofResult, prove, scan, strip_attributes

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


def test_quoted_path_is_still_scanned(repo):
    write(repo, 'we"ird.rs', "#[test]\nfn q() {}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "q")
    write(repo, 'we"ird.rs', "#[allow(dead_code)]\nfn q() {}\n")
    got = kinds(run(repo))
    assert ("silence", 'we"ird.rs') in got and ("tests-removed", "*") in got


# ── strip_attributes / prove ─────────────────────────────────────────────────
def test_strip_attributes_inline_and_standalone():
    src = "#[allow(clippy::needless_return)]\nfn a() {}\n#[allow(x)] fn b() {}\nfn c() { let s = \"#[allow(y)]\"; }\n"
    out = strip_attributes(src, [1, 3])
    assert out == "fn a() {}\nfn b() {}\nfn c() { let s = \"#[allow(y)]\"; }\n"


def test_strip_attributes_multiline_and_brackets():
    src = "#![allow(\n    dead_code, // ] in comment-free\n)]\nfn a() -> [u8; 1] { [0] }\n#[allow(x)] #[derive(Debug)] struct S;\n"
    out = strip_attributes(src, [1, 5])
    assert out == "fn a() -> [u8; 1] { [0] }\n#[derive(Debug)] struct S;\n"


def test_strip_attributes_only_added_positions():
    src = "#[test]\n#[ignore]\nfn a(){}\n#[test]\n#[ignore]\nfn b(){}\n"
    assert strip_attributes(src, [5]) == "#[test]\n#[ignore]\nfn a(){}\n#[test]\nfn b(){}\n"


@pytest.fixture(autouse=True)
def xdg(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("xdg")))


LIB_OK = "pub fn a() -> i32 {\n    1\n}\n"


def proof(repo, profile="stop", cfg=None, **kw):
    cfg = cfg or gates.load_gate_config(repo)
    h = head(repo)
    return prove(repo, "HEAD", h, scan(repo, "HEAD", h, cfg), profile, cfg, **kw)


def commit(repo, msg="base"):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)


def wt_dirs(repo):
    return list(integrity._wt_path(repo.resolve(), "x").parent.glob("proof-*"))


def test_prove_nothing_provable_passes(rust_repo):
    write(rust_repo, "src/lib.rs", "pub fn a() {}\n")
    assert proof(rust_repo) == ProofResult("passed", "nothing to prove")


def test_prove_unneeded_allow_passes(rust_repo):
    write(rust_repo, "src/lib.rs", LIB_OK)
    commit(rust_repo)
    write(rust_repo, "src/lib.rs", "#[allow(dead_code)]\n" + LIB_OK)
    r = proof(rust_repo)
    assert r.status == "passed", r


def test_prove_masking_allow_fails(rust_repo):
    write(rust_repo, "src/lib.rs", LIB_OK)
    commit(rust_repo)
    write(rust_repo, "src/lib.rs", "#[allow(clippy::needless_return)]\npub fn a() -> i32 {\n    return 1;\n}\n")
    r = proof(rust_repo)
    assert r.status == "failed" and not r.error, r


def test_prove_cap_lints_fails(rust_repo):
    write(rust_repo, "src/lib.rs", LIB_OK)
    commit(rust_repo)
    write(rust_repo, "src/lib.rs", "pub fn a() -> i32 {\n    return 1;\n}\n")
    write(rust_repo, ".cargo/config.toml", '[build]\nrustflags = ["--cap-lints", "allow"]\n')
    r = proof(rust_repo)
    assert r.status == "failed" and not r.error, r


def test_prove_tests_off_fails(rust_repo):
    write(rust_repo, "src/lib.rs", LIB_OK + "\n#[test]\nfn t() {\n    assert_eq!(a(), 2);\n}\n")
    commit(rust_repo)
    cfg = gates.load_gate_config(rust_repo)
    toml = (rust_repo / "rustflow.toml").read_text(encoding="utf-8")
    assert 'tests = "auto"' in toml
    write(rust_repo, "rustflow.toml", toml.replace('tests = "auto"', 'tests = "off"'))
    r = proof(rust_repo, cfg=cfg)
    assert r.status == "failed" and not r.error, r


def test_prove_ignore_on_failing_test_fails(rust_repo):
    write(rust_repo, "src/lib.rs", LIB_OK + "\n#[test]\nfn t() {\n    assert_eq!(a(), 2);\n}\n")
    commit(rust_repo)
    write(rust_repo, "src/lib.rs", LIB_OK + "\n#[test]\n#[ignore]\nfn t() {\n    assert_eq!(a(), 2);\n}\n")
    r = proof(rust_repo)
    assert r.status == "failed" and not r.error, r


def test_prove_neutral_clippy_toml_passes(rust_repo):
    write(rust_repo, "src/lib.rs", LIB_OK)
    commit(rust_repo)
    write(rust_repo, "clippy.toml", "too-many-arguments-threshold = 7\n")
    r = proof(rust_repo)
    assert r.status == "passed", r


def test_prove_worktree_error_fails(rust_repo, monkeypatch):
    write(rust_repo, "src/lib.rs", "#[allow(dead_code)]\n" + LIB_OK)
    real = integrity.gates._git

    def fake(root, *args, **kw):
        if args[:2] == ("worktree", "add"):
            import subprocess
            return subprocess.CompletedProcess(args, 1, "", "boom")
        return real(root, *args, **kw)

    monkeypatch.setattr(integrity.gates, "_git", fake)
    r = proof(rust_repo)
    assert r.status == "failed" and r.error


def test_prove_recovers_stale_worktree(rust_repo):
    write(rust_repo, "src/lib.rs", "#[allow(dead_code)]\n" + LIB_OK)
    stale = integrity._wt_path(rust_repo.resolve(), head(rust_repo))
    for d in reversed(stale.parents[:3]):
        d.mkdir(mode=0o700, exist_ok=True)
    stale.mkdir(mode=0o700)
    (stale / "junk.txt").write_text("junk", encoding="utf-8")
    assert proof(rust_repo).status == "passed" and not wt_dirs(rust_repo)


def test_prove_respects_cargo_dir(tmp_path, shared_target, monkeypatch):
    monkeypatch.setenv("CARGO_TARGET_DIR", str(shared_target))
    r = tmp_path / "repo"
    write(r, "crate/Cargo.toml", '[package]\nname = "cd"\nversion = "0.1.0"\nedition = "2021"\n')
    write(r, "crate/src/lib.rs", LIB_OK)
    write(r, ".gitignore", "target/\n")
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "Test")
    commit(r)
    write(r, "crate/src/lib.rs", "#[allow(clippy::needless_return)]\npub fn a() -> i32 {\n    return 1;\n}\n")
    cfg = dict(gates.DEFAULT_GATES, cargo_dir="crate")
    res = proof(r, cfg=cfg)
    assert res.status == "failed" and not res.error and "clippy=FAIL" in res.summary, res
    write(r, "crate/src/lib.rs", "#[allow(dead_code)]\n" + LIB_OK)  # counterpart: unneeded allow passes
    res = proof(r, cfg=cfg)
    assert res.status == "passed" and "clippy=PASS" in res.summary, res


def test_prove_leaves_tree_untouched(rust_repo):
    write(rust_repo, "src/lib.rs", "#[allow(dead_code)]\n" + LIB_OK)
    before = gates.tree_fingerprint(rust_repo)
    proof(rust_repo)
    assert gates.tree_fingerprint(rust_repo) == before and not wt_dirs(rust_repo)


def test_prove_only_added_ignore_is_stripped(rust_repo):
    bad = "#[test]\n#[ignore]\nfn bad_t() {\n    assert_eq!(a(), 2);\n}\n"
    write(rust_repo, "src/lib.rs", LIB_OK + "\n" + bad)
    commit(rust_repo)
    good = "#[test]\n#[ignore]\nfn good_t() {}\n"
    write(rust_repo, "src/lib.rs", LIB_OK + "\n" + bad + "\n" + good)
    # the pre-existing #[ignore] on the failing test must stay; only the added one (on a passing test) is stripped
    r = proof(rust_repo)
    assert r.status == "passed", r


@pytest.mark.parametrize("src", [
    '/* " */ #[allow(clippy::needless_return)] pub fn a() -> i32 {\n    return 1;\n}\n',
    'pub const P: &str = r"C:\\"; #[allow(clippy::needless_return)] pub fn a() -> i32 {\n    return 1;\n}\n',
])
def test_prove_attribute_after_tricky_literal_is_stripped(rust_repo, src):
    # round 1-3 failed these closed ("could not strip"); the Rust lexer now strips them, so clippy sees the lint
    write(rust_repo, "src/lib.rs", LIB_OK)
    commit(rust_repo)
    write(rust_repo, "src/lib.rs", src)
    r = proof(rust_repo)
    assert r.status == "failed" and not r.error and "clippy=FAIL" in r.summary, r


def test_prove_unlocatable_silence_fails(rust_repo):
    write(rust_repo, "src/lib.rs", LIB_OK)
    commit(rust_repo)
    ghost = integrity.GateChange("G-silence-src/ghost.rs", "src/ghost.rs", "silence", "x", "d", True)
    h = head(rust_repo)
    r = prove(rust_repo, "HEAD", h, [ghost], "stop", gates.load_gate_config(rust_repo))
    assert r.status == "failed" and "cannot locate" in r.error, r


def test_prove_ancestor_cargo_config_fails(rust_repo, monkeypatch, tmp_path):
    write(rust_repo, "src/lib.rs", "#[allow(dead_code)]\n" + LIB_OK)
    cache = tmp_path / "planted"
    cache.mkdir()
    write(cache, ".cargo/config.toml", '[build]\nrustflags = ["--cap-lints", "allow"]\n')
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    r = proof(rust_repo)
    assert r.status == "failed" and ".cargo" in r.error, r


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership/mode checks")
def test_prove_wt_dir_mode_and_symlink_fail(rust_repo, tmp_path):
    write(rust_repo, "src/lib.rs", "#[allow(dead_code)]\n" + LIB_OK)
    wt = integrity._wt_path(rust_repo.resolve(), head(rust_repo))
    assert proof(rust_repo).status == "passed"
    scope = wt.parent
    scope.chmod(0o777)
    r = proof(rust_repo)
    assert r.status == "failed" and "accessible" in r.error, r
    shutil.rmtree(scope)
    other = tmp_path / "elsewhere"
    other.mkdir()
    scope.symlink_to(other)
    r = proof(rust_repo)
    assert r.status == "failed" and "symlink" in r.error, r


def test_prove_form_feed_does_not_shift_stripping(rust_repo):
    base = "//! demo\x0c crate\n#[allow(dead_code)]\npub fn a() -> i32 {\n    1\n}\n"
    write(rust_repo, "src/lib.rs", base)
    commit(rust_repo)
    head_src = ("//! demo\x0c crate\n#[allow(dead_code)]\n#[allow(clippy::needless_return)]\n"
                "pub fn a() -> i32 {\n    return 1;\n}\n")
    write(rust_repo, "src/lib.rs", head_src)
    r = proof(rust_repo)
    assert r.status == "failed" and not r.error and "clippy=FAIL" in r.summary, r


def test_scan_form_feed_and_cr_in_content(repo):
    write(repo, "src/lib.rs", "pub fn a() {}\x0c\n\n#[test]\nfn t() {}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "ff")
    write(repo, "src/lib.rs", "pub fn a() {}\x0c\n\x0b\n#[allow(dead_code)]\n#[test]\nfn t() {}\n")
    assert kinds(run(repo)) == [("silence", "src/lib.rs")]
    positions: dict = {}
    d = gates._git(repo, "diff", "-U0", "--no-color", "HEAD", head(repo), "--", "*.rs").stdout
    integrity._parse_rs_diff(d, positions)
    assert positions == {"src/lib.rs": [3]}


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_prove_symlinked_cache_with_real_ancestor_cargo_config_fails(rust_repo, monkeypatch, tmp_path):
    write(rust_repo, "src/lib.rs", "#[allow(dead_code)]\n" + LIB_OK)
    real = tmp_path / "real"
    write(real, ".cargo/config.toml", '[build]\nrustflags = ["--cap-lints", "allow"]\n')
    (real / "cache").mkdir()
    link = tmp_path / "link"
    link.symlink_to(real / "cache")
    monkeypatch.setenv("XDG_CACHE_HOME", str(link))
    r = proof(rust_repo)
    assert r.status == "failed" and "ancestor" in r.error, r


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode checks")
def test_prove_rustflow_cache_dir_is_checked(rust_repo):
    write(rust_repo, "src/lib.rs", "#[allow(dead_code)]\n" + LIB_OK)
    assert proof(rust_repo).status == "passed"
    integrity._wt_path(rust_repo.resolve(), "x").parents[2].chmod(0o777)
    r = proof(rust_repo)
    assert r.status == "failed" and "accessible" in r.error, r


def test_prove_raw_string_overstrip_fails(rust_repo):
    write(rust_repo, "src/lib.rs", LIB_OK)
    commit(rust_repo)
    write(rust_repo, "src/lib.rs",
          '#[allow(clippy::needless_return, reason = r"\\")]\npub fn a() -> i32 {\n    return 1;\n}\n// "]\npub fn b() {}\n')
    r = proof(rust_repo)
    assert r.status == "failed" and not r.error and "clippy=FAIL" in r.summary, r


def test_prove_multiline_added_attribute_is_stripped(rust_repo):
    write(rust_repo, "src/lib.rs", LIB_OK)
    commit(rust_repo)
    write(rust_repo, "src/lib.rs", "#[allow(\n    clippy::needless_return,\n)]\npub fn a() -> i32 {\n    return 1;\n}\n")
    r = proof(rust_repo)
    assert r.status == "failed" and not r.error and "clippy=FAIL" in r.summary, r
    write(rust_repo, "src/lib.rs", "#[allow(\n    dead_code,\n)]\n" + LIB_OK)
    r = proof(rust_repo)
    assert r.status == "passed", r


# ── fix round 4: Rust lexing of attribute spans ──────────────────────────────
@pytest.mark.parametrize("src, start, end", [
    ('r"\\"; x', 0, 4),                       # raw string: no escapes
    ('r#"x"]"#)]', 0, 8),                     # raw string with hashes, `"` and `]` inside
    ('br##"a"#b"##;', 0, 12),                 # raw byte string, fewer hashes inside
    ('cr#"a"#;', 0, 7),                       # raw C string
    ('br"\\";', 0, 5),                        # raw byte string: no escapes
    ('b"\\"";', 0, 5),                        # byte string with an escaped quote
    ("b'\\'';", 0, 5),                        # byte char with an escaped quote
    ("'a';", 0, 3),
    ("'\\n';", 0, 4),
    ("'\\u{1F600}';", 0, 11),
    ("'\"';", 0, 3),                          # char literal holding a quote
    ("'a, 'b>", 0, 0),                        # lifetime, not a literal
    ("'static str", 0, 0),
    ("/* a /* b */ \" */x", 0, 17),           # nested block comment
    ("/**/x", 0, 4),
    ("// c \"\nx", 0, 6),
    ('xr"a"', 1, 1),                          # `r` inside an identifier is no prefix
    ('r#"abc', 0, 6),                         # unterminated: runs to the end
    ("/* /* */", 0, 8),                       # unterminated nested comment
    ("r#raw", 0, 0),                          # raw identifier
])
def test_skip_literal_lexes_rust(src, start, end):
    assert integrity._skip_literal(src, start) == end


@pytest.mark.parametrize("attr", [
    '#[allow(x, reason = r"\\")]',
    '#[allow(x, reason = br"\\")]',
    '#[allow(x, reason = r#"x"]"#)]',
    '#[allow(x /* " ] */)]',
    '#[allow(x /* /* */ " ] */)]',
    "#[allow(x, reason = ']')]",
])
def test_strip_attributes_tricky_literals(attr):
    src = "pub fn a() {}\n" + attr + '\npub fn c() -> i32 {\n    return 1;\n}\n// "]\n'
    assert strip_attributes(src, [2]) == 'pub fn a() {}\npub fn c() -> i32 {\n    return 1;\n}\n// "]\n'


def test_strip_attributes_lifetimes_next_to_attribute():
    src = "pub struct W<'a>(#[allow(dead_code)] &'a u8, char);\nconst Q: char = ']';\n"
    assert strip_attributes(src, [1]) == "pub struct W<'a>(&'a u8, char);\nconst Q: char = ']';\n"


def test_strip_attributes_continues_literals_across_lines():
    # line 2 lies inside a raw string opened on line 1: nothing there is an attribute
    src = 'const S: &str = r#"\n#[allow(x)] "]\n"#;\n#[allow(y)]\nfn f() {}\n'
    out, left = integrity._strip(src, [2, 4])
    assert out == 'const S: &str = r#"\n#[allow(x)] "]\n"#;\nfn f() {}\n' and left == [2]


def test_strip_rejects_span_with_code_tokens():
    for attr in ("#[allow(dead_code, reason = {})]", "#[allow(dead_code); ]", "#[allow(dead_code)} ]"):
        with pytest.raises(RuntimeError, match="swallow"):
            strip_attributes(attr + "\npub fn a() {}\n", [1])


def test_strip_reports_cut_outside_allowed_lines():
    src = "#[allow(\n    dead_code,\n)]\npub fn a() {}\n"
    assert integrity._strip(src, [1], {1})[1] == [2, 3]


def _naive_skip_literal(text, i):  # the pre-round-4 lexer: no raw strings, no block comments
    c = text[i]
    if c == '"':
        j = i + 1
        while j < len(text) and text[j] != '"':
            j += 2 if text[j] == "\\" else 1
        return j + 1
    if text.startswith("//", i):
        j = text.find("\n", i)
        return len(text) if j < 0 else j
    return i


EXPLOIT_TAIL = '\npub fn c() -> i32 {\n    return 1;\n}\n// "]\n'


def test_prove_span_check_catches_lexer_miss(rust_repo, monkeypatch):
    write(rust_repo, "src/lib.rs", LIB_OK)
    commit(rust_repo)
    write(rust_repo, "src/lib.rs", LIB_OK + '#[allow(clippy::needless_return, reason = r"\\")]' + EXPLOIT_TAIL)
    monkeypatch.setattr(integrity, "_skip_literal", _naive_skip_literal)
    r = proof(rust_repo)
    assert r.status == "failed" and "swallow" in r.error, r


@pytest.mark.parametrize("attr", [
    '#[allow(clippy::needless_return, reason = r"\\")]',
    '#[allow(clippy::needless_return, reason = br"\\")]',
    '#[allow(clippy::needless_return, reason = r#"x"]"#)]',
    '#[allow(clippy::needless_return /* " ] */)]',
    '#[allow(clippy::needless_return /* /* */ " ] */)]',
])
def test_prove_all_added_tricky_attribute_fails(rust_repo, attr):
    write(rust_repo, "src/lib.rs", LIB_OK)
    commit(rust_repo)
    write(rust_repo, "src/lib.rs", LIB_OK + attr + EXPLOIT_TAIL)
    r = proof(rust_repo)
    assert r.status == "failed" and not r.error and "clippy=FAIL" in r.summary, r


def test_prove_lifetime_next_to_attribute(rust_repo):
    write(rust_repo, "src/lib.rs", LIB_OK)
    commit(rust_repo)
    masked = "#[allow(clippy::needless_lifetimes)]\npub fn f<'a>(x: &'a u8) -> &'a u8 {\n    x\n}\n"
    write(rust_repo, "src/lib.rs", LIB_OK + masked)
    r = proof(rust_repo)
    assert r.status == "failed" and not r.error and "clippy=FAIL" in r.summary, r
    write(rust_repo, "src/lib.rs", LIB_OK + "pub struct W<'a>(#[allow(dead_code)] pub &'a u8);\n")
    r = proof(rust_repo)
    assert r.status == "passed", r


# ── state functions ──────────────────────────────────────────────────────────
def _gc(path="clippy.toml", kind="config", digest="d1", provable=True):
    return integrity.GateChange(f"G-{kind}-{path}", path, kind, "detail", digest, provable)


def test_reconcile_carries_by_digest():
    first = integrity.reconcile([], [_gc()])
    e = first[0]
    e.update(proof="passed", proof_tree="t", votes={"reviewer_a": {"digest": "d1", "decision": "accept", "reason": "r"}},
             justification="why", fix_rounds=2, status="accepted", accepted_by="proof")
    same = integrity.reconcile(first, [_gc()])[0]
    assert (same["proof"], same["justification"], same["fix_rounds"], same["status"]) == ("passed", "why", 2, "accepted")
    assert same["votes"] == e["votes"]
    other = integrity.reconcile(first, [_gc(digest="d2")])[0]
    assert (other["proof"], other["votes"], other["justification"], other["status"]) == ("", {}, "", "pending")
    assert other["fix_rounds"] == 2
    assert integrity.reconcile(first, []) == []


def test_decide_rules():
    ents = integrity.reconcile([], [_gc("a.toml"), _gc("b.toml"), _gc("c.toml"), _gc("u", "unknown", provable=False),
                                    _gc("d", "tests-removed", provable=False)])
    by = {e["path"]: e for e in ents}
    integrity.apply_proof([by["a.toml"]], ProofResult("passed", "ok"))
    integrity.apply_proof([by["b.toml"]], ProofResult("failed", "no"))
    integrity.decide(ents)
    assert (by["a.toml"]["status"], by["a.toml"]["accepted_by"]) == ("accepted", "proof")
    assert by["b.toml"]["status"] == "blocked" and by["c.toml"]["status"] == "pending"
    assert by["u"]["status"] == "blocked" and by["d"]["status"] == "pending"
    assert {e["path"] for e in integrity.pending(ents)} == {"b.toml", "c.toml", "u", "d"}


def _entry(kind="cargo-lints", provable=False, digest="d1"):
    return {"id": f"G-{kind}-Cargo.toml", "path": "Cargo.toml", "kind": kind, "detail": "", "digest": digest,
            "provable": provable, "status": "pending", "accepted_by": "", "proof": "", "proof_tree": "",
            "votes": {}, "justification": "", "fix_rounds": 0}


def _vote(e, decision="accept"):
    return [{"id": e["id"], "decision": decision, "reason": "r"}]


def test_apply_votes_and_decide():
    e = _entry()
    integrity.apply_votes([e], "reviewer_a", _vote(e))
    integrity.decide([e])
    assert e["status"] == "pending"                       # one vote is not consent
    integrity.apply_votes([e], "reviewer_b", _vote(e))
    integrity.decide([e])
    assert (e["status"], e["accepted_by"]) == ("accepted", "reviewers")
    integrity.apply_votes([e], "reviewer_b", _vote(e, "reject"))   # a new vote replaces the old one
    integrity.decide([e])
    assert (e["status"], e["accepted_by"]) == ("pending", "")
    # a vote for an old digest does not count; reconcile drops it when the content changes
    e2 = _entry()
    integrity.apply_votes([e2], "reviewer_a", _vote(e2))
    integrity.apply_votes([e2], "reviewer_b", _vote(e2))
    e2["digest"] = "d2"
    integrity.decide([e2])
    assert e2["status"] == "pending"
    # a review without the field casts no vote
    e3 = _entry()
    integrity.apply_votes([e3], "reviewer_a", _vote(e3))
    integrity.apply_votes([e3], "reviewer_b", [])
    integrity.decide([e3])
    assert e3["status"] == "pending" and "reviewer_b" not in e3["votes"]


def test_votes_ignored_for_provable_and_unknown():
    p, u = _entry("config", True), _entry("unknown")
    for r in ("reviewer_a", "reviewer_b"):
        integrity.apply_votes([p, u], r, _vote(p) + _vote(u))
    integrity.decide([p, u])
    assert p["votes"] == {} and u["votes"] == {}
    assert p["status"] == "pending" and u["status"] == "blocked"
    integrity.apply_votes([p], "reviewer_a", [{"id": "G-none", "decision": "accept", "reason": ""}])


# ── C1: the diff reading is immune to git config and in-tree attributes ─────
def _weaken(repo):
    write(repo, "src/lib.rs", "#![allow(warnings)]\npub fn a() {}\n")   # silence added, #[test] removed


def _assert_detected(repo):
    assert sorted(c.kind for c in run(repo)) == ["silence", "tests-removed"]


def test_diff_external_does_not_hide_changes(repo):
    git(repo, "config", "diff.external", "/bin/true")
    _weaken(repo)
    _assert_detected(repo)


def test_gitattributes_nodiff_does_not_hide_changes(repo):
    for attr in ("*.rs -diff", "*.rs binary"):
        write(repo, ".gitattributes", attr + "\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "attr")
        _weaken(repo)
        _assert_detected(repo)
        git(repo, "checkout", "-q", "--", ".")


def test_diff_noprefix_does_not_break_attribution(repo):
    git(repo, "config", "diff.noprefix", "true")
    _weaken(repo)
    _assert_detected(repo)


def test_gitattributes_is_config(repo):
    write(repo, "sub/.gitattributes", "*.rs -diff\n")
    c = only(run(repo))
    assert (c.kind, c.path) == ("config", "sub/.gitattributes")


def test_added_attrs_uses_hardened_diff(repo):
    git(repo, "config", "diff.external", "/bin/true")
    git(repo, "config", "diff.noprefix", "true")
    write(repo, "src/lib.rs", "#![allow(warnings)]\npub fn a() {}\n")
    positions, _ = integrity._added_attrs(repo, "HEAD", head(repo))
    assert positions == {"src/lib.rs": [1]}


def test_reviewer_diff_shows_text_despite_attributes(repo):
    from rustflow import gitutil
    write(repo, ".gitattributes", "*.rs binary\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attr")
    write(repo, "src/lib.rs", "pub fn changed() {}\n")
    git(repo, "add", "-A")
    out = gitutil.diff(repo, "HEAD", gates.tree_fingerprint(repo))
    assert "pub fn changed" in out and "Binary files" not in out


# ── I1: the tests-removed digest covers how many tests were removed ─────────
def test_tests_removed_digest_changes_with_more_removals(repo):
    write(repo, "src/lib.rs", "pub fn a() {}\n\n#[test]\nfn t() {}\n#[test]\nfn t2() {}\n#[test]\nfn t3() {}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "three tests")
    write(repo, "src/lib.rs", "pub fn a() {}\n\n#[test]\nfn t() {}\nfn t2() {}\n#[test]\nfn t3() {}\n")
    one = only(run(repo))
    assert run(repo) == [one]                       # stable for the same change
    write(repo, "src/lib.rs", "pub fn a() {}\n\nfn t() {}\nfn t2() {}\nfn t3() {}\n")
    two = only(run(repo))
    assert (one.id, one.kind) == (two.id, two.kind) and one.digest != two.digest


def test_tests_removed_votes_reset_by_more_removals(repo):
    write(repo, "src/lib.rs", "pub fn a() {}\n\n#[test]\nfn t() {}\n#[test]\nfn t2() {}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "two tests")
    write(repo, "src/lib.rs", "pub fn a() {}\n\n#[test]\nfn t() {}\nfn t2() {}\n")
    entries = integrity.reconcile([], run(repo))
    for r in integrity.REVIEWERS:
        integrity.apply_votes(entries, r, [{"id": entries[0]["id"], "decision": "accept"}])
    integrity.decide(entries)
    assert entries[0]["accepted_by"] == "reviewers"
    write(repo, "src/lib.rs", "pub fn a() {}\n\nfn t() {}\nfn t2() {}\n")
    entries = integrity.reconcile(entries, run(repo))
    integrity.decide(entries)
    assert entries[0]["status"] == "pending" and entries[0]["votes"] == {}
