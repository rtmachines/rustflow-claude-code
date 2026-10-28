# Documentation verification

This record describes local verification of the documentation update on **2026-10-27**. It is not a claim that external provider services or GitHub Actions were exercised.

## Checks performed

| Check | Result and scope |
| --- | --- |
| Source investigation | Used the repository code graph to locate entry points and relationships, then read implementation and relevant tests. Compared CLI parsing, MCP schemas, configuration consumers, templates, recovery, and reporting. |
| Full local test suite | `cd rustflow && .venv/bin/python -m pytest -q -p no:cacheprovider`: **326 passed**, no skips, in 364.75 seconds on Linux/Python 3.13.17. Provider calls were fakes or controlled responses; Git and Cargo were real. |
| Ruff through reviewdog | From the repository root, raw `python -m ruff check rustflow --output-format=rdjson` output was piped to `reviewdog -f=rdjson -reporter=local -filter-mode=nofilter -fail-level=warning` with Bash `pipefail` and RTK bypass: exit 0, no diagnostics. |
| Wheel build | `uv build --wheel rustflow --out-dir /tmp/rustflow-docs-dist` succeeded. Setuptools emitted package-discovery warnings for template directories; installed resource checks below passed. An earlier `build --no-isolation` attempt could not find setuptools in the existing development environment; the isolated uv build supplied the backend. |
| Local wheel installation | Installed the built wheel into a separate Python 3.13 environment in `/tmp`. Confirmed imports came from that environment and checked packaged default config, CI, driver skill, interactive reviewer skill, and stack skill resources. |
| README quickstart | Executed in a disposable, dependency-free library repository with offline Cargo: initialize without knowledge download/CI, stop gates, commit generated files and lockfile, triage JSON, dry-run, and full report. All final commands exited 0; dry-run kept `main` checked out and created no provider attempt records. |
| Standalone full and CI gates | Both passed in the demo after explicitly setting `[gates].semver = "off"`. SQLx skipped because no query macros existed; deny skipped because no `deny.toml` existed; semver was deliberately disabled. These results do not validate those integrations. |
| Hook self-test | Installed `rustflow doctor --selftest`: **14/14 passed** with Python 3.13. Includes formatting/check feedback, Stop behavior, guards, UTF-8 input, router, report contract, context, and strict gate behavior. |
| Standalone compatibility harness | `python .github/scripts/test-standalone.py` passed locally with Python 3.13. Python 3.9 compatibility is represented by existing CI configuration, not a local 3.9 execution in this update. |
| MCP smoke check | Installed stdio server accepted initialization, listed all eight tools, and returned local triage through `tools/call`. Detached start/cancel/resume and other MCP paths were covered by the full fake-provider test suite. |
| PR comment preview | `rustflow comment --pr 123 --dry-run` exited 0 and prepared/printed the command. No GitHub comment was published. |
| Documentation structure | A local one-off checker resolved relative paths and heading anchors in the current guides, checked code fences/whitespace, parsed TOML/JSON examples, validated merged configuration examples and criteria, and compared the CLI option inventory and default-template keys with their references. `git diff --check` also passed. |

The quickstart was corrected using actual failures: standalone full gates attempted a registry semver baseline for a new crate, and running Cargo after the initial commit generated an untracked `Cargo.lock`. The documented preview now uses `stop` and runs it **before** committing project files.

## Verification not performed

- No paid Claude/Codex invocation, authentication flow, remote model availability check, or live fallback-model test.
- No real Trail of Bits plugin audit, Apollo download, or default knowledge-base clone. Installation/download behavior is covered by controlled tests; upstream content and availability were not independently checked.
- No live SQLx database/migration, cargo-deny policy, registry semver comparison, or GitHub Actions run. The configuration example applying migrations requires a configured test database and changes that database.
- No Windows run in this local session. Windows coverage described in the guides refers to the checked-in test matrix and platform-specific code/tests.
- No visual Mermaid rendering or external-link availability check. The diagrams use GitHub Mermaid flowchart syntax; local tools for Markdown linting and Mermaid rendering were not configured/available. Local Markdown structure and relative destinations were checked.
- The separate installed-wheel CI harness was inspected but not rerun as a second full test suite. Wheel import/resources/CLI and quickstart were checked separately after the full source-environment suite.

## Implementation observations left unchanged

These are limitations or follow-up candidates found while documenting behavior; this update changes documentation only.

| Observation | Source / consequence |
| --- | --- |
| Full-report localization is partial | [`report.py`](../rustflow/report.py) has fixed Polish headings/status text. `project.language = "en"` requests English agent prose but does not translate the report shell. |
| Alternate config has limited reach | [`cli.py`](../rustflow/cli.py) accepts common `--config` even on handlers that do not consume it; [`orchestrator.py`](../rustflow/orchestrator.py) reads gate settings from the run's base commit and freezes them in the run state. Commit them before you start. |
| Configuration validation is partial | [`config.py`](../rustflow/config.py) does not reject every unknown key or validate every gate/provider type/enum. A typo can be ineffective. |
| Final workflow gates differ from CI mode | Final uses strict full gates but not `ci=True`; conditional extras and deny policy can differ. The report's fixed label still refers to final gates as CI. |
| Accepted baseline failures remain accepted at final | [`Orchestrator._gates_ok`](../rustflow/orchestrator.py) applies recorded signature acceptance to final gates as well. Approval does not mean all baseline failures disappeared. |
| Resume exit status needs interpretation | Already finished, nonresumable runs can make `resume` return 0 without changing their failed outcome. Read the resulting status. |
| Disputes and degraded reviews need attention | [`findings.py`](../rustflow/findings.py) can make a repeatedly disputed P0 nonblocking; an ordinary reviewer failure can leave approval by the remaining reviewer. Summaries/reports expose these conditions. |
| Report-only checks can update lockfiles | Intermediate review gates disable formatting writes but are unlocked; Cargo can generate/update `Cargo.lock`. Final gates are strict. |
| Dollar accounting is not a global billing cap | Claude thresholds are checked before logical calls; retries/repair/parallel in-flight work can exceed a threshold, and Codex usage has no measured USD here. |
| Editable root module invocation can fail | From this repository's root, the outer source directory can shadow an editable package as a namespace; `python -m rustflow` failed locally with an `__version__` import error. Use the package directory or an installed wheel outside the checkout. |
| Template packaging emits warnings | The current explicit setuptools package declaration produces discovery warnings, although the wheel contained the resources checked above. |

See [troubleshooting](troubleshooting.md) for operational responses and [development](development.md) for checks to run after implementation changes.
