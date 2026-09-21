"""Run tests with isolated default state/config, including child processes."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="worker-desk-tests-") as td:
        env = dict(os.environ, DELEGATE_STATE=str(Path(td) / "state"),
                   DELEGATE_CONFIG=str(Path(td) / "config.json"))
        return subprocess.call([sys.executable, "-m", "unittest", "discover", "-s", "tests",
                                *(sys.argv[1:] or [])], cwd=root, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
