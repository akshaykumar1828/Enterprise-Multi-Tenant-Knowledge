"""Unit tests for the extractive document descriptions shown in the document list (no database, no models).

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_descriptions -v
"""

import unittest

from src.rag.descriptions import MAX_LENGTH, describe, merge_passages


class DescribeTests(unittest.TestCase):
    def test_skips_title_headings_and_labels(self):
        text = ("Restoration Liaison Playbook\n\nSummary:\nThis playbook defines the role of the Restoration Liaison "
                "during incidents. It also covers status updates.\n")
        self.assertEqual(describe(text, "confluence"),
                         "This playbook defines the role of the Restoration Liaison during incidents. It also covers status updates.")

    def test_inline_label_is_removed_but_text_kept(self):
        self.assertEqual(describe("Weekly pulse\nSummary: Quick sync on the bench results for the pilot customer.", "fireflies"),
                         "Quick sync on the bench results for the pilot customer.")

    def test_email_headers_and_greetings_are_skipped(self):
        text = ("Re: Renewal timing\n\nFrom: A <a@x.test>\nTo: b@redwood.com\nDate: Mon, 1 Jun 2026\nSubject: Renewal\n\n"
                "Hi Diego,\n\nThanks again for the call. Our audit team needs verification that the tenant was deleted on time.")
        self.assertEqual(describe(text, "gmail"), "Our audit team needs verification that the tenant was deleted on time.")

    def test_metadata_sections_are_skipped(self):
        text = "ADR-0172: Metrics\n## Status\nPublished (v1).\n## Decision summary\nA canonical set of serving metrics with strict boundaries."
        self.assertEqual(describe(text, "confluence"), "A canonical set of serving metrics with strict boundaries.")

    def test_slack_names_the_channel_and_drops_speakers(self):
        text = "incidents\n\nalex: Quick follow-up from today's postmortem — we need a short customer-facing summary.\nmaria: I can draft it."
        # The first message is short, so the second is added too; speaker names never appear.
        self.assertEqual(describe(text, "slack"),
                         "Discussion in #incidents: Quick follow-up from today's postmortem — we need a short customer-facing "
                         "summary. I can draft it.")

    def test_never_exposes_benchmark_ids(self):
        text = ("dsid_12d7160760e846bf92203f4d7009f606\n\n"
                "emma: Heads up - procurement asked for an assurance bundle for their pilot, see dsid_0123456789abcdef0123456789abcdef.")
        description = describe(text, "slack")
        self.assertIsNotNone(description)
        self.assertNotIn("dsid_", description)
        self.assertFalse(description.startswith("Discussion in #dsid"))

    def test_long_text_is_capped_at_a_word_boundary(self):
        text = "Title\n" + "This sentence keeps going with many words and no full stop " * 10
        description = describe(text)
        self.assertLessEqual(len(description), MAX_LENGTH)
        self.assertTrue(description.endswith("…"))
        self.assertFalse(description[:-1].endswith(" "))

    def test_nothing_useful_gives_none(self):
        self.assertIsNone(describe(None))
        self.assertIsNone(describe(""))
        self.assertIsNone(describe("Only a title"))
        self.assertIsNone(describe("Title\n## Heading\nFrom: someone@example.test"))

    def test_overlapping_passages_are_joined_without_repeating_text(self):
        first = "Title\nThe pipeline exports audit logs for every tenant and keeps them for ninety days"
        second = "keeps them for ninety days before deleting them safely."
        self.assertEqual(merge_passages([first, second]),
                         "Title\nThe pipeline exports audit logs for every tenant and keeps them for ninety days"
                         " before deleting them safely.")
        self.assertEqual(describe([first, second]),
                         "The pipeline exports audit logs for every tenant and keeps them for ninety days before deleting them safely.")


if __name__ == "__main__":
    unittest.main()
