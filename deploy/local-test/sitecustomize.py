"""Keep the local network fence in Python children of the repo test runner."""

import os
import runpy
import sys
from pathlib import Path

if os.environ.get("LOCAL_TEST_CLEAN_PROCESS") == "1":
    launcher = runpy.run_path(str(Path(__file__).with_name("run.py")))
    sys.addaudithook(launcher["network_guard"])
