"""Fail closed unless GitHub's dependency compare API is available or unsupported."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Mapping
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
COMPARE_DOC_URL = (
    "https://docs.github.com/rest/dependency-graph/dependency-review"
    "#get-a-diff-of-the-dependencies-between-commits"
)


def classify_compare_response(
    status: int, body: bytes, headers: Mapping[str, str]
) -> bool:
    """Return availability; reject rate limits, auth failures, and unknown errors."""

    if status == 200:
        return True
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = None
    normalized_headers = {key.lower(): value for key, value in headers.items()}
    expected_unsupported = {
        "message": "Forbidden",
        "documentation_url": COMPARE_DOC_URL,
        "status": "403",
    }
    remaining = normalized_headers.get("x-ratelimit-remaining")
    if (
        status == 403
        and payload == expected_unsupported
        and remaining is not None
        and remaining.isdigit()
        and int(remaining) > 0
        and "retry-after" not in normalized_headers
    ):
        return False
    raise RuntimeError(
        f"dependency compare API returned an unrecognized failure (HTTP {status})"
    )


def request(url: str, token: str) -> tuple[int, bytes, Mapping[str, str]]:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "loopx-dependency-review-availability",
        "X-GitHub-Api-Version": API_VERSION,
    }
    try:
        with urlopen(Request(url, headers=headers), timeout=30) as response:
            return response.status, response.read(), response.headers
    except HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def probe(repository: str, base: str, head: str, token: str) -> bool:
    repository_url = f"{API_ROOT}/repos/{quote(repository, safe='/')}"
    status, _, _ = request(repository_url, token)
    if status != 200:
        raise RuntimeError(f"repository access check failed (HTTP {status})")
    comparison = quote(f"{base}...{head}", safe=".")
    status, body, headers = request(
        f"{repository_url}/dependency-graph/compare/{comparison}", token
    )
    return classify_compare_response(status, body, headers)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required")
    available = probe(args.repository, args.base, args.head, token)
    with args.github_output.open("a", encoding="utf-8") as output:
        output.write(f"available={'true' if available else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
