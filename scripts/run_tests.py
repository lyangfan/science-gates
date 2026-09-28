#!/usr/bin/env python3
"""Run the single current SCI implementation's regression suite."""
from pathlib import Path
import os
import subprocess
import sys
import time


def main():
    scripts = Path(__file__).resolve().parent
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s",
                             str(scripts / "tests"), "-p", "test_*.py", "-v"],
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, check=False)
    if result.returncode:
        return result.returncode
    print("All suites passed in %.3f seconds." % (time.monotonic() - started))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
