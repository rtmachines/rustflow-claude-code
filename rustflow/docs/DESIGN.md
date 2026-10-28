# rustflow: projekt workflow i recenzja proponowanego cyklu

> Historical design notes and earlier live experiments (Polish). For supported behavior in the current checkout, use the [English documentation index](README.md), [usage](usage.md), and [architecture](architecture.md). Commands and external-tool observations below may describe an earlier implementation.

## 1. Recenzja proponowanego cyklu

Propozycja: **Implementer → 2 recenzentów → Fixer → (powrót do 2, N razy) → sprawdzenie kompilacji.**

Elementy trafne i zachowane: rozdzielenie ról, dwóch niezależnych recenzentów, limit iteracji
w konfiguracji, wybór modelu i effortu według trudności, allowlista modeli (Claude Code + Codex), skills.

Problemy:

| # | Problem | Skutek | Poprawka w rustflow |
|---|---|---|---|
| 1 | Kompilacja dopiero na końcu | Dwóch drogich recenzentów czyta kod, który się nie kompiluje; fixer może zepsuć build i nikt tego nie wie aż do końca | Bramki deterministyczne (fmt, clippy `-D warnings`, testy, doctesty) **po każdym kroku zmieniającym kod i zawsze przed recenzją**. Czerwone → fixer naprawia build (limit prób) → nadal czerwone = stop |
| 2 | Brak kroku "czy baza jest zielona" | Agent dostaje winę za cudze błędy, pętla naprawia nie to, co trzeba | Preflight: bramki na bazie; czerwona baza = stop albo `--accept-red-baseline` (blokują tylko NOWE błędy) |
| 3 | Brak specyfikacji dla recenzentów | Recenzent ocenia "czy ładne", a nie "czy spełnia zadanie" | Planner dla zadań hard: kroki + **kryteria akceptacji**; recenzenci zawsze zwracają status każdego wymagania (`met/not_met/unclear`); `not_met` = blokujący finding (P1; gdy recenzent wskaże swój finding polem `finding`, ten finding zostaje podniesiony do P1 zamiast dublowania go bez kotwicy) |
| 4 | "Dwóch niezależnych" bez mechanizmu niezależności | Dwa takie same modele z tym samym promptem dają skorelowane błędy | Różne rodziny modeli (Claude + Codex) i różne soczewki (poprawność/soundness vs design/idiomy); równolegle, bez wglądu w siebie nawzajem |
| 5 | Nie wiadomo, kto rozstrzyga między recenzentami | Fixer dostaje sprzeczne, zduplikowane lub zmyślone uwagi | **Deterministyczna agregacja** (bez LLM): walidacja `plik:linia` (nieistniejący plik = odrzucony), deduplikacja, potwierdzenie przez 2 recenzentów, polityka P0–P3, próg pewności |
| 6 | Fixer musi przyjąć każdą uwagę | Fałszywe alarmy psują kod | Fixer zwraca status per finding: `fixed/partial/rejected` z dowodem; recenzent w kolejnej rundzie akceptuje odrzucenie albo je podtrzymuje; 2 razy spór = decyzja człowieka |
| 7 | Powrót do pełnej recenzji w każdej rundzie | Koszt rośnie liniowo, recenzent gubi kontekst poprawek | Re-recenzja **przyrostowa**: delta od ostatniej recenzji + lista poprzednich findingów z odpowiedziami fixera; domyślnie `rereview = "auto"`: w rundzie >= 2 recenzuje tylko autor otwartych findingów, jeśli poprawka nie wyszła poza ich pliki (zmierzone: recenzent bez findingów w r2 to 18% kosztu runu); opcja `rereview = "first"` (jeden recenzent) |
| 8 | Tylko limit rund jako warunek stopu | Pętla kręci się bez postępu do limitu | Wykrywanie zastoju (te same blokujące ID po fixie albo fixer nic nie zmienił), eskalacja effortu fixera przy powtórce, budżet $ na cały run |
| 9 | Brak izolacji i punktów kontrolnych | Trudno cofnąć, trudno przejrzeć zmiany | Gałąź `rustflow/<run>` (albo worktree), commit-checkpoint po każdej fazie, scalenie przez człowieka (`merge --squash`) |
| 10 | Brak ścieżki dla `unsafe`/FFI | Zwykły recenzent nie zrobi audytu soundness | Sygnały ryzyka w dodanych liniach → recenzenci na poziomie hard, opcjonalny trzeci recenzent-audytor, flaga **ESCALATE** w raporcie (np. `/rust-review:rust-review` Trail of Bits) |
| 11 | Kompilacja ≠ jakość | Kod się kompiluje, ale clippy/testy/doctesty/licencje są czerwone | Final = pełne bramki w trybie CI (`fmt --check`, `--locked`, doctesty, cargo-deny przy zmianie manifestów) - te same co w GitHub Actions |

## 2. Docelowy cykl

```
0 preflight   git repo? czyste drzewo? gałąź/worktree; bramki na bazie (czy baza zielona)
1 triage      heurystyka (PL+EN słowa kluczowe, rozmiar, workspace) | llm | hybrid -> easy/medium/hard
              -> routing ról z rustflow.toml (provider:model:effort, allowlista)
2 plan        tylko hard (konfigurowalne): kroki + kryteria akceptacji           [read-only, skill rustflow-plan]
3 implement   skill rustflow-implement; wewnętrzna pętla: hook po edycji (fmt+check), hook Stop (clippy+testy)
4 gates       autorytatywne, Python; RED -> fixer (gate-fix, max N, eskalacja) -> RED = STOP (bez recenzji)
5 review x2   równolegle, różne modele i soczewki, read-only, JSON wg schematu
              wejście: diff, wynik bramek, clippy pedantic na zmienionych liniach, sygnały unsafe, kryteria
6 aggregate   walidacja kotwic, dedupe, potwierdzenia, spory, polityka block_on -> decyzja
              brak blokujących -> 8 | limit rund / zastój -> 8 | inaczej -> 7
7 fix         skill rustflow-fix; resolutions per ID; checkpoint -> 4 -> 5 (przyrostowo)
8 final       bramki w trybie CI, raport.md, ESCALATE?, gałąź gotowa do merge przez człowieka
```

Dlaczego tak:
- **Najtańszy sygnał najpierw.** Kompilator i testy kosztują sekundy i są obiektywne; LLM-recenzja jest droga i probabilistyczna.
- **Decyzje deterministyczne.** O tym, co blokuje, decyduje kod orkiestratora (polityka P0–P3), a nie kolejny model - powtarzalne i audytowalne.
- **Każdy model robi to, w czym jest dobry.** Planner myśli, implementer pisze, recenzenci szukają defektów, fixer naprawia punktowo; agregacji nie robi żaden model.
- **Jedno źródło prawdy dla bramek.** `.rustflow/gates.py` używają hooki Claude Code, orkiestrator i CI.

## 3. Wybór modelu i effortu

`rustflow.toml` → `[roles.<rola>]` ma `easy/medium/hard = "provider:model:effort"`. Orkiestrator:
1. ustala trudność (CLI `--difficulty` > triage),
2. waliduje spec względem `[models]` i `[efforts]` (allowlista),
3. podnosi poziom, gdy: diff ma sygnały unsafe/FFI/atomics (recenzenci → hard), fixer naprawia
   finding z poprzedniej rundy (`escalate_on_repeat`), druga próba gate-fix.

Domyślnie: reviewer_a = Claude (soczewka poprawności), reviewer_b = Codex (soczewka designu).
`rustflow doctor` sprawdza modele i efforty Codex z jego katalogiem (`codex debug models`).

## 4. Odpowiedzialności plików

| Plik | Rola |
|---|---|
| `rustflow/orchestrator.py` | maszyna stanów, zapis `state.json` po każdej fazie (resume) |
| `rustflow/agents.py` | `claude -p` / `codex exec`, prompt przez stdin, schematy, koszty, retry |
| `rustflow/findings.py` | agregacja recenzji (deterministyczna) |
| `rustflow/criteria.py` | kryteria akceptacji, ocena wymaganych testów z wyniku bramek (bez LLM) |
| `rustflow/gates.py` | bramki (standalone; kopiowane do `.rustflow/gates.py`) |
| `rustflow/hooks.py` | hooki Claude Code (standalone; `.rustflow/hooks.py`) |
| `rustflow/integrity.py` | scan zmian wpływających na bramki, dowód, zgody (standalone; kopiowane do `.rustflow/integrity.py`) |
| `rustflow/triage.py` | heurystyka trudności + schemat triage LLM |
| `rustflow/prompts.py` | prompty ról |
| `rustflow/templates/` | rustflow.toml, skills, REVIEW.md, bloki AGENTS/CLAUDE.md, CI |

## 5. Świadome ograniczenia

- `--max-budget-usd` w Claude Code to limit miękki (sprawdzany po turze); globalny budżet rustflow liczy realny koszt z wyników.
- Koszt Codex nie jest raportowany w $ (tylko tokeny).
- Sandbox Codex na Windows jest słabszy niż na Linux/macOS; dla Codex-implementera na Windows rozważ WSL.
- Izolacja `worktree` wymaga, by `.rustflow/`, `.claude/`, `.agents/` były zacommitowane (worktree powstaje z HEAD).
- `cargo` w bramkach wykonuje `build.rs` i makra proc zależności - niezaufane repo uruchamiaj w kontenerze.
- Tryb interaktywny (bez `rustflow run`) gwarantuje tylko zielone bramki dla tego, co trafia do HEAD/merge; pełny cykl (triage, plan, dwie recenzje, fix, bramki final) jest opcjonalny.
- Dowód sprawdza tylko bieżące drzewo: zaakceptowana zmiana konfiguracji może osłabić bramki dla przyszłych zmian po merge'u (widoczne w raporcie).
- Zgoda recenzentów dla `cargo-lints` i `tests-removed` to osąd dwóch modeli; skorelowany błąd albo prompt injection w diffie może dać fałszywą zgodę (ślad w raporcie, status `approved_with_advisories`).
- W trybie interaktywnym zgodą dla zmian bez dowodu jest commit człowieka poza Claude Code; bramka commita rozpoznaje tylko komendy pasujące do wzorca `git commit` i ocenia drzewo robocze, nie indeks.
- `cargo-lints` bez parsera TOML (Python < 3.11 bez `tomli`) działa zachowawczo i może blokować zmiany neutralne.
- Cache wyników bramek i dowodu w `.rustflow/cache/` nie jest chroniony przed zapisem przez agenta.
- Reguły `--disallowedTools` dla ról piszących nie obejmują zapisu przez Bash; o blokadzie decyduje detekcja w diffie.
- Przy `--accept-red-baseline` dowód nie uwzględnia czerwonej bazy, więc zmiany dowodliwe kończą się porażką dowodu (blokują zachowawczo).
- Wykrywanie w diffie to stały zestaw wzorców, nie analiza semantyczna. Znane luki poza specyfikacją: test zakomentowany blokowo (`/* #[test] ... */`), `#[should_panic]` dodane do padającego testu, `#[cfg(any())]` / `#[cfg(all(test, any()))]` na module testów, `#[cfg_attr(x, ignore)]`, `# [allow(..)]` i atrybut rozbity na wiele linii (rustfmt zwykle to normalizuje), `[lib] test = false` / `autotests = false` w `Cargo.toml` oraz osłabienie ciała testu (`assert!(true)`).
- Hook Stop i bramka commita (bramki + dowód) mają limit czasu 1800 s; po jego przekroczeniu Claude Code traktuje hook jako niekrytyczny błąd (fail-open) i nic nie trafia do cache.
- Pliki konfiguracji ukryte w `.gitignore` (np. `.cargo/config.toml`) są traktowane jak zmiany `config`: w trybie interaktywnym każdy obecny, w orkiestratorze nowy lub zmieniony względem stanu z preflightu. Dowód uruchamia bramki w worktree bez nich, więc projekt, który faktycznie wymaga takiego pliku do budowy, zostanie zablokowany.
- Wygenerowane CI uruchamia `gates.py` na roboczym `rustflow.toml` PR-a i bez skanu integralności, więc PR, który osłabia `[gates]`, osłabia także własne bramki CI.

## 6. Prompt caching

Cache Claude działa na prefiksie: narzędzia → system prompt → wiadomości, osobno dla każdego modelu.
Pomiary z runów na żywo (Claude Code 2.1.310, haiku):

| Wywołanie | zapis do cache | odczyt z cache |
|---|---|---|
| implementer (jedna sesja, ~10 tur) | 41k | **290k** |
| recenzent, runda 1 (zimny start roli) | ~17k | ~18k |
| recenzent, runda 2 (ta sama rola, inny prompt) | ~22k | **~58k** |
| 2 kolejne identyczne wywołania z commitem pomiędzy | 172 | **62k** |

Wnioski:
- **Najwięcej tokenów to tury wewnątrz jednej sesji agenta**, a tam cache działa normalnie. Hooki
  (feedback z `cargo check`, blokada Stop) tylko dopisują wiadomości na końcu, nie zmieniają prefiksu.
- **Między rolami o innym modelu lub zestawie narzędzi cache nie jest współdzielony** (cache jest per
  model; recenzent nie ma Edit/Write). Implementer i fixer mają jednak ten sam model i te same narzędzia,
  więc system prompt jest **identyczny dla wszystkich ról** (`SHARED_SYSTEM` + skille always-on), a linia
  roli trafia do promptu za komendą skilla. Zmierzone na żywo przed tą zmianą: fixer startujący 3,5 min po
  implementerze zapisał do cache cały prefiks od nowa (~35k tokenów, cache_read = 0), bo różniła się
  tylko linia roli; zapis cache to ~54% kosztu Claude w runie.
- **Ta sama rola w kolejnej rundzie korzysta z cache** (prefiks narzędzia + system + skill; zmierzone:
  24k tokenów odczytu w pierwszej turze recenzenta r2), bo wszystko zmienne (rola, diff, runda, findingi)
  jest w wiadomości użytkownika.
- `--exclude-dynamic-system-prompt-sections` (domyślnie włączone) przenosi cwd/git status do pierwszej
  wiadomości: nazwa gałęzi `rustflow/<run>` i ścieżka worktree są unikalne per run i inaczej
  wymuszałyby zimny start na początku każdego runu.

Świadome kompromisy: fixer startuje świeżą sesją zamiast `--resume` implementera (świeże oczy, mniejszy
kontekst; po kilkuminutowej recenzji cache implementera i tak może wygasnąć), eskalacja modelu fixera
oznacza zimny cache, a dwaj recenzenci Claude uruchomieni równocześnie nie skorzystają nawzajem ze
swojego zapisu (domyślnie reviewer_b to Codex, więc nie dotyczy).

## 7. Optymalizatory wyjścia (rtk)

rtk instaluje globalny hook PreToolUse(Bash), który przepisuje komendy (`git status` → `rtk git status`,
`cargo test` → `rtk cargo test`). Reguły uprawnień czyta tylko z plików `settings.json`; dla komendy bez
pasującej reguły zwraca `updatedInput` **bez** decyzji, a Claude Code sprawdza wtedy uprawnienia na
komendzie przepisanej.

Zmierzone na żywo (Claude Code 2.1.310, hook o identycznym kształcie odpowiedzi jak rtk):

| | `git status` | `git log` | `cargo build` | `cargo test` |
|---|---|---|---|---|
| implementer, przed poprawką | odrzucone | odrzucone | odrzucone | OK (reguła w settings.json) |
| implementer, z `rtk_compat` | OK | OK | OK | OK |
| recenzent (guard daje jawne allow) | OK | OK | odrzucone (celowo) | OK |

Co robi rustflow:
- `[providers.claude].rtk_compat = true`: każda reguła `Bash(X)` w `--allowedTools` dostaje bliźniaka
  `Bash(rtk X)` (bez rtk nic nie zmienia).
- `guard-bash` zdejmuje prefiks `rtk` / `rtk proxy` przed oceną (`rtk read` = `cat`), więc agent, który
  sam napisze `rtk git commit`, dalej jest blokowany (wcześniej wyrażenie zaczepione na `git` by to przepuściło).

Czego rtk nie dotyka: bramek (`gates.py` i hooki uruchamiają cargo przez subprocess, nie przez narzędzie
Bash agenta; `python3 .rustflow/gates.py` nie jest na liście przepisywanych komend), feedbacku bramek
w promptach i hooka Stop. Instrukcje rtk w globalnym CLAUDE.md są stałe, więc nie psują prompt cache.

Codex: rtk nie ma tam hooka, tylko instrukcje w `~/.codex/AGENTS.md`. W sandboxie `read-only`
rtk nie zapisze bazy statystyk ani plików tee (źródło sugeruje, że błąd zapisu statystyk jest
ignorowany; nie zweryfikowano uruchomieniem). Recenzenci i tak dostają diff i wynik bramek w prompcie.

## 8. Współistnienie z rust-review-kit

Zmierzone na repo z obydwoma zestawami (Claude Code 2.1.310, haiku):
- `review-router.sh` (UserPromptSubmit) doklejał recenzentom rustflow regułę „reviews MUST go through
  rust-pr-review”. Haiku ją zignorował, ale to sprzeczna instrukcja - silniejszy model mógł odpalić
  drugą, forkowaną recenzję.
- `rust-gate.sh stop` uruchamiał pełne bramki w sesjach recenzentów (w trybie `review` zmiana jest
  niezacommitowana), czyli przy czerwonych bramkach mógł 3 razy zablokować agenta, który nie może nic poprawić.
- `reviewer-bash-guard.sh` odrzucał `rtk git diff`; `rtk git commit` omijał bramkę commita kitu.
- Hooki kitu w exec-form (`command: ….sh`) nie startują na Windows.

Poprawki: rustflow ustawia agentom `RUST_GATE_SKIP=1`, `doctor` ostrzega o drugiej bramce Stop
i niezałatanym routerze; łatka kitu (router, guard, settings, CLAUDE.md) - po niej router nie dokleja
nic agentom rustflow, a bramka kitu nie rusza w ich sesjach (zweryfikowane na żywo).
Interaktywne `/rust-pr-review` i eskalacja do Trail of Bits działają jak wcześniej.

## 9. Najlepsze elementy rust-review-kit w rustflow

| Kit | rustflow | Co się zmieniło |
|---|---|---|
| `/rust-pr-review` + `rust-reviewer` | `/rustflow-pr-review` + `rustflow-reviewer` | bramki przez `gates.py --check` (deterministyczne, te same co CI, zero zmian w drzewie) zamiast 4 ręcznych komend; preload `rustflow-review` (+ rust-skills; także Apollo, jeśli zainstalowany) |
| `context.sh` (bash) | `hooks.py review-context` | Python (Windows), diff obejmuje też nieśledzone pliki, informacja o zielonych bramkach z cache, jawny komunikat gdy bazy nie ma |
| `check-review-report.sh` | `hooks.py check-report` | ten sam kontrakt + linie doctests/semver; testy jednostkowe |
| `review-router.sh` | `hooks.py route-review` | pomija agentów orkiestratora, bez fałszywych trafień na samo słowo "pr" |
| `reviewer-bash-guard.sh` | `guard-bash --role reviewer` | rozumie `rtk`, `git -C`, pozwala na `gates.py ... --check` |
| eskalacja do ToB (ręczna) | komenda w raporcie + opcjonalny `auditor.mode = "tob"` | audyt raz na run, zakres = katalog plików z sygnałami, kontrola że audyt nie zmienił kodu |
| `enabledPlugins` / marketplace | `rustflow init --with-plugins` | opcjonalne |
| `/code-review --comment` | `rustflow comment` | przez `gh`, bez dodatkowego pluginu |
| semver-checks (w promptach) | bramka `semver` | deterministyczna, `--baseline-rev` = baza runu |

Zweryfikowane na żywo (Claude Code 2.1.310, haiku, ~$0.003 za recenzję): `!`-kontekst się wstrzykuje, forkowany
recenzent uruchamia `gates.py --check`, guard odrzuca `git -C … diff` (agent przechodzi na `git diff`), raport
spełnia kontrakt, `unsafe` w diffie daje ESCALATE z komendą ToB.

Ograniczenie platformy (dotyczyło też kitu): hooki z frontmattera subagenta działają tylko w zaufanym folderze
(log: "Skipping frontmatter hooks ... folder ... is not trusted"). Bez zaufania recenzent nadal nie ma Edit/Write,
ale Bash wymaga zatwierdzenia, a raport nie jest walidowany.
Audyt ToB w orkiestratorze jest zaimplementowany i przetestowany atrapą pluginu; z prawdziwym pluginem nie był
uruchamiany (koszt, a w tym środowisku zablokowane wykonywanie zewnętrznego kodu).

## 10. Co wzięliśmy z rust-feedback-loop

| feedback-loop | rustflow | Dlaczego tak |
|---|---|---|
| `scripts/check.sh` | `gates.py` | ta sama zasada (jedno źródło prawdy dla hooka i CI), wieloplatformowo |
| `[workspace.lints]` (ręcznie) | `rustflow lints` - zapadka | reguła w kompilatorze kosztuje zero tokenów; pomiar chroni przed nagle czerwonymi bramkami |
| `sqlx prepare --check` w check.sh | bramka `sqlx` (auto / require / off) | wykrywanie `query!`, `.env`, tryb wymagany w CI, podpowiedź naprawy |
| `guard-sqlx.sh` | `guard-bash` | `migrate revert` tylko na lokalnej bazie (env, `--database-url`, `-D`, `.env`) |
| `.config/nextest.toml` | szablon ze `slow-timeout` | zawieszony test nie trzyma bramki do godzinnego limitu |
| `CLAUDE.md`, `tests/handlers.rs`, pułapki z README | skill `rustflow-stack-axum-sqlx` (`--stack`) | wiedza o stacku ładowana tylko gdy potrzebna, dla Claude i Codex |
| CI z Postgresem | CI stacku | `query!` i `#[sqlx::test]` potrzebują bazy; `REQUIRE_SQLX_CHECK=1` |
| tabela „sprawdzone scenariusze” | `rustflow doctor --selftest` | ręczna weryfikacja → automatyczna, 14 scenariuszy, także na Windows |

Nie przeniesione: aplikacja demo, `rustfmt.toml` (decyzja projektu), `retries = 1` w profilu CI nextest
(ponawianie ukrywa niestabilne testy przed recenzją).

## 11. rustflow jako narzędzie agenta (Claude Code sterujący)

| Problem agenta sterującego | Rozwiązanie |
|---|---|
| Bash ma limit 2-10 min, run trwa 20-90 min | `--detach` + `wait --timeout 540` (exit 75 = nadal trwa) + `cancel`/`resume --detach` |
| Hook Stop agenta odpala bramki na pracy implementera (izolacja `branch`) i blokuje mu turę | `ACTIVE.json` (run_id, PID, workdir); Stop pomija bramki dla checkoutu runu |
| Agent "pomaga" edytując pliki w trakcie runu | `guard-edit` (PreToolUse Edit/Write) + zakaz zmian gita w checkoucie runu |
| Dwa runy w jednym repo | blokada z wykrywaniem martwych procesów (POSIX `kill 0`, Windows `OpenProcess`) |
| `report.md` to tysiące tokenów | `status`/`report` brief (~20-30 linii) i `--json` |
| Brak protokołu decyzji | `next_actions` (`wait`, `ask_user` z gotowym pytaniem, `resume`, `merge` z `needs_user`, `offer_audit`) |
| Parsowanie Basha, prośby o zgody | serwer MCP (stdio, stdlib), `mcp__rustflow` i `Skill(rustflow-*)` w `permissions.allow` |
| Koszt nieznany przed startem | `triage --estimate` z historii runów w repo (uczciwie: brak historii = "nieznany") |
| Rekurencja: agenci runu widzą `.mcp.json` i skill `rustflow` | `--strict-mcp-config` dla agentów + guard odrzuca `rustflow run/review/resume` |

Zweryfikowane na żywo (Claude Code 2.1.310, haiku jako dyspozytor i recenzenci):
- pierwsza próba ujawniła 3 problemy: router i CLAUDE.md przechwytywały "użyj rustflow" na `/rustflow-pr-review`;
  narzędzia MCP są odraczane (ToolSearch) a skill podawał skrócone nazwy; skill z `allowed-tools` wywołany przez
  model wymaga zgody ("Execute skill: …"), której w `-p` nie ma kto dać;
- po poprawkach: skill `rustflow` → ToolSearch (ładuje `mcp__rustflow__*`) → triage → start (review, w tle)
  → jedno `rustflow_wait` → brief → raport dla użytkownika zgodny z `next_actions`, bez uruchamiania fix bez zgody;
  0 odmów uprawnień, ~$0.025 łącznie. Wykryty przy tym błąd (flaga `--allow-dirty` w trybie review) naprawiony.

## 12. Skille zawsze aktywne

Skill wywoływany przez model ładuje się tylko wtedy, gdy model uzna, że opis pasuje do zadania, więc skill
stacku czy rust-skills może nie zostać przeczytany wcale. `[skills]` (domyślnie `all = ["auto"]`) dołącza je
deterministycznie do każdej roli: Claude przez `--append-system-prompt-file` (treść identyczna dla
wszystkich ról z tymi samymi skillami, więc zostaje w prompt cache), Codex na początku promptu, interaktywny recenzent przez preload `skills:`.
Brak jawnie wymienionego skilla to błąd w preflight, a `max_kb` chroni kontekst. Zweryfikowane na żywo:
fraza ukryta w skillu dołączonym przez `--append-system-prompt-file` wróciła w odpowiedzi modelu.

## 13. Co wzięliśmy z bb-rust-workflow

Porównanie z `rtmachines/bb-rust-workflow` (commit `221dbdc`, 26.10) wskazało kilka mechanizmów,
które wzmacniają tezę „decyduje kod, nie model”. Część była już w rustflow (wejście zadania z pliku
i stdin, macierz CI Ubuntu/Windows, hash drzewa, zabijanie drzewa procesów, sygnatury błędów bez numerów
linii, sprawdzanie flag CLI w `doctor`); wdrożone zostało to, czego brakowało:

| bb-rust-workflow | rustflow | Uwagi |
|---|---|---|
| kryterium mechaniczne = nazwany test, który musi przejść | `[[criteria]] test = "..."` (`--criteria`, planner) | `criteria.py`; zaliczone testy z libtest (`test x ... ok`) i nextest (`PASS [..] bin x`, teraz `--status-level pass`); 0 testów, `#[ignore]`, inny test = niespełnione; blokująca uwaga `T-<id>` co rundę, `criteria_unmet` na końcu |
| manifest drzewa przed/po kontroli i recenzji | krok `tree` w pełnych bramkach; `tree_changed` po recenzji | bez `--locked` wolno zmienić tylko `Cargo.lock`; `tree_changed` jest wznawialne (cofa do bramek) |
| `no_progress` przy powrocie do wcześniejszej migawki | `seen_trees` | oscylacja fixera/gate-fixa = `stalled` |
| `BackendError.retryable` | `AgentResult.retryable` | auth, nieznany model, nieznana flaga, HTTP 400/401/403/404 bez ponowienia |
| model zgłoszony vs żądany | `observed_model`, `model_mismatch` | z `modelUsage` (model z największym wyjściem); ostrzeżenie, nie błąd |
| duplikaty z pełnymi dowodami dla Fixera | `Finding.also` | kotwica idzie z wygrywającym opisem |
| fokus reviewer B: regresje, brzegi, brakujące testy | soczewka `regressions` (domyślna dla reviewer_b) | `design` nadal dostępna |
| `--setting-sources ""` | `[providers.claude].setting_sources` (opcja) | patrz niżej |

Zmierzone na żywo (Claude Code 2.1.310, haiku, fałszywy `HOME` z hookiem i CLAUDE.md użytkownika):

| | CLAUDE.md użytkownika | hook użytkownika | CLAUDE.md projektu | hook projektu |
|---|---|---|---|---|
| bez flagi | tak | tak | tak | tak |
| `--setting-sources project,local` | nie | nie | tak | tak |

`project,local` izoluje agentów od globalnych hooków (np. rtk) i osobistych instrukcji, a bramki rustflow
(hooki projektu) działają dalej. Domyślnie wyłączone: użytkownik logujący się przez `apiKeyHelper`/`env`
w `~/.claude/settings.json` straciłby uwierzytelnienie. `doctor` podpowiada opcję, gdy widzi hooki lub
`~/.claude/CLAUDE.md`. `rtk_compat` i `RUST_GATE_SKIP` zostają (działają bez izolacji i z nią).

Run na żywo (haiku, `--criteria` z `tests::trims_whitespace`, `setting_sources = "project,local"`):
implementer utworzył test o tej nazwie, orkiestrator potwierdził go po bramkach i w trybie final
(nextest: `demo tests::trims_whitespace`), recenzenci oznaczyli kryterium `[C1]`, `approved_with_advisories`,
$0.024. `doctor` sprawdza teraz tylko flagi widoczne w `--help` (`--append-system-prompt-file` działa,
ale jest ukryta).

Nie przeniesione: kopie katalogów zamiast gita, stały LLM-triage, obowiązkowy plik kryteriów, implementer
bez Basha, wersjonowanie konfiguracji.

## 14. Ergonomia dla Claude Code (zmierzone)

Pomiary na żywo (Claude Code 2.1.308, haiku, effort low; łącznie kilka centów):

| Pomiar | Wynik | Co z tego wynika |
|---|---|---|
| ten sam prompt z `--json-schema`, bez `--tools` / z `--tools "Read,Grep,Glob,Bash"` | 27 / 5 narzędzi w kontekście; zapis do cache 41k / 11k; $0.0085 / $0.0023 | `--allowedTools` tylko zatwierdza, `--tools` usuwa definicje z promptu; StructuredOutput zostaje |
| `/skill` w prompcie bez narzędzia Skill / z nim | oba wczytują skill; $0.0014 / $0.0038 | `tools_*` bez `Skill` |
| `--resume <sesja> --json-schema <schemat>` | `success`, poprawny `structured_output`, ta sama sesja | naprawa JSON-u jedną turą zamiast powtarzania sesji; ten sam schemat i argv, bo inny schemat = inny prefiks (odczyt z cache 0) |
| SIGTERM / SIGINT w trakcie generowania | 143 bez wyjścia / 0 z `error_during_execution`, ale `total_cost_usd = 0` | przy timeoucie koszt nie jest znany: rustflow księguje limit roli (`cost_estimated`) |
| wynik `-p --output-format json` | `permission_denials`, `num_turns`, `terminal_reason` | odmowy (cicho przy `--permission-prompts none`) trafiają do `state.json` i raportu |

Zmiany:
- **`[providers.claude].tools_read/tools_write`** (`--tools`); wywołania z własną listą (audyt ToB) mają
  pełny zestaw. Na Windows dochodzi PowerShell.
- **Naprawa structured output**: nieudany JSON z `session_id` -> jedna tura `--resume` z tym samym argv.
  Nie dotyczy kompromisu z §6 (fixer nie wznawia sesji implementera; wznawia własną, jeszcze ciepłą).
  Gate-fix nie ma już schematu: o wyniku decydują bramki.
- **`agent_failed`** (wznawialny): wywołanie modelu nie powiodło się i drzewo jest bez zmian - zamiast
  `no_changes` / `review_failed`. Role piszące mają jedno ponowienie, ale tylko przy niezmienionym drzewie.
  Opcjonalnie `fallback_model`.
- **`resume --extra-rounds N`** zamiast nowego `review --fix`: ta sama gałąź/worktree, checkpointy i ID
  uwag (nowy run w izolacji worktree pracowałby w złym checkoucie). `next_actions` dla worktree podaje merge.
- **Hook po edycji** formatuje tylko edytowany plik (`rustfmt` ze stdin nie schodzi do modułów `mod x;`);
  `cargo fmt --all` przepisywał cały niesformatowany workspace przy pierwszej edycji. Powtórzony zestaw
  błędów `cargo check` wraca jedną linią.
- **Feedback bramek w hookach** obcinany od końca (dawniej `[-8000:]` gubił nagłówek i pierwsze błędy).
- **Runda >= 2**: delta inline, pełny diff tylko do `rereview_full_diff_max_kb` (domyślnie 16 KB).
- `start_tree` po bramkach bazowych: `Cargo.lock` utworzony przez bramki nie jest pracą implementera.

### Repo, które nie jest sformatowane rustfmt (run na żywo)

Pierwszy run na prawdziwej bibliotece (~5k linii) skończył się `baseline_red`: 8 plików niesformatowanych.
Poprawka w hooku po edycji nie wystarczała:
- bramki bez `--check` (po implementacji, hook Stop) uruchamiały `cargo fmt --all` -> z `--accept-red-baseline`
  diff dostawał przeformatowanie całego repo;
- błąd fmt nie był rozpisany (sygnatura `fmt|*`), więc nigdy nie liczył się jako istniejący wcześniej;
- stop w preflight zostawiał checkout na pustej gałęzi `rustflow/<run>`.

Teraz: `fmt_fix_scope = "changed"` formatuje tylko pliki `.rs` zmienione względem `$RUSTFLOW_BASE` (interaktywnie:
HEAD) + nieśledzone; tryb `--check` daje jedną diagnostykę na plik (`fmt|<plik>|fmt|not rustfmt-formatted`), więc
czerwona baza z samym fmt jest akceptowalna; stop w preflight cofa gałąź/worktree; `next_actions` dla bazy
czerwonej tylko przez fmt podsuwa `cargo fmt --all` na bazie albo `--accept-red-baseline`.

Drugi run na żywo (ta sama biblioteka, niesformatowana baza, `--accept-red-baseline`): approved, 6 nietkniętych
plików bez zmian, ale dwa dotknięte pliki przeformatowane w całości - diff +712/-140 zamiast +240/-0
na sformatowanej bazie. Po poprawce: +205/-0, samo zadanie. Dlatego `changed` pomija też pliki, które w bazie nie były sformatowane (sprawdzane przez
sformatowanie wersji z bazy ze stdin i porównanie; `rustfmt --check` na stdin zawsze zwraca 0) - dotyczy bramek
i hooka po edycji. Zasada „formatuj to, czego dotykasz” zostaje jako `fmt_fix_scope = "touched"`.

### Komendy tylko do odczytu dla ról piszących (zmierzone)

W trzech runach na żywo implementer dostał tę samą odmowę: `rtk ls && rtk read Cargo.toml && …`. Test uprawnień
na argv implementera (Claude Code 2.1.308, haiku) pokazał, że przyczyną nie jest łączenie komend:

| komenda | przed | po (`readonly_shell = true`) |
|---|---|---|
| `cargo check && cargo test`, `cargo test 2>&1 \| tail -5` | dozwolone | dozwolone |
| `ls` (hook rtk -> `rtk ls`), `wc -l …`, `rtk read Cargo.toml`, `rtk ls && rtk wc …` | **odmowa** | dozwolone |
| `find . -name …` | odmowa | odmowa (celowo: `find -delete`; jest narzędzie Glob) |
| `rm -rf <katalog w repo>` | dozwolone | dozwolone - to `acceptEdits` Claude Code, nie allowlista |
| `rm -rf .git` | odmowa | odmowa (chroni sam Claude Code) |

Claude Code sprawdza każdą część łańcucha osobno, więc `&&` działa, gdy każda komenda ma regułę. Role piszące nie
miały żadnej komendy tylko do odczytu. Reguły dokłada kod (nie szablon), bo `allowed_tools_write` z `rustflow.toml`
istniejących projektów nie jest nadpisywane przez `init`; `rtk read` dostaje bliźniaka `cat`.

### Dyspozytor przez MCP (zmierzone)

Run na żywo z haiku jako dyspozytorem (Claude Code 2.1.308, `claude -p` w repo z `rustflow init --with-mcp`):
- **Model widzi `structuredContent`, nie tekst `content`.** Brief w `content` jest dla innych klientów MCP;
  wszystko, co dyspozytor musi przeczytać, musi być w części strukturalnej. Wcześniej `rustflow_report format=full`
  oddawał w `structuredContent` samo podsumowanie, więc raport nigdy nie docierał do modelu - teraz jest tam
  (`report`), z tym samym limitem rozmiaru.
- Kroki `next_actions` mają pole `mcp` (`{tool, arguments}`); `rustflow_wait` do 1800 s - dyspozytor czekał jednym
  wywołaniem (`timeout_s: 1800`) zamiast kilku.
- Odpowiedzi trzymają się poniżej 40k znaków (Claude Code ostrzega od 10k tokenów, tnie na 25k, a tekst powyżej
  50k znaków zapisuje do pliku): zwięzłe findingi w `format=json`, przycięcie z informacją, jak dostać resztę.
- W katalogu bez zaakceptowanego dialogu zaufania Claude Code pomija `permissions.allow` z `.claude/settings.json`
  (komunikat na stderr), więc `mcp__rustflow` jest odrzucane w `-p`. W prawdziwym repo wystarczy raz otworzyć
  Claude Code interaktywnie i zaakceptować zaufanie.
