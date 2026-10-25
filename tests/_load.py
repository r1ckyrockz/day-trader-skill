"""Load a script from a skill folder (the folder names have hyphens)."""

import importlib.util
import os
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")

# The tests never see the user's own setup: no keys from the shell, and an empty
# working folder, so trader/config.json, trader/state/ and portfolio.json in the
# project are never read or written by a test run.
for _var in ("FINNHUB_API_KEY", "SEC_USER_AGENT"):
    os.environ.pop(_var, None)
os.chdir(tempfile.mkdtemp(prefix="day-trader-tests-"))


def load(folder, name):
    path = os.path.join(ROOT, folder, name + ".py")
    spec = importlib.util.spec_from_file_location("trader_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
