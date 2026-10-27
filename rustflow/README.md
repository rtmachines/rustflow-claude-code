# Rustflow

Rustflow coordinates a Rust task through triage, optional planning, implementation, deterministic gates, two independent reviews, and a bounded fix loop. It uses the Claude Code and Codex CLIs and persists prompts, findings, process attempts, usage, and resumable state locally.

This directory is the Python **3.11+**, standard-library-only package (`rustflow` version 0.1.0). From a checkout, install it from the repository root:

```bash
python -m pip install ./rustflow
```

If your current directory is this package directory instead, use `python -m pip install .`. `pipx` and `uv tool install` can install the same local directory.

In the Rust repository you want to work on:

```bash
rustflow init --no-rust-skills --no-ci
rustflow gates --profile stop
git add .
git commit -m "Configure Rustflow"
rustflow triage "Add an add(a: u64, b: u64) function with unit tests" --json
rustflow run "Add an add(a: u64, b: u64) function with unit tests" --dry-run
rustflow report --full
```

This preview calls no model providers. Review generated files before committing them; a Git author is required for your commit. Install and authenticate the provider CLIs selected by your configuration, and run `rustflow doctor` before removing `--dry-run`. Normal `init` downloads the default Rust knowledge base; omit `--no-rust-skills` when you want it. Normal runs create local branches and checkpoint commits, and provider calls can incur charges.

The defaults use both Claude Code and Codex. Set `[project].language = "en"` to request English model-written prose; the full report's fixed headings and status descriptions remain Polish. The generated `report.md` lives under `.rustflow/runs/<id>/`; a dry-run result does not establish approval.

- [Full introduction and first real task](../README.md)
- [Usage and CLI reference](docs/usage.md)
- [Configuration reference](docs/configuration.md)
- [Architecture and execution flow](docs/architecture.md)
- [Troubleshooting and recovery](docs/troubleshooting.md)
- [Development and tests](docs/development.md)
- [Documentation index](docs/README.md)

License: [MIT](../LICENSE).
