# rustflow: Claude Code + Codex workflow dla Rusta

Orkiestrator CLI (Python 3.11+, tylko stdlib, Windows i Linux), który prowadzi zadanie od promptu do
gałęzi gotowej do merge:

```
preflight -> triage -> [plan] -> implement -> GATES -> review x2 (Claude + Codex) -> aggregate
                                                ^                                      |
                                                +------ fix <---- blokujące uwagi -----+
                                                                  brak -> final gates (CI) -> raport
```

Dlaczego taka kolejność i co zmieniłem względem pierwotnej propozycji: [docs/DESIGN.md](docs/DESIGN.md).

## Instalacja

```bash
pip install ./rustflow            # albo: pipx install ./rustflow  /  uv tool install ./rustflow
cd <twoje-repo-rust>
rustflow init                     # config, skills, hooki, REVIEW.md, bloki AGENTS.md/CLAUDE.md, CI
rustflow doctor                   # narzędzia, CLI, modele/efforty Codex, pliki
git add -A && git commit -m "rustflow"
```

Wymagane: Python 3.11+ dla CLI (hooki i `gates.py` działają już od 3.9; `doctor` sprawdza interpreter hooków), `git`, `cargo` + `rustfmt` + `clippy`, `claude` (Claude Code ≥ 2.1.259) i/lub `codex`
(≥ 0.16x), zalogowane. Zalecane: `rust-analyzer`, `cargo-nextest`, `cargo-deny`.
`rustflow init --with-apollo` dokłada skill Apollo `rust-best-practices` (kopia przypięta w repo).

## Użycie

```bash
rustflow triage "Dodaj cache LRU z Mutex do crate core"   # trudność + routing modeli, bez agentów
rustflow run "Dodaj endpoint GET /books ... z testami"    # pełny cykl
rustflow run -f zadanie.md --difficulty hard              # zadanie z pliku, wymuszona trudność
rustflow run "..." --criteria kryteria.toml               # kryteria z wymaganymi testami (patrz niżej)
rustflow run "..." --dry-run                              # tylko prompty i komendy do .rustflow/runs/
rustflow review --base origin/main                        # recenzja istniejących zmian (raport)
rustflow review --base origin/main --fix                  # + pętla naprawcza w miejscu
rustflow resume                                           # wznów przerwany run (Ctrl+C, budżet)
rustflow status                                           # stan ostatniego runu
rustflow gates --profile full                             # bramki ręcznie (to samo co hooki i CI)
rustflow lints [--apply]                                  # zapadka lintów: włącz te, które kod już spełnia
rustflow doctor --selftest                                # odegraj hooki jak Claude Code (sprawdź instalację, też na Windows)
rustflow init --stack axum-sqlx                           # skill stacku Axum/SQLx/Askama/htmx + CI z Postgres
```

Wynik runu: `.rustflow/runs/<id>/report.md` (status, routing, koszty, findingi z każdej rundy,
sporne punkty do decyzji, ESCALATE, komendy merge), plus `state.json`, prompty, argv i surowe
wyjścia każdego agenta w `agents/`, wyniki bramek w `gates/`.

Statusy: `approved`, `approved_with_advisories`, `max_rounds`, `stalled`, `gates_red`,
`final_gates_red`, `criteria_unmet`, `baseline_red`, `changes_requested` (review bez `--fix`), `budget`,
`interrupted`, `tree_changed`. Kod wyjścia 0 tylko dla `approved*`.

### Kryteria akceptacji z wymaganym testem

Kryterium ocenia recenzent, ale jeśli ma `test`, to o jego spełnieniu decyduje orkiestrator z wyniku
testów, a nie model: test o tej nazwie musi się uruchomić i przejść na bieżącym drzewie. Zielony
`cargo test`, który puścił 0 testów, test `#[ignore]` albo inny test nie wystarczą.

```toml
# kryteria.toml  ->  rustflow run "..." --criteria kryteria.toml   (także `rustflow review`)
[[criteria]]
text = "Spacje wokół kluczy i wartości są obcinane"
test = "parser::tests::trims_whitespace"     # pełna ścieżka albo krótka nazwa (gdy jednoznaczna)

[[criteria]]
text = "Publiczne API bez zmian"             # bez `test`: ocenia tylko recenzent
```

Planner (zadania hard) sam nadaje kryteriom nazwy testów. Niespełnione kryterium to blokująca uwaga
`T-<id>` w każdej rundzie, a na końcu status `criteria_unmet`. Raport ma tabelę kryteriów: wymagany
test, wynik sprawdzenia i oceny recenzentów.

### Wyniki dotyczą jednego drzewa

- Pełne bramki odrzucają run, w którym `build.rs`, test lub narzędzie zmieniło pliki źródłowe w trakcie
  bramek (krok `tree`). Bez `--locked` wolno tylko utworzyć/zaktualizować `Cargo.lock`.
  `[gates].tree_stable = false` wyłącza to sprawdzenie.
- Jeśli pliki zmienią się w czasie recenzji (recenzenci są tylko do odczytu, więc edytował ktoś inny),
  wyniki recenzji są odrzucane, status `tree_changed`; `rustflow resume` puszcza bramki i recenzję
  na bieżącym drzewie.
- Fixer, który przywraca wcześniejszy stan drzewa (oscylacja między wersjami), kończy pętlę jako `stalled`.

## Konfiguracja (`rustflow.toml`)

```toml
[models]                       # allowlista - nic spoza niej się nie uruchomi
claude = ["haiku", "sonnet", "opus", "fable"]
codex  = ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-astra"]

[roles.implementer]            # trudność -> "provider:model:effort"
easy   = "claude:sonnet:medium"
medium = "claude:opus:high"
hard   = "claude:opus:xhigh"

[roles.reviewer_b]
lens = "regressions"           # correctness | regressions | design | audit
medium = "codex:gpt-6-astra:high"

[loop]
max_review_rounds = 3          # review -> fix -> review
max_gate_fix_attempts = 2
block_on = ["P0", "P1"]        # dodaj "P2" dla surowszego trybu
rereview = "both"              # albo "first" (taniej)
```

Pełny, skomentowany plik tworzy `rustflow init`. Najważniejsze przełączniki: `[triage].mode`
(`heuristic | llm | hybrid`), `[roles.planner].when` (`hard | always | never`),
`[roles.auditor].spec` (trzeci recenzent tylko przy unsafe/FFI), `[project].isolation`
(`branch | worktree | none`), `[budget].max_total_usd`, `[gates.extra]` (np. `sqlx prepare --check`),
`[providers.claude].setting_sources` (np. `"project,local"`: agenci bez Twoich globalnych hooków
i `~/.claude/CLAUDE.md`; zostają hooki i CLAUDE.md projektu).

Soczewka `regressions` (domyślna dla reviewer_b) szuka tego, co zmiana psuje istniejącym wywołaniom,
przypadków brzegowych i testów, które przeszłyby i bez zmiany; idiomy tylko jako P3. Wcześniejsza
`design` jest nadal dostępna.

Wywołania agentów: błąd trwały (logowanie, nieznany model, flaga, której nie zna zainstalowana wersja
CLI) nie jest ponawiany; awaria przejściowa i niepoprawny JSON - raz. Raport zapisuje model, który
faktycznie pracował (`modelUsage` Claude Code), i ostrzega, gdy różni się od żądanego.

## Co jest instalowane w repo

| Ścieżka | Co robi |
|---|---|
| `rustflow.toml` | konfiguracja |
| `.rustflow/gates.py` | bramki: fmt, clippy `-D warnings`, nextest/test, doctesty, cargo-deny, extras; profile `edit/stop/full`, cache po hashu drzewa |
| `.rustflow/hooks.py` | hooki Claude Code: `post-edit` (fmt + check → błędy do modelu), `stop` (bramki, max 3 blokady), `guard-bash` (read-only allowlista dla recenzentów, zakaz zmian gita dla implementera/fixera, bramka na `git commit` w sesji interaktywnej) |
| `.claude/settings.json` | rejestracja hooków (exec-form: `python3` / `py -3`, bez basha), plugin `rust-analyzer-lsp`, uprawnienia |
| `.claude/skills/`, `.agents/skills/` | `rustflow-plan`, `rustflow-implement`, `rustflow-review`, `rustflow-fix` (Claude Code i Codex) |
| `REVIEW.md` | macierz P0–P3 i reguły projektu (dostosuj) |
| `AGENTS.md`, `CLAUDE.md` | zarządzany blok z regułami (CLAUDE.md importuje AGENTS.md i REVIEW.md) |
| `.github/workflows/rustflow-gates.yml` | CI: `python .rustflow/gates.py ci` na Ubuntu i Windows |

Skills są wywoływane natywnie: Claude dostaje `/rustflow-review` na początku promptu (sprawdzone:
Claude Code rozwija wtedy SKILL.md), Codex dostaje `$rustflow-review`, a do tego ścieżkę do pliku.
`skill_invocation = "inline"` wkleja treść skilla wprost do promptu.

## Mapowanie na Twoje zestawy

- **rust-feedback-loop** → `scripts/check.sh` → `gates.py` (jeden skrypt dla hooków, orkiestratora i CI),
  `rust-on-edit`/`rust-on-stop` → `hooks.py post-edit`/`stop`, `guard-sqlx` → `guard-bash` (drop/reset zawsze,
  `migrate revert` tylko na lokalnej bazie), `sqlx prepare --check` → wbudowana bramka `sqlx` (wykrywa `query!`,
  czyta `.env`, `REQUIRE_SQLX_CHECK=1` w CI, podpowiedź naprawy), `[workspace.lints]` + `clippy.toml` →
  `rustflow lints` (zapadka z pomiarem), `.config/nextest.toml` → szablon ze `slow-timeout`, `CLAUDE.md` +
  wzorzec testów handlerów + pułapki → skill `rustflow-stack-axum-sqlx`, CI z Postgresem → `--stack axum-sqlx`,
  tabela sprawdzonych scenariuszy → `rustflow doctor --selftest`.
- **rust-review-kit** → przeniesiony w całości: `/rust-pr-review` → `/rustflow-pr-review` + subagent
  `rustflow-reviewer`, `context.sh` → `hooks.py review-context`, `check-review-report.sh` → `hooks.py check-report`,
  `review-router.sh` → `hooks.py route-review`, `reviewer-bash-guard.sh` → `guard-bash --role reviewer`,
  `rust-gate.sh` → `gates.py` + hooki, eskalacja ToB i pluginy → `--with-plugins` / `auditor.mode = "tob"`,
  `/code-review --comment` → `rustflow comment`, semver-checks → bramka `semver`. Wersja kitu z łatką może działać obok
  (z łatką zgodności - zob. [docs/DESIGN.md](docs/DESIGN.md#8-współistnienie-z-rust-review-kit)).

## Skille: które i jak są ładowane

| Faza | Skill roli (zawsze) |
|---|---|
| plan | `rustflow-plan` |
| implement | `rustflow-implement` |
| gate-fix, fix | `rustflow-fix` |
| review ×2, audytor | `rustflow-review` |

Dodatkowo **zawsze aktywne** skille z `[skills]` w `rustflow.toml` (domyślnie `all = ["auto"]`: każdy
zainstalowany `rustflow-stack-*` oraz Apollo `rust-best-practices`) trafiają do każdego agenta danej roli
deterministycznie, a nie wtedy, gdy model uzna, że opis pasuje:
Claude przez `--append-system-prompt-file` (stały plik na rolę, więc trafia do prompt cache, bez limitu
długości linii poleceń na Windows), Codex na początku promptu, interaktywny `rustflow-reviewer` przez preload
`skills:` (ustawiany przy `rustflow init`). Dołączany jest SKILL.md; pliki, do których odsyła (np. rozdziały
Apollo), agent czyta na żądanie. Jawnie wymieniony, a nieobecny skill zatrzymuje run na starcie; `doctor`
pokazuje listę dla każdej roli.

```toml
[skills]
all = ["auto"]
reviewer = ["rust-best-practices"]   # przykład: Apollo dla recenzentów nawet bez "auto"
max_kb = 32
```

## Zapadka lintów

Każda reguła wymuszona przez kompilator to jedna uwaga mniej do wyłapania przez recenzenta-LLM - za darmo,
po każdej edycji. `rustflow lints` mierzy jednym przebiegiem clippy kandydatów (m.in. `unwrap_used`,
`await_holding_lock`, `undocumented_unsafe_blocks`, `unsafe_code`, `dbg_macro`, `todo`) z wyłączeniem kodu testów
i pokazuje liczbę naruszeń. `--apply` dopisuje do `[workspace.lints]` (i `[lints] workspace = true` w crate'ach)
tylko te, które kod już spełnia, więc bramki nie robią się czerwone. Resztę włączysz po sprzątnięciu.

## Windows

- Cały tooling jest w Pythonie; hooki są w exec-form (`py -3 .rustflow/hooks.py …`), więc Git Bash nie jest potrzebny.
  Inny interpreter: `rustflow init --python-cmd "python"`. Zespół mieszany: `rustflow init --local`
  (hooki w `settings.local.json`).
- Shimy npm `.cmd` (`claude.cmd`, `codex.cmd`) są rozpakowywane do `node <script.js>`, żeby argumenty
  (schemat JSON) nie przechodziły przez `cmd.exe`. Prompt zawsze idzie przez stdin (limit długości linii poleceń).
- Timeout ubija całe drzewo procesów (`taskkill /T`); Ctrl+C zapisuje stan do `resume`.
- Sandbox Codex na Windows jest słabszy; dla Codex jako implementera rozważ WSL.
- Po instalacji uruchom `rustflow doctor --selftest`: tworzy tymczasowy crate i wywołuje każdy hook dokładnie
  tak jak Claude Code (exec-form, interpreter z `settings.json`, JSON w UTF-8). Wykrywa m.in. brakujący
  interpreter albo shim `.cmd`, przez które Claude Code po cichu nie uruchomiłby żadnego hooka.
- „Blocking waiting for file lock”: rust-analyzer i hooki dzielą `target/` - ustaw w edytorze
  `"rust-analyzer.cargo.targetDir": true`.

## Sterowanie przez agenta (Claude Code jako dyspozytor)

`rustflow init --with-mcp` rejestruje serwer MCP (`.mcp.json`) i skill `rustflow`, który uczy agenta: kiedy
delegować, jak potwierdzić koszt, wystartować w tle, czekać i działać według `next_actions`.

```bash
rustflow triage "<zadanie>" --json          # trudność, routing modeli, koszt z historii runów w tym repo
rustflow run --detach -f task.md            # od razu JSON z run_id (też: review --detach, resume --detach)
rustflow wait <run_id> --timeout 540        # poniżej limitu 10 min narzędzia Bash; exit 75 = nadal trwa
rustflow status [--json]                    # zwarte podsumowanie + next_actions (zamiast czytać report.md)
rustflow report [--json|--full]
rustflow cancel <run_id>                    # łagodne zatrzymanie, run zostaje wznawialny
```

Narzędzia MCP: `rustflow_triage`, `rustflow_start`, `rustflow_wait`, `rustflow_status`, `rustflow_report`,
`rustflow_cancel`, `rustflow_resume`, `rustflow_gates` (w Claude Code: `mcp__rustflow__…`).

Bezpieczniki: jeden aktywny run na repo (`ACTIVE.json` + sprawdzanie PID); dopóki run pracuje w checkoucie,
hooki odmawiają agentowi sterującemu edycji i zmian w gicie, a jego hook Stop nie odpala bramek na
niedokończonej pracy; agenci runu nie mogą uruchomić rustflow rekurencyjnie (`--strict-mcp-config` + guard).

## Interaktywna recenzja w Claude Code (z rust-review-kit)

W sesji Claude Code: `/rustflow-pr-review [base]` (albo po prostu „zrób review” - router hook kieruje do skilla).

- Forkowany subagent `rustflow-reviewer`: bez Edit/Write, guard z allowlistą tylko do odczytu, sam uruchamia
  bramki w trybie `--check` (nigdy nie zmienia drzewa), REVIEW.md P0–P3, raport markdown walidowany hookiem
  SubagentStop (APPROVE zabronione przy FAIL/P0/P1, max 2 poprawki).
- Kontekst (baza, zmienione pliki, sygnały unsafe/FFI, czy bramki są już zielone dla tego drzewa) wstrzykuje
  `hooks.py review-context` - Python, więc działa też na Windows bez Git Bash.
- `ESCALATE` → gotowa komenda audytu Trail of Bits (`rustflow init --with-plugins` włącza plugin).
  W trybie automatycznym: `[roles.auditor] mode = "tob"` uruchamia ten audyt raz na run i dołącza REPORT.md
  do raportu (drogie: 13–23 workerów ToB, domyślnie wyłączone).
- `rustflow comment [--pr N]` publikuje raport runu jako komentarz w PR (przez `gh`).
- Nowa bramka `semver`: `cargo semver-checks` dla publikowalnych crate'ów lib, względem bazy runu.

Uwaga: hooki z definicji subagenta Claude Code uruchamia tylko w **zaufanym** folderze (raz zaakceptowany
dialog zaufania). W folderze niezaufanym, np. `claude -p` w CI, recenzent dalej nie ma Edit/Write, ale jego
komendy Bash wymagają zatwierdzenia (w `-p` są odrzucane), a raport nie jest walidowany. To samo dotyczyło
guarda w review-kit.

## Współpraca z rtk i podobnymi optymalizatorami

Działa: `rtk_compat = true` (domyślnie) dodaje reguły `Bash(rtk …)`, a `guard-bash` rozumie prefiks `rtk`.
Szczegóły i pomiary: [docs/DESIGN.md](docs/DESIGN.md#7-optymalizatory-wyjścia-rtk).

## Testy

```bash
pip install -e ".[test]" && pytest      # 157 testów: jednostkowe, hooki jako procesy, e2e na prawdziwym cargo
```

E2E używają atrap `claude`/`codex` (`tests/fake_agents/`), ale prawdziwego `git` i `cargo`.
Dodatkowo zweryfikowane na żywo z Claude Code 2.1.310 (haiku): dwa pełne `run` (implement → hooki → review → approve),
`review` (raport) i `review --fix` (4 blokujące → fix z resolutions per ID → re-review przyrostowa → approve).
Kod przeszedł też niezależny przegląd (19 uwag, m.in. Windows: kodowanie stdin hooków, ubijanie drzew procesów,
Ctrl+C przy równoległych recenzentach) - wszystkie naniesione i pokryte testami regresyjnymi.
Codex 0.164: argv zaakceptowany (`thread.started`), wywołanie modelu niezweryfikowane (brak sieci do OpenAI
w środowisku testowym). Windows: zaprojektowane i obsłużone w kodzie, ale nie uruchomione - workflow CI
`.github/workflows/test.yml` odpala testy na `windows-latest`.
