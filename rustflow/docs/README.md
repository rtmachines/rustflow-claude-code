# Rustflow documentation

These guides describe the implementation in this checkout. Start with the [repository README](../../README.md) for requirements and a short quickstart.

| Reader | Reading path |
| --- | --- |
| New user | [Usage: installation and workflows](usage.md), then [troubleshooting](troubleshooting.md) |
| Advanced user | [Configuration](configuration.md), [CLI and MCP](usage.md#cli-reference), [recovery](troubleshooting.md) |
| Contributor | [Architecture](architecture.md), then [development](development.md) |

The executable defaults are in the [packaged configuration template](../rustflow/templates/rustflow.toml). Some template comments, generated project instructions, and the full report's fixed headings remain Polish. The English guides explain the effective behavior and how to request English model-written prose. See the [verification record and remaining gaps](verification.md).

## Historical material

[DESIGN.md](DESIGN.md) records the original design, comparisons with earlier tools, and past live experiments in Polish. It is retained as historical evidence, not the command reference. In particular, use `triage --json` for the current cost estimate interface; there is no `triage --estimate` flag. Consult the current guides and implementation when historical examples disagree.

The repository-root [original design proposal](../../rustflow-DESIGN%282%29.md) is also historical, rather than a specification of supported behavior.
