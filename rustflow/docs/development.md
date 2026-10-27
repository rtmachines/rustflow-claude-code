# Development

[Documentation index](README.md) · [Architecture](architecture.md) · [Verification record](verification.md)

## Prepare an environment

The repository root contains the package directory `rustflow/`, CI scripts, license, and historical design material. There is no root `pyproject.toml` or Cargo workspace. The Python import package is `rustflow/rustflow/`, its templates are package data, and tests live in `rustflow/tests/`.

Use Python 3.11+ and Git. Integration tests also need Cargo, rustfmt, and Clippy, and create disposable local Git repositories. No paid provider credentials are required: tests use [`tests/fake_agents/fake_agent.py`](../tests/fake_agents/fake_agent.py) and controlled process responses. Fixtures disable the default knowledge-base download. Cargo can still need network access for a test project's dependencies or installed toolchain.

```bash
# Start at the repository root.
python -m venv .venv
# POSIX:
. .venv/bin/activate
# Windows PowerShell alternative: .venv\Scripts\Activate.ps1
python -m pip install -e "./rustflow[dev]"
cd rustflow
python -m pytest -q
python -m ruff check .
```

The `test` extra contains pytest; `dev` adds pytest, build, and Ruff. Runtime dependencies remain empty. The source [package configuration](../pyproject.toml) selects Ruff rules `E9,F63,F7,F82`; passing this configuration is a focused syntax/error check, not a broad style audit.

Run `python -m rustflow --help` from the package directory or outside the repository after installation. In an editable environment, the outer `rustflow/` directory can be resolved as a namespace package when invoking `python -m rustflow` from the repository root; this was observed to fail with an `__version__` import error. Changing into the package directory avoids that source-layout issue. Installed-wheel CI explicitly runs outside the checkout to test the shipped package.

### Useful focused tests

```bash
# From rustflow/:
python -m pytest -q tests/test_e2e.py tests/test_hooks_process.py
python -m pytest -q tests/test_agent_driver.py tests/test_mcp_driver.py
python -m pytest -q tests/test_attempts.py tests/test_usage.py tests/test_usage_state.py
python -m pytest -q tests/test_execution_budget.py tests/test_process_limits.py tests/test_budget_recovery.py tests/test_limit_flow.py
python -m pytest -q tests/test_criteria.py tests/test_tree_stability.py tests/test_fmt_base.py
python -m pytest -q tests/test_default_knowledge.py
```

The E2E fixture creates a dependency-free Rust crate and shares a temporary Cargo target directory. Provider executables are replaced with Python fakes while the actual Git/Cargo workflow runs. Tests cover successful fix loops, red gates, degraded review, disputes, isolation, dry-run, crashes, and recovery. Agent/process tests exercise timeout, cancellation, retry, output capture, and usage accounting. A passing fake-provider test does not validate a live account, remote model, or external plugin.

## Build and verify the installed product

From the repository root with the development environment active:

```bash
python -m build --wheel rustflow --outdir dist
```

To reproduce installed-product CI, create a separate environment, install the resulting wheel plus `pytest` and `ruff`, and run from the root:

```bash
# In the separate environment:
python -m pip install dist/rustflow-0.1.0-py3-none-any.whl pytest ruff
python -m ruff check rustflow
python .github/scripts/test-installed.py
python .github/scripts/test-standalone.py
```

[`test-installed.py`](../../.github/scripts/test-installed.py) removes inherited `PYTHONPATH`, uses a temporary working directory, checks console/module entry points and packaged resources, and runs the full test suite. It asserts that the imported package is installed under the environment prefix; an editable source installation is not a substitute. Ensure the environment's console script is on PATH for detached child processes.

[`test-standalone.py`](../../.github/scripts/test-standalone.py) copies gates/hooks/process helpers into a temporary directory and exercises output boundaries without importing the installed package. CI runs this with Python 3.9; running it locally with a newer Python only verifies that interpreter. On Python below 3.11, the standalone loader requires optional `tomli` to read custom TOML, otherwise it warns and uses gate defaults.

The [package workflow](../../.github/workflows/test.yml) builds and tests wheels on Ubuntu/Windows with Python 3.11/3.13 and checks standalone behavior on Python 3.9. This is distinct from the workflow template installed into Rust projects.

### Local reviewdog diagnostics

There is no repository reviewdog configuration. With Ruff and reviewdog available, run from the repository root:

```bash
set -o pipefail
rtk bypass ruff check rustflow --output-format=rdjson | reviewdog -f=rdjson -reporter=local -filter-mode=nofilter -fail-level=warning
```

This Bash command preserves pipeline failure and feeds raw Ruff JSON to the matching parser. If RTK is not installed, invoke `ruff` directly. The local reporter publishes nothing. A filtered report is not proof that the full project passes; the example deliberately uses `nofilter`.

## Extend the implementation

Read the relevant source and tests before changing behavior. The following are practical extension points, rather than a versioned plugin API.

| Change | Main implementation and evidence to update |
| --- | --- |
| Add/change CLI options | [`cli.py`](../rustflow/cli.py): parser, `Options` forwarding, detached argument forwarding; CLI and detached driver tests |
| Add/change MCP options | [`mcp_server.py`](../rustflow/mcp_server.py): schema, handler, mode filtering; driver tests and `runs.py` next-action mappings |
| Add a provider | [`config.py`](../rustflow/config.py), [`agents.py`](../rustflow/agents.py), [`doctor.py`](../rustflow/doctor.py), templates; runner construction, error classification, permissions, schema validation and fake provider cases |
| Add a role or output contract | Role routing/defaults in config/template, [`schemas.py`](../rustflow/schemas.py), [`prompts.py`](../rustflow/prompts.py), orchestrator phase handling and artifacts |
| Add a deterministic gate | [`gates.py`](../rustflow/gates.py): defaults, step result/diagnostics, profile plan, cache/tree behavior; tests for pass, fail, skip and incomplete process results |
| Change workflow/recovery | [`orchestrator.py`](../rustflow/orchestrator.py), [`runs.py`](../rustflow/runs.py): state transitions, resumability, next actions, locks and worktree cleanup; E2E and recovery tests |
| Change budget/usage | [`budget.py`](../rustflow/budget.py), [`usage.py`](../rustflow/usage.py), agent attempt lifecycle, report/summary; crash reconciliation and concurrent reviewer tests |
| Add skill/stack guidance | [`templates/`](../rustflow/templates/), installer registration/stack map, [`skills.py`](../rustflow/skills.py) discovery, language/interpreter rendering; installation and always-on skill tests |
| Change interactive hooks | [`hooks.py`](../rustflow/hooks.py), installer registrations, [`selftest.py`](../rustflow/selftest.py); subprocess hook tests and standalone compatibility |

`gates.py`, `hooks.py`, and `process.py` are copied into target repositories and must continue working without the Python package installed. Do not add package-only imports to the standalone path. Update packaged templates rather than editing generated files in a sample target. Changes to those scripts reach an existing target through another `rustflow init`; custom config is retained, so new defaults must remain compatible with older overrides.

When adding a process invocation, preserve bounded stdout/stderr capture, cancellation, execution scope, and process-tree cleanup. When adding a provider attempt, preserve its durable record before process launch and its relationship to the logical call. Complete output and known measurements must remain distinct from truncated output and unknown usage. See the [architecture invariants](architecture.md).

## Maintain the documentation

The root README is the first-use entry; the package README is also packaging metadata. Keep both short and link to thematic guides. Defaults belong in [configuration](configuration.md); CLI/MCP behavior in [usage](usage.md); recovery and limitations in [troubleshooting](troubleshooting.md). Keep historical design documents explicitly separate from the supported reference.

For changes, compare examples against `build_parser()`, MCP `TOOLS`, the packaged TOML template, actual configuration consumers, and relevant tests. Parse TOML/JSON snippets, check relative links and anchors, and exercise examples in a disposable repository. Provider/Actions/plugin examples require separate live verification; label unexecuted paths. There is currently no configured Markdown linter, link checker, or Mermaid renderer in this repository. The [verification record](verification.md) states exactly what was run for this documentation update.
