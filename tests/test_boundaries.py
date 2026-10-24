"""The core and the standalone server are standard library only, and the
core never imports Django."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CHECK = """
import sys
import foxray.core.ingest, foxray.core.maintenance, foxray.core.sql, foxray.core.present, foxray.core.storage
import foxray.standalone.web, foxray.standalone.cli
bad = sorted(m for m in sys.modules if m.split('.')[0] in ('django', 'psycopg', 'psycopg2', 'sentry_sdk', 'brotli'))
print(','.join(bad))
"""


def test_core_and_standalone_import_only_the_stdlib():
    out = subprocess.run([sys.executable, "-c", CHECK], cwd=ROOT, capture_output=True, text=True, check=True,
                         env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"})
    # brotli is optional and imported only if installed; nothing else may load
    assert [m for m in out.stdout.strip().split(",") if m and not m.startswith("brotli")] == []


def test_core_source_never_mentions_django():
    for path in (ROOT / "foxray" / "core").glob("*.py"):
        text = path.read_text()
        assert "import django" not in text and "from django" not in text, path
