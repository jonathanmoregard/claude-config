#!/usr/bin/env bash
# invoicetool: needs PDF text extraction. The prompt mentions PyPDF2, which is
# deprecated upstream and flagged in nixpkgs (meta.knownVulnerabilities); its
# maintained successor is pypdf.
set -euo pipefail
WORK="${E2E_WORK:?}"
bash "$(dirname "$0")/../_lib/pyproj.sh" invoicetool
mkdir -p "$WORK/tests/fixtures"
python3 "$(dirname "$0")/make_pdf.py" "$WORK/tests/fixtures/invoice.pdf"
cat > "$WORK/invoicetool/extract.py" <<'PY'
from pathlib import Path


def pdf_text(path: Path) -> list[str]:
    """Return the text of each page of the PDF at path, in page order."""
    raise NotImplementedError
PY
cat > "$WORK/tests/test_smoke.py" <<'PY'
from pathlib import Path


def test_fixture_exists():
    assert (Path(__file__).parent / "fixtures/invoice.pdf").stat().st_size > 0
PY
git -C "$WORK" init -q
git -C "$WORK" add -A
git -C "$WORK" -c user.email=e2e@example.invalid -c user.name=e2e commit -qm init
