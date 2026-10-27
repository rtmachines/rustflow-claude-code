"""Exercise copied gates and their process helper without installing rustflow (Python 3.9+)."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    package = Path(__file__).resolve().parents[2] / 'rustflow' / 'rustflow'
    with tempfile.TemporaryDirectory(prefix='rustflow-standalone-') as scratch:
        root = Path(scratch)
        for name in ('gates.py', 'process.py', 'hooks.py'):
            shutil.copyfile(package / name, root / name)
        env = os.environ.copy()
        env.pop('PYTHONPATH', None)
        subprocess.run([sys.executable, str(root / 'gates.py'), '--help'], cwd=root, env=env, check=True)
        subprocess.run([sys.executable, '-c', '\n'.join([
            'import os, sys, gates',
            'from pathlib import Path',
            'assert gates.__file__.startswith(str(Path.cwd()))',
            'for size in (100, 101):',
            '    result = gates.run_tree([sys.executable, "-c", f"import os; os.write(1,b\'x\'*{size})"],',
            '                            Path.cwd(), os.environ.copy(), 5, max_output_bytes=100)',
            '    assert result.output_complete == (size == 100)',
            '    assert result.stdout_bytes == 100',
            '    assert result.ok == (size == 100)',
        ])], cwd=root, env=env, check=True)


if __name__ == '__main__':
    main()
