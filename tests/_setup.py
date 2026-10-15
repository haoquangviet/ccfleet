"""Imported first by every test: point CCFLEET_HOME at a throwaway dir (tests must never touch a
real install) and put the repo root on sys.path."""
import atexit
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if not os.environ.get("CCFLEET_TEST_HOME"):
    os.environ["CCFLEET_TEST_HOME"] = tempfile.mkdtemp(prefix="ccfleet-test-")
    atexit.register(shutil.rmtree, os.environ["CCFLEET_TEST_HOME"], True)
os.environ["CCFLEET_HOME"] = os.environ["CCFLEET_TEST_HOME"]
