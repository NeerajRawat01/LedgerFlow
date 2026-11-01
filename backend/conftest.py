"""Test configuration.

Point filesystem storage at a temporary directory BEFORE the application
module is imported, so the app's startup layout creation never writes
relative to the repository working directory.
"""

from __future__ import annotations

import os
import tempfile

_storage_root = tempfile.mkdtemp(prefix="ledgerflow-tests-")

os.environ["DATA_DIR"] = os.path.join(_storage_root, "data")
os.environ["STORAGE_DIR"] = os.path.join(_storage_root, "storage")
os.environ["OUTPUT_DIR"] = os.path.join(_storage_root, "output")
