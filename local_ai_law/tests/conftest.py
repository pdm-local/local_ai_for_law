"""Pytest bootstrap.

Critically, this points RAG_DATA_DIR at a throwaway temp directory **before** the app
package (and thus config.DATA_DIR) is imported, so no test can read or mutate real user
data under ./data. Also wires sys.path so `local_ai_law.*` imports resolve, matching
scripts/security_smoke.py.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 1) Redirect the data dir AND the users store to a fresh temp tree BEFORE anything
#    imports config/auth — so no test can read or mutate the real users.json or ./data.
_TMP_DATA = tempfile.mkdtemp(prefix="ragtest_data_")
os.environ["RAG_DATA_DIR"] = _TMP_DATA
os.environ["RAG_USERS_FILE"] = str(Path(_TMP_DATA) / "users.json")
# TestClient sends "Host: testserver"; the front door (services/guard.py) only knows names it
# is told about.
os.environ.setdefault("PDM_HOSTS", "testserver")

# 2) Make the package importable: the folder that holds local_ai_law must be on sys.path.
#    tests/conftest.py parents: [0]=tests [1]=local_ai_law [2]=<pkg parent>
_PKG_PARENT = str(Path(__file__).resolve().parents[2])
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)
