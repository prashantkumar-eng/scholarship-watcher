import unittest

from watcher import (
    classify_line,
    extract_deadline,
    extract_explicit_deadline,
    find_event_title,
    format_scholarship_table,
    looks_like_error_page,
)


class WatcherFilterTests(unittest.TestCase):
    def test_accepts_actionable_scholarship(self):
        event = classify_line(
            "Merit Scholarship 2026 applications are now open until 30 July 2026"
        )
        self.assertIsNotNone(event)
        self.assertEqual(event[1], "Application window OPEN")
        self.assertEqual(event[2], 3)

    def test_rejects_unrelated_deadline(self):
        self.assertIsNone(classify_line("Tender deadline extended to 30 July 2026"))

    def test_rejects_recruitment_on_scholarship_page(self):
        self.assertIsNone(classify_line(
            "Recruitment applications are open",
            "https://example.org/scholarships",
        ))

    def test_detects_soft_404(self):
        self.assertTrue(looks_like_error_page(["404 Error - Page Not Found"]))

    def test_formats_requested_email_columns(self):
        text, html = format_scholarship_table([
            {
                "line": "Example Scholarship",
                "deadline": "2026-09-20",
                "source": "https://example.org/apply",
            }
        ])
        self.assertEqual(
            text,
            "ScholarshipTitle\tDeadline\tApplyLink\n"
            "Example Scholarship\t2026-09-20\thttps://example.org/apply",
        )
        self.assertIn("<th align='left'>ScholarshipTitle</th>", html)
        self.assertEqual(extract_deadline("Apply by 2026-09-20"), "2026-09-20")

    def test_joins_title_and_following_deadline(self):
        lines = [
            "Mahindra All India Talent",
            "Scholarship (MAITS)",
            "The applications for 2026-27 are now open. "
            "The deadline to apply for the scholarship is",
            "31st August 2026.",
        ]
        self.assertEqual(
            find_event_title(lines, 2, "Fallback"),
            "Mahindra All India Talent Scholarship (MAITS)",
        )
        self.assertEqual(
            extract_explicit_deadline(" ".join(lines[2:])),
            "2026-08-31",
        )


if __name__ == "__main__":
    unittest.main()
