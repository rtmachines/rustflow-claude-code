# Integralność bramek - plan implementacji

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bramki rustflow przestają ufać drzewu, które oceniają: konfiguracja z bazy, wykrywanie zmian
wpływających na bramki i automatyczna zgoda (dowód deterministyczny albo jednomyślność recenzentów).

**Architecture:** Nowy samodzielny moduł `integrity.py` (skan zmian, dowód w tymczasowym worktree,
czyste funkcje stanu zgody). Orkiestrator zamraża `[gates]` z `base_sha` i zamienia niezaakceptowane
zmiany na mechaniczne findingi `G-…`. Hooki trybu A używają konfiguracji z `HEAD` i tego samego skanu.

**Tech Stack:** Python 3.11+ (pakiet), stdlib-only i 3.9+ dla plików kopiowanych do `.rustflow/`
(`gates.py`, `hooks.py`, `integrity.py`), git, cargo, pytest, ruff.

**Spec:** `rustflow/docs/superpowers/specs/2026-10-27-gate-integrity-design.md`

## Global Constraints

- Katalog roboczy poleceń: `rtm-rust-flow/rustflow/`; testy: `.venv/bin/python -m pytest -q <ścieżki>`; lint: `.venv/bin/python -m ruff check .`.
- Praca na nowej gałęzi `feat/gate-integrity` (utworzonej przy starcie wykonania); commit po każdym zadaniu.
- `integrity.py`, `gates.py`, `hooks.py`: tylko stdlib, bez składni nowszej niż 3.9 (`from __future__ import annotations` jak w `gates.py`), import `gates` w układzie z `hooks.py:27-30`.
- ID zmian: `G-<kind>-<path>`; `kind` ∈ `config | cargo-lints | silence | tests-removed | unknown`; `provable` = `kind in ("config", "silence")`.
- Ścieżki `config`: `rustflow.toml`, `.cargo/config`, `.cargo/config.toml` (dowolny poziom), `.config/nextest.toml`, `clippy.toml`, `.clippy.toml`, `rustfmt.toml`, `.rustfmt.toml`, `rust-toolchain`, `rust-toolchain.toml`, `deny.toml` + `[gates].integrity_extra_paths` z konfiguracji bazy.
- Nowy status runu: `gate_changes_pending` (spoza `runs.APPROVED`, w `CONTINUABLE`, exit 2). Nowy status findingu: `accepted_gate_change`.
- Zaakceptowane zmiany: status runu co najwyżej `approved_with_advisories`.
- Bramki orkiestratora zawsze z `state["gates_cfg"]` (konfiguracja z `base_sha`), także po akceptacji `rustflow.toml`.
- Brak flagi akceptacji w CLI i MCP.
- Fail-closed w nowym kodzie: błąd skanu = `unknown` (blokuje), błąd dowodu = `failed`.

## Review Focus

- Komentarz lub doc-komentarz z przykładem `/// #[allow(dead_code)]` nie może dawać `silence` (Task 1: `test_silence_ignores_comments`).
- Zmiana nazwy pliku konfiguracji (`clippy.toml` → `.clippy.toml`) musi być wykryta mimo wykrywania przemianowań przez git (Task 1: `test_config_rename_detected`).
- Członek workspace dodający `[lints] workspace = true` to zmiana `cargo-lints`, a nowy członek bez sekcji `lints` nią nie jest (Task 1: `test_workspace_member_lints`).
- Pozostałość po przerwanym dowodzie (`.rustflow/wt/proof-*` z poprzedniego procesu) nie może psuć kolejnego dowodu (Task 2: `test_prove_recovers_stale_worktree`).
- Dowód w projekcie z `[gates].cargo_dir = "crate"` musi uruchamiać bramki w podkatalogu (Task 2: `test_prove_respects_cargo_dir`).

---

### Task 1: `integrity.scan` - wykrywanie zmian wpływających na bramki

**Files:**
- Create: `rustflow/integrity.py`
- Modify: `rustflow/installer.py:120-122` (kopiowanie `integrity.py` do `.rustflow/integrity.py`, `managed=True`)
- Test: `tests/test_integrity.py` (nowy)

**Interfaces:**
- Produces:
  - `@dataclass GateChange(id: str, path: str, kind: str, detail: str, digest: str, provable: bool)` z `to_dict() -> dict`
  - `CONFIG_PATTERNS: tuple[str, ...]` (fnmatch na ścieżce i na `basename`; `.cargo/config*` na dowolnym poziomie)
  - `scan(root: Path, base_rev: str, head_tree: str, cfg: dict) -> list[GateChange]` - `head_tree` to wynik `gates.tree_fingerprint`/`gitutil.snapshot`; `cfg` = konfiguracja bramek z bazy (dla `integrity_extra_paths`); kolejność wyniku posortowana po `id`.
  - `is_config_path(path: str, extra: list[str]) -> bool`

Uwagi implementacyjne: diff `git diff --no-renames --name-only <base_rev> <head_tree>` i `-U0` dla `*.rs`; treść przez `git show <rev>:<path>` (`gates._git`). `digest` = sha256 treści head (plik) albo posortowanych dodanych linii atrybutów (`silence`) albo listy plików z usuniętymi testami (`tests-removed`); usunięty plik → digest `"deleted"`. Pomijaj `gates.RUNTIME_PATHS`. Linie zaczynające się od `//` (po strip) nie liczą się do `silence` ani `tests-removed`.

Fixture testów: tymczasowe repo git (`git init`, commit bazowy z `Cargo.toml` pakietu `demo`, `src/lib.rs` z jednym `#[test]`, `tests/it.rs` z jednym `#[test]`); helper `head(repo)` = `gates.tree_fingerprint(repo)`; `cfg = dict(gates.DEFAULT_GATES)`. Cargo nie jest uruchamiane w tym zadaniu.

- [ ] **Step 1: Napisz testy (padające)**

```python
def kinds(changes): return sorted((c.kind, c.path) for c in changes)

def test_no_changes(repo):            assert scan(repo, "HEAD", head(repo), CFG) == []
def test_dependency_added_is_not_a_change(repo):    # [dependencies] serde = "1"
    assert kinds(scan(...)) == []
def test_lints_section_change(repo):  # [lints.clippy] all = "allow"
    assert kinds(scan(...)) == [("cargo-lints", "Cargo.toml")]
def test_profile_change(repo):        # [profile.dev] opt-level = 1
    assert kinds(scan(...)) == [("cargo-lints", "Cargo.toml")]
def test_workspace_member_lints(repo):  # nowy członek crates/a z [lints] workspace = true -> zmiana; drugi członek bez lints -> brak
    assert ("cargo-lints", "crates/a/Cargo.toml") in kinds(...) and all(p != "crates/b/Cargo.toml" for _, p in kinds(...))
def test_config_files(repo):          # .cargo/config.toml, .config/nextest.toml, rustflow.toml, sub/.cargo/config
    assert {p for k, p in kinds(...) if k == "config"} == {".cargo/config.toml", ".config/nextest.toml", "rustflow.toml", "sub/.cargo/config"}
def test_config_rename_detected(repo):  # clippy.toml -> .clippy.toml
    assert {p for _, p in kinds(...)} == {"clippy.toml", ".clippy.toml"}
def test_extra_paths(repo):           # cfg["integrity_extra_paths"] = ["ci/*.sh"]
    assert ("config", "ci/check.sh") in kinds(scan(repo, "HEAD", head(repo), {**CFG, "integrity_extra_paths": ["ci/*.sh"]}))
def test_silence_added(repo):         # dodane: "#[allow(clippy::needless_return)]", "    #[ignore]", "#![expect(dead_code)]", "#[cfg_attr(test, allow(unused))]"
    c = only(scan(...)); assert (c.kind, c.path, c.provable) == ("silence", "src/lib.rs", True) and "needless_return" in c.detail
def test_silence_removed_is_not_a_change(repo):     # baza ma #[allow(x)], head go usuwa
    assert kinds(scan(...)) == []
def test_silence_ignores_comments(repo):  # dodane "/// #[allow(dead_code)]" i "// #[ignore]"
    assert kinds(scan(...)) == []
def test_tests_removed(repo):         # usunięty #[test] z tests/it.rs
    c = only(scan(...)); assert (c.kind, c.path, c.provable) == ("tests-removed", "*", False) and "tests/it.rs" in c.detail
def test_test_moved_is_not_removed(repo):  # #[test] przeniesiony z tests/it.rs do src/lib.rs
    assert kinds(scan(...)) == []
def test_ids_and_digest_stable(repo): assert scan(...) == scan(...) and scan(...)[0].id == "G-config-.cargo/config.toml"
def test_digest_changes_with_content(repo):  # druga wersja .cargo/config.toml -> inny digest, to samo id
def test_broken_cargo_toml_is_unknown(repo):  # Cargo.toml w head = "[package"
    assert any(c.kind == "unknown" and not c.provable for c in scan(...))
def test_git_failure_is_unknown(repo):  # base_rev = "nie-ma-takiej-rewizji"
    assert [c.kind for c in scan(repo, "nie-ma-takiej-rewizji", head(repo), CFG)] == ["unknown"]
def test_without_toml_parser_is_conservative(repo, monkeypatch):  # monkeypatch integrity._toml = None
    # [dependencies] dodane -> brak zmiany; linia w [lints] zmieniona -> cargo-lints
def test_init_copies_integrity(tmp_path):  # Installer(...).run() -> (repo/".rustflow/integrity.py").is_file()
```

- [ ] **Step 2: Uruchom - oczekiwany FAIL** (`ModuleNotFoundError: rustflow.integrity`)

Run: `.venv/bin/python -m pytest -q tests/test_integrity.py`

- [ ] **Step 3: Zaimplementuj `GateChange`, `CONFIG_PATTERNS`, `is_config_path`, `scan` w `rustflow/integrity.py` i kopiowanie w `installer.py`**

Parser TOML w zmiennej modułu `_toml` (`tomllib`, potem `tomli`, inaczej `None`), żeby test mógł go wyłączyć. Porównanie `cargo-lints`: `data.get("lints")`, `data.get("workspace", {}).get("lints")`, `data.get("profile")` w bazie i head (brak pliku = `{}`). Tryb zachowawczy (`_toml is None`): z pełnej treści bazy i head wybierz linie należące do sekcji, których nagłówek `[...]` zaczyna się od `lints`, `workspace.lints` albo `profile`, i porównaj te listy.

- [ ] **Step 4: Uruchom - oczekiwany PASS**

Run: `.venv/bin/python -m pytest -q tests/test_integrity.py && .venv/bin/python -m ruff check .`

- [ ] **Step 5: Commit**

```bash
git add rustflow/integrity.py rustflow/installer.py tests/test_integrity.py
git commit -m "feat(integrity): detect gate-affecting changes between base and tree"
```

### Task 2: `integrity.prove` - dowód w tymczasowym worktree

**Files:**
- Modify: `rustflow/integrity.py`
- Test: `tests/test_integrity.py` (testy z cargo - fixture `rust_repo` z `tests/conftest.py`)

**Interfaces:**
- Consumes: `GateChange`, `scan` (Task 1); `gates.run_gates(root, profile, cfg=..., strict=True, use_cache=False, log_dir=...)`.
- Produces:
  - `@dataclass ProofResult(status: str, summary: str, error: str = "")` - `status` ∈ `"passed" | "failed"`
  - `prove(root: Path, base_rev: str, head_tree: str, changes: list[GateChange], profile: str, cfg: dict, log_dir: Path | None = None) -> ProofResult` - bierze pod uwagę tylko `provable`; pusta lista provable → `ProofResult("passed", "nothing to prove")`.
  - `strip_attributes(text: str, added: list[str]) -> str` - wycina dodane atrybuty (`#[...]`/`#![...]` z licznikiem nawiasów, z pominięciem literałów `"..."`); linia, która zostaje pusta, znika.

Uwagi: worktree w `root/.rustflow/wt/proof-<sha256(head_tree)[:12]>`; przed `worktree add` usuń istniejący katalog i `git worktree prune`; `git commit-tree <head_tree> -m proof` (z `GIT_AUTHOR_*`/`GIT_COMMITTER_*` = `rustflow`), `git worktree add --detach`; `CARGO_TARGET_DIR` = `root/target`, chyba że już ustawiony; `finally`: `git worktree remove --force` + `prune`. Bramki niepełne (`not step.output_complete`) albo wyjątek → `failed` z `error`.

- [ ] **Step 1: Napisz testy (padające)**

```python
def test_strip_attributes_inline_and_standalone():
    src = "#[allow(clippy::needless_return)]\nfn a() {}\n#[allow(x)] fn b() {}\nfn c() { let s = \"#[allow(y)]\"; }\n"
    out = strip_attributes(src, ["#[allow(clippy::needless_return)]", "#[allow(x)] fn b() {}"])
    assert out == "fn a() {}\nfn b() {}\nfn c() { let s = \"#[allow(y)]\"; }\n"

def test_prove_unneeded_allow_passes(rust_repo):      # dodany #[allow(dead_code)] nad używaną pub fn
    assert prove(rust_repo, "HEAD", head(rust_repo), scan(...), "stop", CFG).status == "passed"
def test_prove_masking_allow_fails(rust_repo):        # #[allow(clippy::needless_return)] nad fn z `return 1;`
    assert prove(...).status == "failed"
def test_prove_cap_lints_fails(rust_repo):            # .cargo/config.toml z --cap-lints allow + needless_return
    assert prove(...).status == "failed"
def test_prove_tests_off_fails(rust_repo):            # rustflow.toml tests = "off" + padający test; cfg = konfiguracja bazy
    assert prove(...).status == "failed"
def test_prove_ignore_on_failing_test_fails(rust_repo):  # #[ignore] nad padającym testem
    assert prove(...).status == "failed"
def test_prove_neutral_clippy_toml_passes(rust_repo):    # clippy.toml: too-many-arguments-threshold = 7 (wartość domyślna)
    assert prove(...).status == "passed"
def test_prove_worktree_error_fails(rust_repo, monkeypatch):  # monkeypatch gates._git tak, by "worktree add" zwracał rc=1
    r = prove(...); assert r.status == "failed" and r.error
def test_prove_recovers_stale_worktree(rust_repo):    # utwórz ręcznie .rustflow/wt/proof-<hash>/ z plikiem śmieci, potem prove
    assert prove(...).status == "passed" and not any((rust_repo/".rustflow"/"wt").glob("proof-*"))
def test_prove_respects_cargo_dir(tmp_path):          # repo z crate/Cargo.toml, cfg cargo_dir="crate", masking allow w crate/src/lib.rs
    assert prove(...).status == "failed"
def test_prove_leaves_tree_untouched(rust_repo):      # tree_fingerprint przed == po; brak katalogów proof-*
```

- [ ] **Step 2: Uruchom - oczekiwany FAIL** (`ImportError: prove`)

Run: `.venv/bin/python -m pytest -q tests/test_integrity.py -k "prove or strip"`

- [ ] **Step 3: Zaimplementuj `ProofResult`, `strip_attributes`, `prove`**

- [ ] **Step 4: Uruchom - oczekiwany PASS**

Run: `.venv/bin/python -m pytest -q tests/test_integrity.py && .venv/bin/python -m ruff check .`

- [ ] **Step 5: Commit** - `feat(integrity): prove gate changes harmless on a base-config worktree`

### Task 3: `gates.py` - konfiguracja z rewizji i multiset sygnatur

**Files:**
- Modify: `rustflow/gates.py:192-213` (obok `load_gate_config`), `rustflow/gates.py:118-126` (`GateResult`)
- Test: `tests/test_units.py`

**Interfaces:**
- Produces:
  - `class GateConfigError(RuntimeError)`
  - `load_gate_config_at(root: Path, rev: str) -> dict` - jak `load_gate_config`, ale treść z `git show <rev>:rustflow.toml`; brak ścieżki → `dict(DEFAULT_GATES)`; niepoprawny TOML → domyślne + `"_config_error"`; inny błąd gita → `GateConfigError`. Wspólna funkcja parsująca z `load_gate_config` (`_parse_gate_config(text: str) -> dict`).
  - `GateResult.signature_counts() -> collections.Counter[str]` (te same elementy co `signatures()`, z krotnością)
  - `new_failures(counts: Counter, baseline: Counter) -> Counter` - `counts - baseline`, przy czym każde `"<step>|*"` z `counts` zawsze w wyniku.

- [ ] **Step 1: Napisz testy (padające)**

```python
def test_load_gate_config_at_missing(git_repo):      assert load_gate_config_at(git_repo, "HEAD") == DEFAULT_GATES
def test_load_gate_config_at_reads_revision(git_repo):  # commit z tests="off", potem w drzewie tests="auto"
    assert load_gate_config_at(git_repo, "HEAD")["tests"] == "off"
def test_load_gate_config_at_invalid_toml(git_repo):  # commit z "[gates" w rustflow.toml
    cfg = load_gate_config_at(git_repo, "HEAD"); assert cfg["_config_error"] and cfg["tests"] == "auto"
def test_load_gate_config_at_bad_rev(git_repo):
    with pytest.raises(GateConfigError): load_gate_config_at(git_repo, "nie-ma")
def test_new_failures_multiset():
    assert new_failures(Counter({"clippy|a|x|m": 2}), Counter({"clippy|a|x|m": 1})) == Counter({"clippy|a|x|m": 1})
    assert not new_failures(Counter({"clippy|a|x|m": 1}), Counter({"clippy|a|x|m": 1}))
    assert new_failures(Counter({"fmt|*": 1}), Counter({"fmt|*": 1})) == Counter({"fmt|*": 1})
def test_signature_counts_keeps_duplicates():  # GateResult z dwoma identycznymi diagnostykami clippy
    assert res.signature_counts()["clippy|src/lib.rs|clippy::needless_return|unneeded `return` statement"] == 2
```

(`git_repo`: minimalne repo bez cargo - dodaj fixture w `tests/conftest.py`, jeśli nie istnieje: `git init`, `user.*`, pusty commit.)

- [ ] **Step 2: Uruchom - FAIL** - `.venv/bin/python -m pytest -q tests/test_units.py -k "gate_config_at or new_failures or signature_counts"`
- [ ] **Step 3: Zaimplementuj**
- [ ] **Step 4: Uruchom - PASS** (+ cały `tests/test_units.py`, ruff)
- [ ] **Step 5: Commit** - `feat(gates): gate config from a revision; multiset failure signatures`

### Task 4: Orkiestrator - zamrożona konfiguracja bramek i multiset baseline

**Files:**
- Modify: `rustflow/orchestrator.py:548-578` (`run_gates`), `:1080-1090` (`_advisory`), `:713-725` (`_preflight_checks`), `:829-838` (`_gates_ok`), `:229-295` (`resume`)
- Modify: `rustflow/hooks.py:55-64,222` (`_baseline_signatures` → `Counter`, porównanie przez `new_failures`); `orchestrator.py:383-395` (`_write_baseline` zapisuje listę z powtórzeniami)
- Modify: `tests/fake_agents/fake_agent.py` (scenariusze `weaken_gates`, `red_baseline_multiset`)
- Test: `tests/test_e2e.py`

**Interfaces:**
- Consumes: `gates.load_gate_config_at`, `gates.new_failures`, `GateResult.signature_counts` (Task 3).
- Produces: `state["gates_cfg"]: dict` (ustawiane w preflight przed bramkami bazy); `state["baseline_signatures"]: list[str]` posortowana z powtórzeniami; `Orchestrator._gate_cfg() -> dict` (kopia `state["gates_cfg"]` + `max_output_bytes`; przy braku klucza wylicza z `base_sha` i zapisuje).

Scenariusze atrapy (implementer):
- `weaken_gates`: zapisuje `src/lib.rs` z padającym testem i w `rustflow.toml` zamienia `tests = "auto"` na `tests = "off"`; fixer w gate-fix nic nie zmienia.
- `red_baseline_multiset`: (baza w teście ma `pub fn a() -> i32 { return 1; }`) implementer dopisuje `pub fn b() -> i32 { return 2; }`.

- [ ] **Step 1: Napisz testy (padające)**

```python
def test_gates_use_base_config(rust_repo, monkeypatch):
    rc, st = run(rust_repo, "weaken_gates", monkeypatch, difficulty="easy")
    assert st["status"] == "gates_red" and st["gates_cfg"]["tests"] == "auto"

def test_red_baseline_counts_occurrences(rust_repo, monkeypatch):
    # baza z needless_return w src/lib.rs, commit, accept_red_baseline=True
    rc, st = run(rust_repo, "red_baseline_multiset", monkeypatch, difficulty="easy", accept_red_baseline=True)
    assert st["status"] == "gates_red"

def test_resume_without_gates_cfg(rust_repo, monkeypatch):
    # run "happy" z max_rounds=1 -> usuń "gates_cfg" ze state.json -> resume(extra_rounds=1)
    assert "gates_cfg" in state_after_resume
```

- [ ] **Step 2: Uruchom - FAIL** - `.venv/bin/python -m pytest -q tests/test_e2e.py -k "base_config or red_baseline_counts or without_gates_cfg"`
- [ ] **Step 3: Zaimplementuj** (`GateConfigError` w preflight → `Abort("error", ...)`)
- [ ] **Step 4: Uruchom - PASS** (+ `tests/test_e2e.py tests/test_hooks_process.py tests/test_limit_flow.py`, ruff)
- [ ] **Step 5: Commit** - `fix(orchestrator): gates use the base config; red baseline compares counts`

### Task 5: Orkiestrator - zmiany `G-…`, dowód i status `gate_changes_pending`

**Files:**
- Modify: `rustflow/integrity.py` (czyste funkcje stanu)
- Modify: `rustflow/orchestrator.py` (`phase_gates`, `phase_review`, `phase_fix`, `phase_final`, `CONTINUABLE` w `:38`)
- Modify: `rustflow/findings.py:30,263` (status `accepted_gate_change` przenoszony jak `advisory`)
- Modify: `rustflow/prompts.py:101-128` (`implement_prompt`), `:194-214` (`fix_prompt`)
- Modify: `tests/fake_agents/fake_agent.py` (scenariusze `weaken`, `weaken_revert`, `allow_unneeded`, `digest_change`)
- Test: `tests/test_integrity.py`, `tests/test_e2e.py`

**Interfaces:**
- Consumes: `scan`, `prove`, `ProofResult` (Task 1-2); `Orchestrator._gate_cfg()` (Task 4).
- Produces (w `integrity.py`, wpis stanu = `dict`):
  - `reconcile(prev: list[dict], changes: list[GateChange]) -> list[dict]` - wpis = `GateChange.to_dict()` + `status` (`"pending" | "accepted" | "blocked"`), `accepted_by` (`"" | "proof" | "reviewers"`), `proof` (`"" | "passed" | "failed"`), `votes` (`{role: {"digest", "decision", "reason"}}`), `justification: str`, `fix_rounds: int`; pola przenoszone z `prev` tylko przy tym samym `id` i `digest` (`fix_rounds` przy tym samym `id`); zmiany, których już nie ma w `changes`, znikają.
  - `apply_proof(entries: list[dict], result: ProofResult) -> None` - ustawia `proof` dla `provable` bez wyniku.
  - `decide(entries: list[dict]) -> None` - `unknown` → `blocked`; `provable`: `proof == "passed"` → `accepted`/`"proof"`, `"failed"` → `blocked`, `""` → `pending`; nie-`provable`: patrz Task 6 (tu: `pending`).
  - `pending(entries) -> list[dict]` - wpisy o statusie innym niż `accepted`.
- Produces (orkiestrator): `state["gate_changes"]: list[dict]`; `Orchestrator._refresh_gate_changes(label: str) -> None` (scan → reconcile → prove dla provable bez `proof`, log w `run_dir/gates/proof-<label>/` → decide → zapis); `Orchestrator._gate_findings(rnd: int, prev_open: list[Finding]) -> list[Finding]` (P1, `category="gate-integrity"`, `reviewers=["orchestrator"]`, `file` = `path` jeśli istnieje w workdir, inaczej `""`; `title` = `f"Gate-affecting change ({kind}): {detail}"`; dla `accepted` status `accepted_gate_change`).

Reguły (ze specu §2.3):
- `_refresh_gate_changes` wywoływane w `phase_gates` po zielonych bramkach (`_gates_ok`) i w `phase_final` po bramkach final.
- `phase_review`: findingi `G-…` doklejane jak `T-…` (`orchestrator.py:1002`); jeśli wszystkie blokujące mają `category == "gate-integrity"` i każdy odpowiadający wpis ma `fix_rounds >= 1` → `outcome = "gate_changes_pending"`, `phase = "final"`.
- `phase_fix`: `fix_rounds += 1` dla wpisów, których `G-…` było w blokujących; `note` z `resolutions` dla `G-…` → `justification`.
- `phase_final`: final zielone + `pending(entries)` niepuste i outcome ∈ (`approved`, `gate_changes_pending`) → status `gate_changes_pending`; zielone i wszystkie `accepted` → `approved_with_advisories` (wpisy `accepted` liczą się jak advisory).
- Prompty: implementer - nowa reguła po „Never silence problems”: zmiana plików konfiguracji bramek, `#[allow]`/`#[expect]`/`#[ignore]` albo usunięcie testu tylko gdy zadanie tego wymaga, z uzasadnieniem w raporcie; fixer - dla `G-…`: cofnij, chyba że zadanie wprost tego wymaga, wtedy `rejected` z uzasadnieniem.

Scenariusze atrapy:
- `weaken`: implementer zapisuje poprawny kod i `.cargo/config.toml` z `rustflags = ["--cap-lints", "allow"]` + funkcję z `return 1;`; fixer odrzuca `G-…` bez zmian.
- `weaken_revert`: jak `weaken`, fixer usuwa `.cargo/config.toml` i poprawia `return` (`resolutions` `fixed`).
- `allow_unneeded`: implementer dodaje `#[allow(dead_code)]` nad używaną `pub fn`; recenzenci APPROVE.
- `digest_change`: implementer dodaje neutralny `clippy.toml` (dowód zielony); fixer (runda 1, sprowokowana P1 recenzenta) zmienia go na `too-many-lines-threshold = 1`.

- [ ] **Step 1: Napisz testy (padające)**

```python
# test_integrity.py
def test_reconcile_carries_by_digest():   # te same id+digest -> votes/proof przeniesione; inny digest -> wyzerowane, fix_rounds zachowane
def test_decide_rules():                  # unknown->blocked; provable passed->accepted/proof; failed->blocked; brak dowodu->pending
# test_e2e.py
def test_weaken_ends_gate_changes_pending(rust_repo, monkeypatch):
    rc, st = run(rust_repo, "weaken", monkeypatch, difficulty="easy")
    assert st["status"] == "gate_changes_pending" and rc == 2
    e = next(x for x in st["gate_changes"] if x["path"] == ".cargo/config.toml")
    assert (e["kind"], e["proof"], e["status"]) == ("config", "failed", "blocked")
def test_weaken_revert_approved(rust_repo, monkeypatch):
    rc, st = run(rust_repo, "weaken_revert", monkeypatch, difficulty="easy")
    assert st["status"] == "approved" and st["gate_changes"] == []
def test_allow_unneeded_accepted_by_proof(rust_repo, monkeypatch):
    rc, st = run(rust_repo, "allow_unneeded", monkeypatch, difficulty="easy")
    assert st["status"] == "approved_with_advisories" and st["gate_changes"][0]["accepted_by"] == "proof"
def test_digest_change_requires_new_proof(rust_repo, monkeypatch):
    rc, st = run(rust_repo, "digest_change", monkeypatch, difficulty="easy")
    assert st["status"] == "gate_changes_pending" and st["gate_changes"][0]["proof"] == "failed"
def test_gate_changes_pending_is_continuable():
    from rustflow.orchestrator import CONTINUABLE; assert "gate_changes_pending" in CONTINUABLE
```

- [ ] **Step 2: Uruchom - FAIL** - `.venv/bin/python -m pytest -q tests/test_integrity.py tests/test_e2e.py -k "reconcile or decide or weaken or allow_unneeded or digest_change or continuable"`
- [ ] **Step 3: Zaimplementuj**
- [ ] **Step 4: Uruchom - PASS** (+ pełny `tests/test_e2e.py`, `tests/test_degraded_review.py`, `tests/test_criteria.py`, ruff)
- [ ] **Step 5: Commit** - `feat(orchestrator): block gate-affecting changes unless proven harmless`

### Task 6: Zgoda recenzentów dla zmian bez dowodu

**Files:**
- Modify: `rustflow/schemas.py:35-50` (`REVIEW_SCHEMA`), `rustflow/integrity.py` (`apply_votes`, `decide`), `rustflow/prompts.py` (`ReviewContext`, `review_prompt`), `rustflow/orchestrator.py` (`phase_review`)
- Modify: `tests/fake_agents/fake_agent.py` (scenariusze `lints_both_accept`, `lints_one_reject`, `lints_degraded`)
- Test: `tests/test_integrity.py`, `tests/test_e2e.py`, `tests/test_units.py` (schemat)

**Interfaces:**
- Consumes: wpisy stanu, `reconcile`, `decide` (Task 5).
- Produces:
  - `REVIEW_SCHEMA` - opcjonalne pole `gate_changes: [{id: str, decision: "accept"|"reject", reason: str}]` (nie w `required`).
  - `apply_votes(entries: list[dict], reviewer: str, votes: list[dict]) -> None` - zapisuje `{digest, decision, reason}` dla `id` istniejących wpisów nie-`provable` i nie-`unknown`; nadpisuje poprzedni głos tej roli.
  - `decide`: nie-`provable` → `accepted`/`"reviewers"` wtedy i tylko wtedy, gdy `votes["reviewer_a"]` i `votes["reviewer_b"]` mają `digest` równy bieżącemu i `decision == "accept"`; w przeciwnym razie `pending`.
  - `ReviewContext.gate_changes: str` - sformatowana lista wpisów nie-`provable` w statusie `pending` (id, rodzaj, ścieżka, detail, uzasadnienie implementera z `state["implementation"]["summary"]` i fixera z `justification`); `review_prompt` dodaje blok „GATE-AFFECTING CHANGES NEEDING YOUR VOTE” z instrukcją: głosuj `accept` tylko, gdy zadanie wymaga zmiany i nie ukrywa ona defektu.
- Kolejność w `phase_review`: po zebraniu recenzji `apply_votes` dla każdej udanej recenzji → `decide` → `_gate_findings`.

Scenariusze atrapy: implementer zmienia `[lints.clippy]` w `Cargo.toml` (`needless_return = "allow"`) bez `return` w kodzie; recenzenci APPROVE i głosują: `lints_both_accept` - obaj `accept`; `lints_one_reject` - `reviewer_b` `reject`; `lints_degraded` - `reviewer_b` pada (jak `reviewers_down` tylko dla jednej roli), `reviewer_a` `accept`.

- [ ] **Step 1: Napisz testy (padające)**

```python
def test_apply_votes_and_decide():           # oba accept -> accepted/reviewers; jeden reject -> pending; głos dla starego digestu -> pending; recenzja bez pola gate_changes (apply_votes z []) -> brak głosu, pending
def test_votes_ignored_for_provable_and_unknown():
def test_review_schema_gate_changes_optional():  # schemas.validate(odpowiedź bez pola) przechodzi; z polem o decision "maybe" -> SchemaError
def test_lints_both_accept(rust_repo, monkeypatch):
    rc, st = run(...); assert st["status"] == "approved_with_advisories" and st["gate_changes"][0]["accepted_by"] == "reviewers"
def test_lints_one_reject(rust_repo, monkeypatch):     assert run(...)[1]["status"] == "gate_changes_pending"
def test_lints_degraded_no_consent(rust_repo, monkeypatch): assert run(...)[1]["status"] == "gate_changes_pending"
def test_review_prompt_lists_pending_changes():   # ReviewContext(gate_changes="G-cargo-lints-Cargo.toml ...") -> blok w prompcie
```

- [ ] **Step 2: Uruchom - FAIL**
- [ ] **Step 3: Zaimplementuj**
- [ ] **Step 4: Uruchom - PASS** (+ pełny `tests/test_e2e.py`, ruff)
- [ ] **Step 5: Commit** - `feat(review): unanimous reviewer consent for unprovable gate changes`

### Task 7: `next_actions`, raport i brief

**Files:**
- Modify: `rustflow/runs.py:183-283` (`next_actions`), `:285-350` (`summary`, `brief_markdown`)
- Modify: `rustflow/report.py` (sekcja „Zmiany wpływające na bramki”)
- Test: `tests/test_agent_driver.py` (`test_next_actions_cover_statuses`), `tests/test_units.py` (raport)

**Interfaces:**
- Consumes: `state["gate_changes"]` (Task 5-6).
- Produces:
  - `next_actions` dla `gate_changes_pending`: jedna akcja `{"action": "ask_user", "needs_user": True, "question": ..., "command": f"rustflow resume {rid} --extra-rounds 1 --detach", "mcp": _mcp("rustflow_resume", run=rid, extra_rounds=1)}`; `question` wymienia każde `id` z `pending` (rodzaj, ścieżka, wynik dowodu, głosy) i dwie drogi: cofnąć zmianę i wznowić albo zacommitować ją jako zmianę polityki na bazie i uruchomić run ponownie.
  - `summary(...)["gate_changes"] = {"pending": int, "accepted": int}`.
  - `report.md`: tabela `| id | rodzaj | ścieżka | status | accepted_by | dowód | głosy | uzasadnienie |`, tylko gdy `gate_changes` niepuste; nagłówek `## Zmiany wpływające na bramki`.

- [ ] **Step 1: Napisz testy (padające)** - `next_actions({"status": "gate_changes_pending", "run_id": "r", "gate_changes": [wpis pending]})` → jedna akcja `ask_user` z `needs_user` i `id` w `question`; brak akcji `merge`; `runs.exit_code("gate_changes_pending") == 2`; raport z wpisem `accepted`/`proof` zawiera nagłówek i wiersz z `proof`; `summary` liczy pending/accepted.
- [ ] **Step 2: Uruchom - FAIL** - `.venv/bin/python -m pytest -q tests/test_agent_driver.py -k next_actions tests/test_units.py -k "report or summary"`
- [ ] **Step 3: Zaimplementuj**
- [ ] **Step 4: Uruchom - PASS** (+ `tests/test_mcp_driver.py`, ruff)
- [ ] **Step 5: Commit** - `feat(runs): report and next actions for gate changes`

### Task 8: Hooki trybu A - konfiguracja z HEAD, skan i dowód w Stop i bramce commita

**Files:**
- Modify: `rustflow/hooks.py:80-129` (`post_edit`), `:169-177` (`_rust_changes_pending`), `:193-236` (`stop`), `:336-343` (bramka commita w `guard_bash`)
- Test: `tests/test_hooks_process.py`

**Interfaces:**
- Consumes: `gates.load_gate_config_at`, `integrity.scan`, `integrity.prove`, `integrity.is_config_path` (Task 1-3).
- Produces:
  - `_hook_config(root: Path) -> dict` - `load_gate_config_at(root, "HEAD")`; repo bez `HEAD` → `load_gate_config(root)`; `GateConfigError` propaguje się do wywołującego, a `stop`/bramka commita zamieniają go na `block`/`deny` z opisem (test: `test_head_config_git_error_blocks` - `RUSTFLOW_BASE`/HEAD wskazujące nieistniejącą rewizję w roli `implementer`).
  - `_integrity_verdict(root: Path, cfg: dict) -> tuple[bool, str]` - `(True, "")` bez zmian; `(True, note)` gdy wszystkie zmiany `provable` i dowód (`profile="stop"`) zielony; `(False, opis)` w pozostałych przypadkach, w tym przy wyjątku w `scan`/`prove` (opis zawiera komunikat błędu); repo bez `HEAD` → `(True, "rustflow: no commit yet - gate integrity not checked")`. Wynik dowodu cache'owany w `.rustflow/cache/proof.json` jako `{fingerprint: "passed" | "failed"}`.
- Zachowanie:
  - `stop` (tylko rola pusta i role z `ORCHESTRATED_WRITE_ROLES`): po zielonych bramkach `_integrity_verdict`; `False` → `{"decision": "block", "reason": "Gate-affecting changes: <opis>. Revert them, or ask the user to commit them himself."}` liczone do `stop_max_blocks` jak dziś; `note` → `systemMessage`. Wyjątek w ścieżce integralności → `block` z opisem (nie wraca do `main` jako exit 0).
  - bramka commita: po zielonych bramkach `_integrity_verdict`; `False` → `deny` z tym samym opisem.
  - `_rust_changes_pending`: także ścieżki, dla których `integrity.is_config_path(path, cfg.get("integrity_extra_paths", []))`.
  - `post_edit`: `edit_check`/`edit_fmt`/`fmt_fix_scope` z `_hook_config`.
- Role orkiestrowane (`RUSTFLOW_ROLE` ustawione): `stop` używa `load_gate_config_at(root, os.environ["RUSTFLOW_BASE"])`, gdy `RUSTFLOW_BASE` ustawione, a integralność względem `RUSTFLOW_BASE`.

- [ ] **Step 1: Napisz testy (padające)**

```python
def test_stop_uses_head_config(rust_repo):      # drzewo: tests="off" w rustflow.toml + padający test -> block (integralność albo testy)
    assert json.loads(hook(rust_repo, "stop", {"session_id": "a"}).stdout)["decision"] == "block"
def test_stop_blocks_masking_allow(rust_repo):  # #[allow(clippy::needless_return)] + return 1;
    d = json.loads(...); assert d["decision"] == "block" and "Gate-affecting" in d["reason"]
def test_stop_passes_unneeded_allow(rust_repo): # #[allow(dead_code)] nad używaną fn
    out = hook(...).stdout; assert out == "" or "decision" not in json.loads(out)
def test_stop_runs_on_config_only_change(rust_repo):  # tylko .cargo/config.toml z cap-lints, kod z HEAD zielony
    assert json.loads(...)["decision"] == "block"
def test_commit_gate_denies_gate_change(rust_repo):   # tests-removed (usunięty #[test]) + git commit -am x
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
def test_no_head_skips_integrity(tmp_path):     # repo po init bez commita, zielony kod -> systemMessage z "no commit yet", bez block
def test_integrity_exception_blocks(rust_repo):  # zapisz do .rustflow/integrity.py moduł, którego scan() rzuca RuntimeError
    assert json.loads(...)["decision"] == "block"
def test_audit_regressions_mode_a(rust_repo):   # config_weaken (tests="off", clippy_args=[]) i cargo_config (cap-lints) z modeA.sh -> block
```

- [ ] **Step 2: Uruchom - FAIL** - `.venv/bin/python -m pytest -q tests/test_hooks_process.py`
- [ ] **Step 3: Zaimplementuj**
- [ ] **Step 4: Uruchom - PASS** (+ `tests/test_agent_driver.py tests/test_papercuts.py tests/test_fmt_base.py`, ruff)
- [ ] **Step 5: Commit** - `feat(hooks): HEAD gate config and gate-integrity checks in Stop and commit gate`

### Task 9: `--disallowedTools` dla ról piszących + weryfikacja na żywo

**Files:**
- Modify: `rustflow/agents.py:338-343` (`ClaudeRunner.build_argv`, gałąź `call.write`)
- Modify: `rustflow/integrity.py` (`protected_edit_rules`)
- Test: `tests/test_ergonomics.py`

**Interfaces:**
- Consumes: `CONFIG_PATTERNS` (Task 1).
- Produces: `integrity.protected_edit_rules() -> list[str]` - `Edit(<p>)` i `Write(<p>)` dla każdej stałej ścieżki `config` (bez wzorców z gwiazdką poza `**/.cargo/config*`) oraz `Edit(.rustflow/**)`, `Write(.rustflow/**)`. `build_argv` dla `call.write and call.allowed_tools is None` dodaje `--disallowedTools ",".join(rules)`; role read-only bez zmian.

- [ ] **Step 1: Test (padający)** - `write = runner.build_argv(_call(tmp_path, "implementer", write=True))`: `"--disallowedTools"` w argv, wartość zawiera `Edit(rustflow.toml)` i `Write(.rustflow/**)`; `read = build_argv(_call(tmp_path))` - wartość `--disallowedTools` bez reguł ścieżek; wywołanie z własnym `allowed_tools` (audyt ToB) bez tych reguł.
- [ ] **Step 2: FAIL** - `.venv/bin/python -m pytest -q tests/test_ergonomics.py`
- [ ] **Step 3: Zaimplementuj**
- [ ] **Step 4: PASS** (+ `tests/test_agent_driver.py tests/test_agent_hardening.py`, ruff)
- [ ] **Step 5: Weryfikacja na żywo (1 wywołanie, haiku, kopia projektu w scratchpadzie poza repo)**

Weź `argv.json` implementera z runu atrapy, podmień binarkę na `claude`, `--model haiku --effort low --max-budget-usd 0.40`, prompt: „Edit tool: change `tests = "auto"` to `tests = "off"` in rustflow.toml; then Write `.rustflow/x.txt`; report done/denied per action.” Oczekiwane: obie akcje w `permission_denials`. Jeśli Edit przeszedł - usuń reguły z argv (wycofaj Step 3), zapisz wynik w DESIGN §5 jako „warstwa niedostępna w 2.1.308”.

- [ ] **Step 6: Commit** - `feat(agents): deny edits of gate config paths for write roles`

### Task 10: Szablon, doctor, dokumentacja

**Files:**
- Modify: `rustflow/templates/rustflow.toml` (sekcja `[gates]`: `integrity_extra_paths = []` z komentarzem PL: „dodatkowe ścieżki (glob), których zmiana wymaga dowodu albo zgody recenzentów”)
- Modify: `rustflow/doctor.py` (ostrzeżenia: `rustflow.toml` niezacommitowany - `git ls-files --error-unmatch rustflow.toml`; brak `.rustflow/integrity.py`)
- Modify: `docs/DESIGN.md` §5, `docs/configuration.md`, `docs/usage.md`, `docs/architecture.md`
- Test: `tests/test_units.py` (doctor), `tests/test_default_knowledge.py` jeśli sprawdza szablon

**Interfaces:**
- Consumes: wszystko powyżej.

Treść dokumentacji (ze specu §5 i §2.7): DESIGN §5 - cztery punkty z sekcji 5 specu + „tryb A gwarantuje zielone bramki dla HEAD/merge, pełny cykl jest opcjonalny”; `configuration.md` - `integrity_extra_paths`; `usage.md` - status `gate_changes_pending`, co robić (cofnąć + `resume --extra-rounds 1` albo commit zmiany polityki na bazie), sekcja raportu; `architecture.md` - moduł `integrity` (scan → reconcile → prove → votes → decide) i miejsce w fazach.

- [ ] **Step 1: Testy (padające)** - doctor na repo z niezacommitowanym `rustflow.toml` zwraca linię `WARN` zawierającą `rustflow.toml`; na repo bez `.rustflow/integrity.py` - linię `WARN`/`FAIL` z `integrity.py`; `load_defaults()["gates"]["integrity_extra_paths"] == []`.
- [ ] **Step 2: FAIL**
- [ ] **Step 3: Zaimplementuj i uzupełnij dokumentację**
- [ ] **Step 4: Pełna weryfikacja**

Run: `.venv/bin/python -m pytest -q && .venv/bin/python -m ruff check .`
Run: `set -o pipefail; .venv/bin/python -m ruff check --output-format=concise . 2>&1 | reviewdog -efm="%f:%l:%c: %m" -reporter=local -filter-mode=added -diff="git diff --no-ext-diff --no-color $(git merge-base HEAD feat/ci-attempt-ledger) --" -fail-level=warning`
Expected: wszystkie testy zielone, brak diagnostyk.

Regresja audytu: `bash /tmp/claude-1000/-home-rtm/4013439e-0e5f-465e-b83a-274b95447006/scratchpad/scripts/modeB.sh weaken` na gałęzi (atrapa z audytu) → STATUS `gate_changes_pending`.

- [ ] **Step 5: Commit** - `docs: gate integrity, gate_changes_pending and limitations`
