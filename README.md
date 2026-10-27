# Rustflow

Rustflow is a Python CLI that coordinates Claude Code and Codex while developing a Rust project. It routes a task by difficulty, optionally plans it, implements it, runs deterministic checks, requests two independent reviews, and repeats fixes before writing a report.

Use it when you want a repeatable implementation and review loop with local evidence: prompts, process output, test results, findings, execution limits, and resumable state. You can also review existing changes, run the checks without an AI provider, or drive the workflow through MCP.

Rustflow is version **0.1.0**. Its source distribution lives in [`rustflow/`](rustflow/); this repository is a Python tool for Rust projects, not a Rust crate. It creates local branches and checkpoint commits; you inspect and integrate the result yourself.

## Requirements

- Python **3.11+** for the CLI, Git, and a Rust project with Cargo, rustfmt, and Clippy.
- Authenticated Claude Code and Codex CLIs for the default routing. You can configure all roles to use one supported provider. Model and flag availability depend on your installed provider CLI and account; `rustflow doctor` checks local capabilities.
- Optional: rust-analyzer, cargo-nextest, cargo-deny, cargo-semver-checks, SQLx CLI, and GitHub CLI for the features that use them.

The Python package has no runtime dependencies outside the standard library. CI covers Linux and Windows; copied hooks and gates have a separate Python 3.9 compatibility check. See [installation and platform details](rustflow/docs/usage.md#installation).

## Install from this checkout

Run from the root of this repository, preferably inside a virtual environment:

```bash
python -m pip install ./rustflow
rustflow --version
```

Alternatively, use `pipx install ./rustflow` or `uv tool install ./rustflow`. These install the local package; no published package registry release is assumed.

## Quickstart without model calls

Create a disposable Rust repository outside this checkout. The commands below require a configured Git author for the commit.

```bash
cargo new --lib rustflow-demo
cd rustflow-demo
git branch -M main
rustflow init --no-rust-skills --no-ci
rustflow gates --profile stop
git add .
git commit -m "Initialize Rustflow demo"
rustflow triage "Add an add(a: u64, b: u64) function with unit tests" --json
rustflow run "Add an add(a: u64, b: u64) function with unit tests" --dry-run
rustflow report --full
```

`--no-rust-skills` skips the default GitHub knowledge-base download; `--no-ci` skips generating a workflow. The `stop` profile checks formatting, Clippy, and tests without the full profile's optional registry-based semver check. Cargo can access the network if the project needs dependencies. Dry-run writes prompts and argv under `.rustflow/runs/<id>/` without calling providers, running workflow gates, creating a task branch, or proving the task complete.

## Run your first real task

In an existing Rust repository, after installing the provider CLIs and logging in:

```bash
rustflow init
rustflow doctor
# Review the generated configuration and files, then commit them.
git add .
git commit -m "Configure Rustflow"
rustflow run "Add an add(a: u64, b: u64) function with unit tests"
rustflow status
rustflow report --full
```

By default, `init` downloads a local copy of `rtmachines/rust-skills` into both skill directories; a download failure stops installation. Set `[project].language = "en"` before committing to request English model-written prose (default `pl`). The full report's fixed headings and status descriptions currently remain Polish. A real run invokes providers and can incur charges. It normally requires a clean Git tree and leaves you on a `rustflow/<run-id>` branch. `approved` and `approved_with_advisories` are successful outcomes; review advisories and disputes before integrating.

## Documentation

| Guide | Contents |
| --- | --- |
| [Usage](rustflow/docs/usage.md) | Installation, CLI reference, workflows, MCP, generated files |
| [Configuration](rustflow/docs/configuration.md) | Defaults, routing, providers, skills, gates, environment variables |
| [Architecture](rustflow/docs/architecture.md) | Module map, execution diagrams, state and usage accounting |
| [Troubleshooting](rustflow/docs/troubleshooting.md) | Diagnostics, failures, retries, limits, cancellation and resume |
| [Development](rustflow/docs/development.md) | Environment, tests, quality checks, extending components |
| [Documentation index](rustflow/docs/README.md) | Reading paths and historical material |

Rustflow is distributed under the [MIT license](LICENSE).
