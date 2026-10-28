# Integralność bramek: konfiguracja z bazy, wykrywanie osłabień, automatyczna zgoda

Data: 2026-10-27. Podprojekt 1 z 3 planu usunięcia słabości z audytu wymuszania cyklu
(`raport-wymuszanie-cyklu.md`, pozycje #6, #18, #20; częściowo #17 i #4).
Kolejne podprojekty: 3 (twarde hooki trybu A, w tym #16), potem 2 (uczciwe statusy orkiestratora).

## 1. Cel i kryteria sukcesu

Bramki nie mogą być osłabiane przez drzewo, które same oceniają.

- Zmiana konfiguracji bramek, lint-configu albo wyciszenie w kodzie (`#[allow]`, `#[ignore]`,
  usunięte testy) nie może po cichu doprowadzić do statusu z `APPROVED` (tryb B) ani przejść przez
  Stop i bramkę commita (tryb A).
- Zasadne zmiany konfiguracji pozostają możliwe, ale bez udziału człowieka w runie. O zgodzie
  decyduje dowód deterministyczny, a gdzie dowodu nie ma, jednomyślna zgoda obu recenzentów.
- Każda zaakceptowana zmiana jest widoczna w raporcie (kto zaakceptował i dlaczego), a run kończy
  się co najwyżej statusem `approved_with_advisories`.
- Scenariusze z audytu `weaken` (B), `config_weaken` i `cargo_config` (A) kończą się blokadą.

Ustalenia z rozmowy:
- tryb A gwarantuje zielone bramki dla tego, co trafia do HEAD lub merge'a; pełny cykl pozostaje
  opcjonalny (#1 trafia do DESIGN §5 jako świadome ograniczenie);
- zmiany wpływające na bramki są blokowane, a zgoda jest automatyczna (dowód + recenzenci);
- wyciszanie w kodzie podlega tej samej polityce co zmiany konfiguracji.

Poza zakresem: uszczelnienie samej bramki commita, worktree, fail-closed całych hooków i cache
(podprojekt 3); walidacja kotwic, status degraded, porażka plannera (podprojekt 2).

## 2. Komponenty

### 2.1 `rustflow/integrity.py` (nowy, tylko stdlib, samodzielny)

`rustflow init` kopiuje go do `.rustflow/integrity.py` (obok `gates.py`, `hooks.py`, `process.py`).
Importuje `gates` w tym samym układzie co `hooks.py` (najpierw obok siebie, potem z pakietu).

```python
@dataclass
class GateChange:
    id: str          # stabilne: "G-<kind>-<path>", np. "G-config-.cargo/config.toml"
    path: str
    kind: str        # config | cargo-lints | silence | tests-removed | unknown
    detail: str      # krótki opis dla promptu i raportu
    digest: str      # sha256 treści po zmianie (plik albo posortowane dodane linie atrybutów)
    provable: bool   # kind in ("config", "silence")

def scan(root: Path, base_rev: str, head_tree: str, cfg: dict) -> list[GateChange]
def prove(root: Path, base_rev: str, head_tree: str, changes: list[GateChange],
          profile: str, cfg: dict, log_dir: Path | None) -> ProofResult
```

Wykrywanie w `scan`:

| kind | Reguła |
|---|---|
| `config` | `git diff --name-only base head` zawiera ścieżkę pasującą do: `rustflow.toml`, `.cargo/config`, `.cargo/config.toml` (na dowolnym poziomie), `.config/nextest.toml`, `clippy.toml`, `.clippy.toml`, `rustfmt.toml`, `.rustfmt.toml`, `rust-toolchain`, `rust-toolchain.toml`, `deny.toml` oraz wzorców z `[gates].integrity_extra_paths` (czytanych z konfiguracji bazy). Jeden `GateChange` na plik. |
| `cargo-lints` | Dla każdego zmienionego `Cargo.toml`: porównanie po parsowaniu (`tomllib`/`tomli`) wartości `lints`, `workspace.lints` i `profile`. Różnica → jeden `GateChange` na plik. Bez parsera TOML: każda zmieniona linia w tych sekcjach (śledzenie nagłówków `[...]` w treści bazy i head) liczy się jako zmiana. |
| `silence` | Dodane linie diffu `*.rs` (`-U0`) pasujące do `#!?\[\s*(allow\|expect)\s*\(`, `cfg_attr\s*\(.*\b(allow\|expect)\s*\(` albo `#\[\s*ignore\b`. Jeden `GateChange` na plik; `detail` wymienia atrybuty. |
| `tests-removed` | Suma netto w całym diffie: liczba usuniętych linii z atrybutem testowym (`#\[\s*(\w+::)*test\b`, np. `#[test]`, `#[tokio::test]`) minus liczba dodanych > 0. Jeden `GateChange` (`path` = "*"), `detail` wymienia pliki z usunięciami. |
| `unknown` | Błąd `git diff`/`git show` albo nieparsowalny `Cargo.toml` w head. `detail` = komunikat błędu. |

Pliki z `RUNTIME_PATHS` (`target`, `.rustflow/runs`, `.rustflow/cache`, `.rustflow/wt`) są pomijane.

Dowód w `prove` (tylko dla `provable`):
1. `git commit-tree <head_tree>` → tymczasowy commit; `git worktree add --detach <tmp> <commit>`
   w `.rustflow/wt/proof-<krótki hash>`; `CARGO_TARGET_DIR` = target projektu (współdzielony).
2. W worktree: każdy `config` przywrócony do wersji z bazy (`git show base:path`) albo usunięty,
   jeśli w bazie go nie było. Każdy dodany atrybut `silence` wycięty tekstowo: od `#[`/`#![` do
   pasującego `]` (licznik nawiasów, z pominięciem literałów `"..."`), także gdy stoi w jednej linii
   z kodem; linia, która zostanie pusta, jest usuwana.
3. `gates.run_gates(tmp, profile, cfg=<konfiguracja z bazy>, strict=True, use_cache=False)`.
4. `ProofResult(ok, per_id: dict[id, "passed"|"failed"], summary, log_dir)`: wszystkie `provable`
   dostają ten sam wynik (jeden przebieg dla całego zestawu).
5. Worktree zawsze usuwane (`finally`). Błąd tworzenia albo niepełne wyjście bramek → `failed`.

### 2.2 `gates.py`

- `load_gate_config_at(root, rev) -> dict`: `git show <rev>:rustflow.toml`. Brak ścieżki →
  `DEFAULT_GATES`. Niepoprawny TOML → `DEFAULT_GATES` + `_config_error`. Inny błąd gita → wyjątek
  `GateConfigError`.
- `GateResult.signature_counts() -> Counter` (obok istniejącego `signatures()`).
- `new_failures(counts: Counter, baseline: Counter) -> Counter`: nadmiar względem bazy;
  `"step|*"` zawsze jest nowe (jak dziś).

### 2.3 Orkiestrator (tryb B)

- Preflight: `state["gates_cfg"] = load_gate_config_at(repo, base_sha)`. `run_gates` i `_advisory`
  używają `state["gates_cfg"]` zamiast `load_gate_config(self.workdir)` (dziś
  `orchestrator.py:560`, `:1087`). `resume` czyta ją ze state.
- `baseline_signatures` zapisywane jako posortowana lista z powtórzeniami; `_gates_ok` porównuje
  przez `new_failures`. Stara lista bez powtórzeń = krotności 1 (surowiej).
- Po każdym `run_gates` w `phase_gates` i `phase_final`: `integrity.scan(workdir, base_sha,
  snapshot, gates_cfg)` → `state["gate_changes"]` (lista słowników z `GateChange` + `status` +
  `accepted_by` + `votes`).
- Dowód: w `phase_gates`, gdy są `provable` bez wyniku dla bieżącego `digest`. Wynik zapisany
  przy zmianie (`proof: passed|failed`, `proof_tree`), log w `runs/<id>/gates/proof-r<n>/`.
- Findingi mechaniczne: każda zmiana o statusie innym niż `accepted` daje finding P1 `G-…`,
  `category="gate-integrity"`, `reviewers=["orchestrator"]`, liczony od nowa w każdej rundzie
  (wzorzec `_mechanical_findings`, `orchestrator.py:608`). Recenzent ani fixer nie mogą go zamknąć.
- Zgoda recenzentów (tylko `not provable` i bez `unknown`): `REVIEW_SCHEMA` dostaje opcjonalne pole
  `gate_changes: [{id, decision: "accept"|"reject", reason}]`. Dla pary `(id, digest)` przechowywany
  jest ostatni głos każdej roli. Status `accepted` (`accepted_by="reviewers"`) wtedy i tylko wtedy,
  gdy ostatni głos `reviewer_a` i `reviewer_b` to `accept`. Zmiana digestu kasuje głosy.
- Zmiana `provable` z `proof: passed` → `accepted` (`accepted_by="proof"`). `proof: failed` →
  blokuje, głosy recenzentów są ignorowane.
- `unknown` nigdy nie jest akceptowane.
- Stan findingu po akceptacji: `accepted_gate_change` (nowa wartość w `Finding.status`,
  przenoszona między rundami jak `advisory`).
- Prompty: implementer i fixer dostają regułę „zmiana konfiguracji bramek, `#[allow]`/`#[ignore]`
  albo usunięcie testu wymaga uzasadnienia w raporcie (fixer: w `resolutions` dla `G-…`)”; fixer
  dostaje też: „cofnij, chyba że zadanie wprost tego wymaga”. Recenzent dostaje listę `G-…`
  czekających na zgodę (ścieżka, rodzaj, fragment diffu, uzasadnienie) i instrukcję głosowania.
- Decyzja po recenzji: jeśli jedyne blokujące findingi to `G-…`, a runda nie jest pierwszą z nimi
  (fixer już je widział), outcome = `gate_changes_pending` → final.
- `phase_final`: jeśli istnieje `G-…` bez `accepted` → status `gate_changes_pending`. Zaakceptowane
  zmiany liczą się jak advisory: status co najwyżej `approved_with_advisories`.
- `gate_changes_pending` dopisany do `CONTINUABLE` (`orchestrator.py:38`); nie należy do
  `runs.APPROVED`; exit 2.

### 2.4 `runs.py`, raport, MCP

- `next_actions` dla `gate_changes_pending`: `ask_user` z listą zmian (id, ścieżka, rodzaj, detail,
  wynik dowodu, głosy, uzasadnienie) i pytaniem: cofnąć zmianę (`resume --extra-rounds 1` po
  ręcznym cofnięciu), czy zacommitować ją jako zmianę polityki na bazie i uruchomić run ponownie.
  Brak flagi akceptacji: ani CLI, ani MCP nie mają ścieżki zgody z zewnątrz.
- `report.md`: sekcja „Zmiany wpływające na bramki”: tabela id | rodzaj | ścieżka | status |
  accepted_by | dowód | głosy | uzasadnienie.
- Brief (`status`, `wait`, MCP): liczba zmian pending/accepted w podsumowaniu; schematy MCP bez
  zmian (nowa wartość `status`).

### 2.5 Hooki (tryb A)

- `post_edit`, `stop`, bramka commita w `guard_bash`: konfiguracja z `load_gate_config_at(root,
  "HEAD")`. Brak `HEAD` (repo bez commitów) → `load_gate_config(root)` jak dziś + kontrola
  integralności pominięta z `systemMessage`.
- `stop` i bramka commita: `scan(root, "HEAD", tree_fingerprint(root), cfg)`. Jeśli są zmiany:
  - wszystkie `provable` i `prove(..., profile="stop")` zielony → przepuszcza, `systemMessage`
    z listą zaakceptowanych zmian;
  - w przeciwnym razie Stop: `decision: block`, bramka commita: `deny`, z komunikatem „zmiany
    wpływające na bramki: … - cofnij je albo poproś użytkownika, żeby sam je zacommitował”.
- Wynik dowodu w `.rustflow/cache/proof.json` po fingerprincie drzewa (jak `gates.json`;
  uszczelnienie obu cache w podprojekcie 3).
- Wyjątek w `scan`/`prove` w tych ścieżkach → blokada z opisem błędu (nie exit 0).
- `_rust_changes_pending` uwzględnia ścieżki `config` (dziś `rustflow.toml`, `.cargo/config.toml`,
  `.config/nextest.toml` nie uruchamiają Stop; część #4).

### 2.6 `agents.py` (obrona w głąb, #18)

Role piszące (`call.write` i `call.allowed_tools is None`) dostają `--disallowedTools` z
`Edit(<ścieżka>)` i `Write(<ścieżka>)` dla stałych ścieżek `config` oraz `Edit(.rustflow/**)`,
`Write(.rustflow/**)`. Warunek: weryfikacja na żywo (sekcja 4); jeśli Claude Code nie respektuje
reguł ze ścieżką w tej fladze, warstwa wypada, a detekcja zostaje.

### 2.7 Instalator, doctor, dokumentacja

- `installer.py`: kopiowanie `integrity.py` (managed).
- `doctor`: ostrzeżenie, gdy `rustflow.toml` nie jest zacommitowany (bazą są wtedy domyślne
  `[gates]`) i gdy brak `.rustflow/integrity.py`.
- `templates/rustflow.toml`: `integrity_extra_paths = []` z komentarzem.
- Dokumentacja: DESIGN §5 (tryb A gwarantuje zielone bramki, cykl opcjonalny; zgoda recenzentów
  to osąd LLM; dowód dotyczy tylko bieżącego drzewa), `configuration.md`, `usage.md` (status
  `gate_changes_pending`, sekcja raportu), `architecture.md` (moduł `integrity`).

## 3. Obsługa błędów

| Sytuacja | Zachowanie |
|---|---|
| `scan`: błąd gita / nieparsowalny `Cargo.toml` w head | `GateChange(kind="unknown")`, blokuje, nigdy nie akceptowane |
| `prove`: worktree się nie tworzy / bramki niepełne / wyjątek | wynik `failed` |
| `load_gate_config_at`: brak `rustflow.toml` w bazie | `DEFAULT_GATES` |
| `load_gate_config_at`: niepoprawny TOML w bazie | `DEFAULT_GATES` + `_config_error` (komunikat jak dziś) |
| `load_gate_config_at`: inny błąd gita | tryb B: `Abort("error")` w preflight; tryb A: blokada z opisem |
| Repo bez `HEAD` (tryb A) | kontrola pominięta, `systemMessage` |
| Stary `state.json` (bez `gates_cfg`) przy `resume` | `load_gate_config_at(repo, base_sha)` i zapis do state |
| Recenzent bez pola `gate_changes` | brak głosu (nie `accept`) |

## 4. Testy i weryfikacja

- `tests/test_integrity.py` (nowy): każdy `kind` wykrywany i niewykrywany (dodanie zależności,
  przeniesienie testu, `#[allow]` w usuniętej linii); stabilność `id`/`digest`; `unknown`; tryb bez
  parsera TOML (monkeypatch); `prove`: zielony (zbędny `#[allow]`, neutralny `clippy.toml`), czerwony
  (`--cap-lints allow`, `tests = "off"` przy padającym teście, `#[ignore]` na padającym teście),
  nieudany (błąd tworzenia worktree); wycinanie atrybutu w linii z kodem.
- `tests/test_units.py`: `load_gate_config_at` (brak pliku, niepoprawny TOML, poprawny);
  `new_failures` (drugie identyczne wystąpienie, `|*`, stary state).
- e2e z atrapą agentów (`tests/fake_agents/fake_agent.py`, nowe scenariusze): `weaken` →
  `gate_changes_pending`, bramki na konfiguracji bazy; `weaken_revert` (fixer cofa) → `approved`;
  `allow_unneeded` → `approved_with_advisories`, `accepted_by=proof`; `lints_both_accept` →
  `approved_with_advisories`, `accepted_by=reviewers`; `lints_one_reject` i `lints_degraded` →
  `gate_changes_pending`; `digest_change` po akceptacji → ponownie pending; `red_baseline_multiset`
  → blokada; `resume` starego state bez `gates_cfg`.
- `tests/test_hooks_process.py`: Stop ignoruje osłabiony `rustflow.toml` z drzewa (konfiguracja
  z HEAD); Stop blokuje przy `silence` z czerwonym dowodem i przepuszcza z zielonym; bramka commita
  odrzuca; zmiana tylko `.cargo/config.toml` uruchamia Stop; repo bez HEAD; wyjątek w `scan` →
  blokada.
- argv: role piszące mają `--disallowedTools` z regułami ścieżek, recenzenci bez zmian.
- `schemas`: `REVIEW_SCHEMA` z opcjonalnym `gate_changes`; odpowiedź bez pola przechodzi walidację.
- Regresja audytu: `config_weaken`, `cargo_config` (tryb A) i `weaken` (tryb B) jako testy.
- Weryfikacja na żywo (1 wywołanie haiku, kopia poza repo): czy `--disallowedTools
  "Edit(rustflow.toml)"` odrzuca Edit tej ścieżki w Claude Code 2.1.308.
- `ruff` i pełny `pytest` zielone; `reviewdog` na zmienionych plikach.

## 5. Świadome ograniczenia (do DESIGN §5)

- Dowód sprawdza tylko bieżące drzewo: zaakceptowana zmiana konfiguracji może osłabić bramki dla
  przyszłych zmian po merge'u (widoczne w raporcie).
- Zgoda recenzentów to osąd dwóch modeli; skorelowany błąd albo prompt injection w diffie może dać
  fałszywą zgodę dla `cargo-lints` i `tests-removed` (ślad w raporcie, status
  `approved_with_advisories`).
- Tryb A: zgodą dla zmian bez dowodu jest commit człowieka poza Claude Code; dopóki podprojekt 3 nie
  uszczelni bramki commita, obejścia z #2 nadal działają.
- `cargo-lints` bez parsera TOML (Python < 3.11 bez `tomli`) działa zachowawczo i może blokować
  zmiany neutralne.
