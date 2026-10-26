"""Markdown report of a run (Polish prose, matches [project].language = "pl" default)."""

from __future__ import annotations

import re
from pathlib import Path

STATUS_TEXT = {
    "approved": "ZATWIERDZONE - bramki zielone, brak blokujących uwag",
    "approved_with_advisories": "ZATWIERDZONE z uwagami doradczymi / spornymi do przejrzenia",
    "changes_requested": "WYMAGANE ZMIANY (tryb review bez pętli naprawczej)",
    "max_rounds": "LIMIT RUND - zostały blokujące uwagi",
    "stalled": "ZASTÓJ - fixer nie robi postępu, potrzebna decyzja człowieka",
    "gates_red": "BRAMKI CZERWONE - kod nie trafił do recenzji",
    "final_gates_red": "Recenzja OK, ale końcowe bramki (tryb CI) są czerwone",
    "criteria_unmet": "Recenzja OK, ale wymagane testy kryteriów nie przeszły na końcowym drzewie",
    "baseline_red": "BAZA CZERWONA - bramki nie przechodzą jeszcze przed zmianą",
    "no_changes": "BRAK ZMIAN",
    "review_failed": "RECENZJA NIEUDANA - żaden recenzent nie zwrócił wyniku",
    "agent_failed": "AGENT NIEUDANY - wywołanie modelu nie powiodło się, drzewo bez zmian (można wznowić)",
    "budget": "PRZEKROCZONY BUDŻET",
    "error": "BŁĄD",
    "interrupted": "PRZERWANE (można wznowić)",
    "tree_changed": "PLIKI ZMIENIONE W TRAKCIE RECENZJI - wyniki odrzucone (można wznowić)",
    "running": "W TOKU",
}


def _table(rows: list[list[str]], head: list[str]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    for r in rows:
        out.append("| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in r) + " |")
    return "\n".join(out)


def render(state: dict, repo: Path) -> str:
    st = state.get("status", "running")
    lines = [f"# rustflow: raport `{state.get('run_id')}`", ""]
    lines.append(f"**Status:** {STATUS_TEXT.get(st, st)}")
    if state.get("stop_reason"):
        lines += ["", "```", state["stop_reason"][:4000], "```"]
    lines += ["", "## Zadanie", "", state.get("task", "").strip(), ""]

    tri = state.get("triage") or {}
    lines += ["## Przebieg", ""]
    lines.append(f"- Trudność: **{state.get('difficulty', '?')}** (źródło: {tri.get('source', '?')}, "
                 f"wynik {tri.get('score', '?')}; {', '.join(tri.get('reasons', []))})")
    lines.append(f"- Baza: `{state.get('base_ref')}` = `{(state.get('base_sha') or '')[:12]}`; "
                 f"izolacja: {state.get('isolation')}" +
                 (f", gałąź `{state['branch']}`" if state.get("branch") else ""))
    lines.append(f"- Rundy recenzji: {len(state.get('rounds', []))}; koszt Claude: "
                 f"${state.get('cost_usd', 0):.2f}; tokeny Codex/Claude: "
                 + ", ".join(f"{k}={v}" for k, v in sorted((state.get('tokens') or {}).items())
                             if isinstance(v, (int, float)) and k.endswith("tokens")))
    if state.get("final_gates"):
        lines.append(f"- Końcowe bramki (CI): {state['final_gates']}")
    if state.get("escalate"):
        lines += ["", "> **ESCALATE:** zmiana dotyka unsafe/FFI/atomics/kolejności blokad. Przed merge "
                      "uruchom dedykowany audyt soundness (np. `/rust-review:rust-review` Trail of Bits) "
                      "na plikach z sygnałami."]
    lines.append("")

    audit = state.get("audit")
    if audit:
        lines += ["## Audyt soundness (Trail of Bits rust-review)", "",
                  f"Zakres: `{audit.get('scope')}`; pliki z sygnałami: "
                  + ", ".join(f"`{f}`" for f in audit.get("files", [])),
                  f"Raport: `{audit['report']}`" if audit.get("report") else
                  f"Brak REPORT.md ({audit.get('error') or 'zobacz log agenta audit-tob'})", ""]
        if audit.get("excerpt"):
            lines += ["<details><summary>Fragment raportu</summary>", "", audit["excerpt"][:4000], "",
                      "</details>", ""]

    agents = state.get("agents") or []
    if agents:
        def cost(a: dict) -> str:
            if not a.get("cost_usd"):
                return "-"
            return f"≤${a['cost_usd']:.2f} (limit roli, timeout)" if a.get("cost_estimated") else f"${a['cost_usd']:.2f}"
        rows = [[a.get("name", ""), a.get("spec", ""), "ok" + (" (JSON naprawiony)" if a.get("json_repair") else "")
                 if a.get("ok") else "BŁĄD", f"{a.get('duration_s', 0):.0f}s", cost(a),
                 (a.get("error") or "")[:80]] for a in agents]
        lines += ["## Agenci", "", _table(rows, ["wywołanie", "model", "wynik", "czas", "koszt", "błąd"]), ""]
        denied = [(a.get("name", ""), d) for a in agents for d in a.get("permission_denials") or []]
        if denied:
            lines += ["Odrzucone wywołania narzędzi (bez pytania, `--permission-prompts none`; każde kosztuje agenta "
                      "turę - rozważ dopisanie do allowlisty w `[providers.claude]`):", ""]
            lines += [f"- `{n}`: `{d}`" for n, d in denied[:20]] + [""]

    plan = state.get("plan")
    if plan:
        lines += ["## Plan", ""]
        lines += [f"{i + 1}. {s}" for i, s in enumerate(plan.get("steps", []))] + [""]

    crits = state.get("criteria") or []
    if crits:
        mech = {m["id"]: m for m in state.get("mechanical") or []}
        last = (state.get("rounds") or [{}])[-1].get("acceptance", [])
        rows = []
        for c in crits:
            own = re.compile(rf"^\W*{re.escape(c['id'])}\b", re.I)
            votes = [f"{a.get('reviewer', '?')}: {a.get('status')}" for a in last
                     if own.match(a.get("criterion", "")) or a.get("criterion", "").strip().lower() == c["text"].lower()]
            m = mech.get(c["id"])
            test = f"`{c['test']}`: **{'OK' if m and m['status'] == 'met' else 'BRAK'}**" if c.get("test") else "-"
            if m and m["status"] != "met":
                test += f" ({m['detail']})"
            rows.append([c["id"], c["text"], c.get("source", ""), test, ", ".join(votes) or "-"])
        lines += ["## Kryteria akceptacji", "",
                  _table(rows, ["id", "kryterium", "źródło", "wymagany test (sprawdza orkiestrator)",
                                "ocena recenzentów (ostatnia runda)"]), ""]

    impl = state.get("implementation") or {}
    if impl.get("summary"):
        lines += ["## Implementacja", "", impl["summary"], ""]
        if impl.get("tests_added"):
            lines += ["Testy: " + ", ".join(impl["tests_added"]), ""]

    for rec in state.get("rounds", []):
        lines += [f"## Runda {rec['round']}", ""]
        lines.append("Werdykty: " + ", ".join(f"{k}: **{v}**" for k, v in rec.get("verdicts", {}).items()))
        if rec.get("failed_reviewers"):
            lines.append(f"Recenzenci bez wyniku: {', '.join(rec['failed_reviewers'])}")
        for name, summ in (rec.get("summaries") or {}).items():
            if summ:
                lines.append(f"- _{name}_: {summ}")
        rows = []
        for f in rec.get("findings", []):
            anchor = f"{f['file']}:{f['line']}" if f.get("file") else "-"
            rows.append([f["id"], f["severity"], f["status"], anchor, f["title"][:140],
                         ",".join(f.get("reviewers", [])), f.get("resolution_status", "")])
        if rows:
            lines += ["", _table(rows, ["id", "sev", "status", "miejsce", "opis", "kto", "fixer"])]
        else:
            lines += ["", "Brak uwag."]
        if rec.get("dropped"):
            lines += ["", f"Odrzucone automatycznie (nieistniejący plik): {len(rec['dropped'])}"]
        fix = rec.get("fix")
        if fix:
            lines += ["", f"**Fixer:** {fix.get('summary', '')}"]
        lines.append("")

    findings = state.get("findings", [])
    disputed = [f for f in findings if f.get("status") == "disputed"]
    open_ = [f for f in findings if f.get("status") == "open"]
    advisory = [f for f in findings if f.get("status") == "advisory"]
    if open_ or disputed or advisory:
        lines += ["## Do decyzji człowieka", ""]
        for title, group in (("Blokujące, nierozwiązane", open_), ("Sporne (fixer vs recenzent, 2x)", disputed),
                             ("Doradcze", advisory)):
            if group:
                lines.append(f"**{title}**")
                for f in group:
                    anchor = f"`{f['file']}:{f['line']}`" if f.get("file") else ""
                    lines.append(f"- [{f['id']}] {f['severity']} {anchor} {f['title']}"
                                 + (f" - fix: {f['fix']}" if f.get("fix") else "")
                                 + (f" - fixer: {f['resolution']}" if f.get("resolution") else ""))
                lines.append("")

    risks = [r for rec in state.get("rounds", [])[-1:] for r in rec.get("residual_risks", [])]
    if risks:
        lines += ["## Ryzyka resztkowe", ""] + [f"- {r}" for r in risks] + [""]

    if state.get("final_gates_feedback"):
        lines += ["## Końcowe bramki - błędy", "", "```", state["final_gates_feedback"][:6000], "```", ""]

    lines += ["## Co dalej", ""]
    if state.get("branch") and state.get("isolation") == "branch":
        orig = state.get("original_branch", "main")
        lines += [f"- Przejrzyj: `git diff {(state.get('base_sha') or '')[:12]}..{state['branch']}`",
                  f"- Scal: `git switch {orig} && git merge --squash {state['branch']}`",
                  f"- Odrzuć: `git switch {orig} && git branch -D {state['branch']}`"]
    elif state.get("isolation") == "worktree":
        lines += [f"- Worktree: `{state.get('workdir')}` (gałąź `{state.get('branch')}`)",
                  f"- Usuń: `git worktree remove {state.get('workdir')}`"]
    if st in ("interrupted", "budget", "tree_changed"):
        lines.append(f"- Wznów: `rustflow resume {state.get('run_id')}`")
    lines.append(f"- Logi agentów: `.rustflow/runs/{state.get('run_id')}/agents/`")
    return "\n".join(lines) + "\n"
