from __future__ import annotations

import json
import unittest

from dependency_review_availability import COMPARE_DOC_URL, classify_compare_response


def unsupported_body(**overrides: str) -> bytes:
    payload = {
        "message": "Forbidden",
        "documentation_url": COMPARE_DOC_URL,
        "status": "403",
        **overrides,
    }
    return json.dumps(payload).encode()


class DependencyReviewAvailabilityTests(unittest.TestCase):
    def test_available_compare_runs_dependency_review(self) -> None:
        self.assertTrue(classify_compare_response(200, b"{}", {}))

    def test_recognized_unsupported_response_skips_dependency_review(self) -> None:
        self.assertFalse(
            classify_compare_response(
                403, unsupported_body(), {"X-RateLimit-Remaining": "42"}
            )
        )

    def test_every_other_failure_fails_closed(self) -> None:
        cases = (
            (401, unsupported_body(), {"X-RateLimit-Remaining": "42"}),
            (403, unsupported_body(message="Resource not accessible"), {"X-RateLimit-Remaining": "42"}),
            (403, unsupported_body(), {"X-RateLimit-Remaining": "0"}),
            (403, unsupported_body(), {"X-RateLimit-Remaining": "42", "Retry-After": "60"}),
            (403, b"not json", {"X-RateLimit-Remaining": "42"}),
            (500, b"{}", {}),
        )
        for status, body, headers in cases:
            with self.subTest(status=status, body=body, headers=headers):
                with self.assertRaises(RuntimeError):
                    classify_compare_response(status, body, headers)


if __name__ == "__main__":
    unittest.main()
