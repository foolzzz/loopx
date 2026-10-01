#!/usr/bin/env python3
"""Reject retired App-host guidance in active onboarding documentation."""

from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from examples.support.active_host_doc_guard import assert_active_host_docs  # noqa: E402


def main() -> int:
    assert_active_host_docs(REPO_ROOT)
    print("docs-active-host-guidance-smoke ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
