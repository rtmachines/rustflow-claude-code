"""rustflow command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rustflow import __version__, gates, gitutil, triage
from rustflow.config import DIFFICULTIES, Config, ConfigError


def _repo(path: str | None) -> Path:
    start = Path(path or ".").resolve()
    if gitutil.is_repo(start):
        return gitutil.toplevel(start)
    return start


def _task(args: argparse.Namespace) -> str:
    if getattr(args, "file", None):
        return Path(args.file).read_text(encoding="utf-8")
    text = " ".join(args.task or []).strip()
    if text == "-" or (not text and not sys.stdin.isatty()):
        return sys.stdin.buffer.read().decode("utf-8", errors="replace")
    return text


def _cfg(repo: Path, args: argparse.Namespace) -> Config:
    return Config.load(repo, Path(args.config) if getattr(args, "config", None) else None)


def cmd_init(args: argparse.Namespace) -> int:
    from rustflow.installer import Installer
    Installer(_repo(args.repo), force=args.force, python_cmd=args.python_cmd, local_settings=args.local,
              ci=not args.no_ci, with_apollo=args.with_apollo, with_plugins=args.with_plugins,
              router=not args.no_router, stack=args.stack, with_mcp=args.with_mcp).run()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from rustflow.doctor import Doctor, load_cfg
    repo = _repo(args.repo)
    if args.selftest:
        from rustflow.selftest import SelfTest
        return SelfTest(repo).run()
    cfg, err = load_cfg(repo, Path(args.config) if args.config else None)
    return Doctor(repo, cfg, err).run()


def cmd_triage(args: argparse.Namespace) -> int:
    repo = _repo(args.repo)
    cfg = _cfg(repo, args)
    task = _task(args)
    res = triage.heuristic(task, repo, cfg)
    if args.difficulty:
        res = triage.TriageResult(args.difficulty, res.score, ["set on the command line"], "cli")
    routing = {r: str(cfg.spec_for(r, res.difficulty)) for r in ("implementer", "reviewer_a", "reviewer_b", "fixer")}
    est = triage.estimate_cost(repo, routing, res.difficulty)
    if args.json:
        print(json.dumps({**res.to_dict(), "routing": routing, "estimate": est}, indent=2))
        return 0
    print(f"difficulty: {res.difficulty}   score: {res.score}")
    for r in res.reasons:
        print(f"  - {r}")
    print("\nrouting:")
    for role in ("planner", "implementer", "reviewer_a", "reviewer_b", "fixer"):
        if role == "planner":
            when = cfg.get("roles.planner.when")
            if when == "never" or (when == "hard" and res.difficulty != "hard"):
                print(f"  {'planner':<12} (skipped: when={when})")
                continue
        lens = cfg.role_opt(role, "lens")
        print(f"  {role:<12} {cfg.spec_for(role, res.difficulty)}" + (f"   lens={lens}" if lens else ""))
    if cfg.get("roles.auditor.spec"):
        print(f"  {'auditor':<12} {cfg.get('roles.auditor.spec')}   (only if unsafe/FFI/atomics in diff)")
    print(f"\ncost: {est['text']}")
    if cfg.get("triage.mode") != "heuristic":
        print(f"\nnote: triage.mode={cfg.get('triage.mode')} - `run` will also ask {cfg.get('triage.model')}")
    return 0


def _options(args: argparse.Namespace, mode: str):
    from rustflow.orchestrator import Options
    return Options(mode=mode, difficulty=args.difficulty, base=args.base,
                   isolation=getattr(args, "isolation", None), allow_dirty=getattr(args, "allow_dirty", False),
                   accept_red_baseline=getattr(args, "accept_red_baseline", False),
                   max_rounds=args.max_rounds, dry_run=args.dry_run, no_plan=getattr(args, "no_plan", False),
                   fix=getattr(args, "fix", True),
                   criteria_file=str(Path(args.criteria).resolve()) if getattr(args, "criteria", None) else None)


def forward_args(args: argparse.Namespace, mode: str) -> list[str]:
    """CLI flags of a run/review, for re-launching it as a detached process (task goes via -f)."""
    out = [mode]
    for flag, attr in (("--repo", "repo"), ("--config", "config"), ("--difficulty", "difficulty"),
                       ("--base", "base"), ("--max-rounds", "max_rounds"), ("--isolation", "isolation"),
                       ("--criteria", "criteria")):
        val = getattr(args, attr, None)
        if val is not None:
            out += [flag, str(Path(val).resolve()) if attr == "criteria" else str(val)]
    for flag, attr in (("--allow-dirty", "allow_dirty"), ("--accept-red-baseline", "accept_red_baseline"),
                       ("--no-plan", "no_plan"), ("--fix", "fix"), ("--dry-run", "dry_run")):
        if getattr(args, attr, False):
            out.append(flag)
    return out


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _detach(repo: Path, cli_args: list[str], task: str | None, run_id: str | None = None) -> int:
    from rustflow import runs
    try:
        info = runs.spawn_detached(repo, cli_args, task, run_id)
    except runs.RunLocked as exc:
        _print_json({"status": "locked", "error": str(exc)})
        return 2
    _print_json(info)
    return 0 if info["status"] != "failed_to_start" else 2


def cmd_run(args: argparse.Namespace) -> int:
    from rustflow.orchestrator import Orchestrator
    repo = _repo(args.repo)
    task = _task(args)
    if not task.strip():
        print("error: empty task (pass text, -f FILE or pipe it on stdin)", file=sys.stderr)
        return 2
    if args.detach:
        return _detach(repo, forward_args(args, "run"), task)
    return Orchestrator(repo, _cfg(repo, args), _options(args, "run")).start(task, run_id=args.run_id)


def cmd_review(args: argparse.Namespace) -> int:
    from rustflow.orchestrator import Orchestrator
    repo = _repo(args.repo)
    task = _task(args) or ("Review the current changes against the base. Judge correctness, robustness, "
                           "tests and idioms; infer the intent from the diff and commit messages.")
    if args.detach:
        return _detach(repo, forward_args(args, "review"), task)
    return Orchestrator(repo, _cfg(repo, args), _options(args, "review")).start(task, run_id=args.run_id)


def cmd_resume(args: argparse.Namespace) -> int:
    from rustflow.orchestrator import Options, Orchestrator, find_run
    repo = _repo(args.repo)
    run_dir = find_run(repo, args.run)
    if args.detach:
        extra = (["--repo", args.repo] if args.repo else []) + (["--config", args.config] if args.config else [])
        return _detach(repo, ["resume", run_dir.name, *extra], None, run_id=run_dir.name)
    return Orchestrator(repo, _cfg(repo, args), Options(dry_run=args.dry_run)).resume(run_dir)


def cmd_status(args: argparse.Namespace) -> int:
    from rustflow import runs
    repo = _repo(args.repo)
    s = runs.summary(runs.load_state(runs.find_run(repo, args.run)))
    if args.json:
        _print_json(s)
    else:
        print(runs.brief_markdown(s))
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    from rustflow import runs
    repo = _repo(args.repo)
    s = runs.wait(runs.find_run(repo, args.run), float(args.timeout),
                  out=(lambda *_: None) if args.json else print)
    if args.json:
        _print_json(s)
    else:
        print("\n" + runs.brief_markdown(s))
    return runs.exit_code(s["status"])


def cmd_report(args: argparse.Namespace) -> int:
    from rustflow import runs
    repo = _repo(args.repo)
    run_dir = runs.find_run(repo, args.run)
    state = runs.load_state(run_dir)
    if args.json:
        s = runs.summary(state, findings_limit=1000)
        s["findings"] = state.get("findings", [])
        _print_json(s)
    elif args.full:
        rpt = run_dir / "report.md"
        print(rpt.read_text(encoding="utf-8") if rpt.is_file() else "(no report yet)")
    else:
        print(runs.brief_markdown(runs.summary(state)))
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    from rustflow import runs
    repo = _repo(args.repo)
    _print_json(runs.cancel(repo, runs.find_run(repo, args.run)))
    return 0


GITHUB_COMMENT_LIMIT = 65000


def cmd_comment(args: argparse.Namespace) -> int:
    """Post a run's report.md on the GitHub PR via `gh` (replaces the kit's `/code-review --comment`)."""
    from rustflow import procutil
    from rustflow.orchestrator import find_run
    repo = _repo(args.repo)
    run_dir = find_run(repo, args.run)
    report = run_dir / "report.md"
    if not report.is_file():
        print(f"error: {report} does not exist (run not finished?)", file=sys.stderr)
        return 2
    body = report.read_text(encoding="utf-8")
    if len(body) > GITHUB_COMMENT_LIMIT:
        body = body[:GITHUB_COMMENT_LIMIT - 200] + "\n\n... (truncated; full report in .rustflow/runs/)\n"
    out = run_dir / "pr-comment.md"
    out.write_text(body, encoding="utf-8")
    argv = ["pr", "comment"] + ([str(args.pr)] if args.pr else []) + ["--body-file", str(out)]
    if args.dry_run:
        print("gh " + " ".join(argv))
        return 0
    try:
        gh = procutil.resolve_command("gh")
    except procutil.CommandNotFound:
        print("error: GitHub CLI `gh` not found (https://cli.github.com), or use --dry-run", file=sys.stderr)
        return 2
    r = procutil.run(gh + argv, cwd=repo, timeout_s=120)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    return r.returncode


def cmd_lints(args: argparse.Namespace) -> int:
    from rustflow import lints
    try:
        return lints.run(_repo(args.repo), do_apply=args.apply, as_json=args.json,
                         exclude={x.strip() for x in (args.exclude or "").split(",") if x.strip()})
    except lints.LintError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def cmd_mcp(args: argparse.Namespace) -> int:
    from rustflow.mcp_server import main as mcp_main
    return mcp_main()


def cmd_gates(args: argparse.Namespace) -> int:
    argv = ["ci"] if args.ci else ["run", "--profile", args.profile] + (["--no-cache"] if args.no_cache else [])
    if args.json:
        argv.append("--json")
    argv += ["--root", str(_repo(args.repo))]
    return gates.main(argv)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="rustflow", description="Claude Code + Codex workflow for Rust")
    ap.add_argument("--version", action="version", version=f"rustflow {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--repo", help="repository path (default: current git toplevel)")
        p.add_argument("--config", help="explicit rustflow.toml")

    p = sub.add_parser("init", help="install rustflow into a Rust repository")
    common(p)
    p.add_argument("--force", action="store_true", help="overwrite customized templates")
    p.add_argument("--python-cmd", help='interpreter for hooks, e.g. "py -3" or "python3"')
    p.add_argument("--local", action="store_true", help="register hooks in .claude/settings.local.json")
    p.add_argument("--no-ci", action="store_true", help="do not add the GitHub Actions workflow")
    p.add_argument("--with-apollo", action="store_true", help="also install Apollo rust-best-practices skill")
    p.add_argument("--with-plugins", action="store_true",
                   help="enable the Trail of Bits rust-review plugin (soundness audit for ESCALATE)")
    p.add_argument("--stack", choices=("axum-sqlx",), help="add a stack skill (+ matching CI), e.g. axum-sqlx")
    p.add_argument("--with-mcp", action="store_true",
                   help="register the rustflow MCP server in .mcp.json (typed tools for a driving agent)")
    p.add_argument("--no-router", action="store_true",
                   help="do not register the UserPromptSubmit hook that steers reviews to /rustflow-pr-review")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("doctor", help="check tools, CLIs, models and installation")
    common(p)
    p.add_argument("--selftest", action="store_true",
                   help="replay hook scenarios in a temp crate exactly as Claude Code runs them")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("triage", help="show difficulty and model routing for a task (no agents)")
    common(p)
    p.add_argument("task", nargs="*")
    p.add_argument("-f", "--file")
    p.add_argument("--difficulty", choices=DIFFICULTIES)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_triage)

    def runopts(p: argparse.ArgumentParser) -> None:
        p.add_argument("--detach", action="store_true",
                       help="run in the background, print {run_id,...} as JSON and return (for agents)")
        p.add_argument("--run-id", help=argparse.SUPPRESS)
        p.add_argument("--difficulty", choices=DIFFICULTIES, help="skip triage")
        p.add_argument("--base", help="base ref (default: [project].base)")
        p.add_argument("--max-rounds", type=int)
        p.add_argument("--dry-run", action="store_true", help="write prompts/commands, call no agents")
        p.add_argument("--criteria", metavar="TOML",
                       help="acceptance criteria file: [[criteria]] text = \"...\", test = \"mod::tests::name\" "
                            "(a named test must run and pass - checked by the orchestrator)")

    p = sub.add_parser("run", help="full workflow for a task")
    common(p)
    runopts(p)
    p.add_argument("task", nargs="*", help="task text (or -f FILE, or stdin)")
    p.add_argument("-f", "--file")
    p.add_argument("--isolation", choices=("branch", "worktree", "none"))
    p.add_argument("--allow-dirty", action="store_true")
    p.add_argument("--accept-red-baseline", action="store_true")
    p.add_argument("--no-plan", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("review", help="review (and optionally fix) existing changes vs a base")
    common(p)
    runopts(p)
    p.add_argument("task", nargs="*", help="optional intent/context for reviewers")
    p.add_argument("-f", "--file")
    p.add_argument("--fix", action="store_true", help="run the fix loop in place (default: report only)")
    p.set_defaults(fn=cmd_review)

    p = sub.add_parser("resume", help="continue an interrupted run")
    common(p)
    p.add_argument("run", nargs="?", default="latest")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--detach", action="store_true", help="resume in the background")
    p.set_defaults(fn=cmd_resume)

    p = sub.add_parser("status", help="compact state of a run (+ next_actions)")
    common(p)
    p.add_argument("run", nargs="?", default="latest")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("wait", help="block until a run finishes or --timeout (exit 75 = still running)")
    common(p)
    p.add_argument("run", nargs="?", default="latest")
    p.add_argument("--timeout", type=float, default=540, help="seconds (default 540: under Claude Code's 10 min "
                                                               "Bash limit)")
    p.add_argument("--json", action="store_true", help="only the final JSON summary, no progress lines")
    p.set_defaults(fn=cmd_wait)

    p = sub.add_parser("report", help="run report: brief (default), --json or --full markdown")
    common(p)
    p.add_argument("run", nargs="?", default="latest")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true")
    g.add_argument("--full", action="store_true")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("cancel", help="stop a running run gracefully (resumable)")
    common(p)
    p.add_argument("run", nargs="?", default="latest")
    p.set_defaults(fn=cmd_cancel)

    p = sub.add_parser("comment", help="post a run report as a GitHub PR comment (gh)")
    common(p)
    p.add_argument("run", nargs="?", default="latest")
    p.add_argument("--pr", help="PR number/URL (default: the PR of the current branch)")
    p.add_argument("--dry-run", action="store_true", help="print the gh command only")
    p.set_defaults(fn=cmd_comment)

    p = sub.add_parser("lints", help="lint ratchet: measure candidate lints, adopt the ones the code satisfies")
    common(p)
    p.add_argument("--apply", action="store_true", help="write adoptable lints to [workspace.lints] / [lints]")
    p.add_argument("--exclude", help="comma-separated lint names to skip, e.g. print_stdout,missing_docs")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_lints)

    p = sub.add_parser("mcp", help="serve rustflow as an MCP server on stdio (for Claude Code / agents)")
    p.set_defaults(fn=cmd_mcp)

    p = sub.add_parser("gates", help="run the deterministic gates")
    common(p)
    p.add_argument("--profile", choices=gates.PROFILES, default="full")
    p.add_argument("--ci", action="store_true")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_gates)
    return ap


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # Windows consoles with legacy code pages
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except gitutil.GitError as exc:
        print(f"git error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
