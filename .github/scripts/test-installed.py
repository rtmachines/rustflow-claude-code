"""CI entry point: test the installed wheel, including detached child processes."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    repo = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONUTF8"] = "1"
    with tempfile.TemporaryDirectory(prefix="rustflow-installed-") as scratch:
        def run(*argv):
            subprocess.run(argv, cwd=scratch, env=env, check=True)

        for argv in (("git", "--version"), ("cargo", "--version"), ("rustc", "--version"),
                     ("rustfmt", "--version"), ("cargo", "clippy", "--version")):
            run(*argv)
        run("rustflow", "--help")
        run(sys.executable, "-m", "rustflow", "--help")
        run(sys.executable, "-c", "\n".join([
            "from pathlib import Path",
            "from importlib.resources import files",
            "import rustflow, sys",
            "assert Path(rustflow.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), rustflow.__file__",
            "templates = files('rustflow').joinpath('templates')",
            "for name in ('rustflow.toml', 'github/rustflow-gates.yml', 'skills/rustflow/SKILL.md',",
            "             'claude/skills/rustflow-pr-review/SKILL.md', 'stacks/axum-sqlx/SKILL.md'):",
            "    assert templates.joinpath(name).read_text(encoding='utf-8').strip(), name",
        ]))
        run(sys.executable, "-m", "pytest", "-q", str(repo / "rustflow" / "tests"))


if __name__ == "__main__":
    main()
