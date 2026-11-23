#!/usr/bin/env python3
from __future__ import annotations

import check
import import_claude
import render_codex


def main() -> int:
    import_claude.main()
    render_codex.main()
    return check.main()


if __name__ == "__main__":
    raise SystemExit(main())
