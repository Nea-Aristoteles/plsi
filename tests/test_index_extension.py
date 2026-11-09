"""Installed extensions keep working through the indexed ingest, and cannot reach the event index (synthetic extension)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from index_support import cli, home, init, naive_events, obs  # noqa: F401


def _extension(root: Path, ext_id: str, body: str) -> Path:
    folder = root / ext_id
    (folder / "tests").mkdir(parents=True)
    (folder / "plsi_extension.yaml").write_text(f"id: {ext_id}\nversion: 0.1.0\nkind: importer\nentrypoint: main:run\nsummary: synthetic lines importer\n", encoding="utf-8")
    (folder / "main.py").write_text(body, encoding="utf-8")
    (folder / "tests" / "test_main.py").write_text("import main\n\ndef test_runs():\n    assert main.run(b'1\\n') and main.run(b'')==[]\n", encoding="utf-8")
    return folder


GOOD = '''import json


def run(data: bytes) -> list[dict]:
    return [{"kind": "measurement", "original_text": "synthetic metric", "code": {"system": "loinc", "code": "2345-7"}, "value_quantity": float(line),
             "unit": "mmol/L", "effective_at": "2026-01-01T00:00:00Z", "origin": {"kind": "extension", "record_id": f"line-{i}"}}
            for i, line in enumerate(data.decode().split()) if line]
'''


def test_importer_extension_ingests_dedupes_and_is_blocked_from_the_index(home, tmp_path):
    pytest.importorskip("pytest")
    init()
    folder = _extension(tmp_path, "lines", GOOD)
    out = cli(home, "ext", "install", "--dir", str(folder))
    if not out["ok"] and "pytest" in json.dumps(out):
        pytest.skip("pytest is not importable by the extension test subprocess in this environment")
    assert out["ok"], out
    data = tmp_path / "lines.txt"
    data.write_text("5.0\n5.5\n5.0\n", encoding="utf-8")
    first = cli(home, "ingest", "--instance", "t", "--ext", "lines", "--input", str(data))["data"]
    assert first["counts"] == {"accepted": 3, "duplicate": 0, "rejected": 0}
    again = cli(home, "ingest", "--instance", "t", "--ext", "lines", "--input", str(data))["data"]
    assert again["counts"] == {"accepted": 0, "duplicate": 3, "rejected": 0}
    assert [e["payload"]["origin"]["kind"] for e in naive_events(home)] == ["extension"] * 3

    bad = _extension(tmp_path, "peek", GOOD.replace("import json", "import json\nimport plsi.life.index"))
    refused = cli(home, "ext", "install", "--dir", str(bad))
    assert not refused["ok"] and "may not import plsi.life.index" in json.dumps(refused)
