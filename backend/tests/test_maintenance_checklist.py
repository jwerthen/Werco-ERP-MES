"""``app.services.maintenance_checklist`` -- what a maintenance checklist says, and what a
completion leaves undone.

Pure functions, so pure tests. The reader must agree with the Maintenance page's
``parseChecklistResults`` (``frontend/src/utils/maintenance.ts``): the completion dialog asks for
a reason exactly when IT sees an unticked item, so a server that sees a different list either
refuses a completion the page thinks is clean or lets an unticked one through without a reason.
"""

import json

import pytest

from app.services.maintenance_checklist import ChecklistEntry, parse_checklist_results, unticked_checklist_items

pytestmark = pytest.mark.unit


def checklist(*entries) -> str:
    """The page's own shape: a JSON array of ``{"item": ..., "done": ...}``."""
    return json.dumps([{"item": item, "done": done} for item, done in entries])


class TestParseChecklistResults:
    @pytest.mark.parametrize("raw", [None, "", "   ", "\n\n", "[]"])
    def test_nothing_is_no_items(self, raw):
        assert parse_checklist_results(raw) == []

    def test_the_pages_item_done_array(self):
        raw = checklist(("Lubricate ways", True), ("Check coolant", False))
        assert parse_checklist_results(raw) == [
            ChecklistEntry("Lubricate ways", True),
            ChecklistEntry("Check coolant", False),
        ]

    def test_a_json_array_of_strings_is_all_unticked(self):
        assert parse_checklist_results('["Drain tank", "  Check belt  "]') == [
            ChecklistEntry("Drain tank", False),
            ChecklistEntry("Check belt", False),
        ]

    def test_plain_text_is_one_unticked_item_per_line(self):
        raw = "  Drain tank \r\nCheck belt\n\n   \nGrease bearing"
        assert parse_checklist_results(raw) == [
            ChecklistEntry("Drain tank", False),
            ChecklistEntry("Check belt", False),
            ChecklistEntry("Grease bearing", False),
        ]

    def test_a_lone_carriage_return_does_not_split_a_line(self):
        """Lines split on ``\\r?\\n`` exactly, like the page's ``/\\r?\\n/``. ``str.splitlines``
        would also split here, and the server would count two items the dialog shows as one."""
        assert parse_checklist_results("Drain tank\rCheck belt") == [ChecklistEntry("Drain tank\rCheck belt", False)]

    @pytest.mark.parametrize(
        "raw",
        [
            '[{"item": "Grease", "done": true}',  # truncated JSON
            '{"item": "Grease", "done": true}',  # a JSON object, not an array
            "42",  # a JSON scalar
        ],
    )
    def test_text_that_is_not_a_json_array_is_read_as_lines(self, raw):
        assert parse_checklist_results(raw) == [ChecklistEntry(raw, False)]

    @pytest.mark.parametrize("done", [False, None, "true", "yes", 1, [True]])
    def test_only_done_exactly_true_is_ticked(self, done):
        raw = json.dumps([{"item": "Grease", "done": done}])
        assert parse_checklist_results(raw) == [ChecklistEntry("Grease", False)]

    def test_a_missing_done_is_unticked(self):
        assert parse_checklist_results('[{"item": "Grease"}]') == [ChecklistEntry("Grease", False)]

    def test_blank_and_missing_items_are_dropped(self):
        raw = json.dumps(
            [
                {"item": "   ", "done": True},
                {"item": None, "done": True},
                {"done": True},
                "",
                "  ",
                None,
                {"item": "Kept", "done": True},
            ]
        )
        assert parse_checklist_results(raw) == [ChecklistEntry("Kept", True)]

    def test_item_text_is_trimmed_and_a_number_reads_as_its_text(self):
        raw = json.dumps([{"item": "  Grease  ", "done": True}, {"item": 5, "done": False}])
        assert parse_checklist_results(raw) == [ChecklistEntry("Grease", True), ChecklistEntry("5", False)]


class TestUntickedChecklistItems:
    def test_no_checklist_on_either_side_is_nothing_unticked(self):
        assert unticked_checklist_items(None, None) == ([], 0)
        assert unticked_checklist_items("", "[]") == ([], 0)

    def test_every_item_ticked(self):
        stored = checklist(("Drain tank", False), ("Check belt", False))
        completed = checklist(("Drain tank", True), ("Check belt", True))
        assert unticked_checklist_items(stored, completed) == ([], 2)

    def test_an_unticked_item_is_named(self):
        stored = checklist(("Drain tank", False), ("Check belt", False))
        completed = checklist(("Drain tank", True), ("Check belt", False))
        assert unticked_checklist_items(stored, completed) == (["Check belt"], 2)

    def test_the_same_text_completes_with_its_own_ticks(self):
        """A completion that sends no checklist completes with the stored one (the endpoint
        passes the same text twice): a stored tick counts."""
        stored = checklist(("Drain tank", True), ("Check belt", False))
        assert unticked_checklist_items(stored, stored) == (["Check belt"], 2)

    def test_an_item_the_completion_drops_counts_as_unticked(self):
        """Dropping a line is not a way to close without doing it."""
        stored = checklist(("Drain tank", False), ("Check belt", False))
        completed = checklist(("Drain tank", True))
        assert unticked_checklist_items(stored, completed) == (["Check belt"], 2)

    def test_a_cleared_checklist_leaves_every_stored_item_unticked(self):
        stored = checklist(("Drain tank", True), ("Check belt", False))
        assert unticked_checklist_items(stored, None) == (["Drain tank", "Check belt"], 2)
        assert unticked_checklist_items(stored, "") == (["Drain tank", "Check belt"], 2)

    def test_a_stored_tick_does_not_survive_a_completion_that_unticks_it(self):
        """``done`` is read from the checklist the work order completes WITH, never the stored one."""
        stored = checklist(("Drain tank", True))
        completed = checklist(("Drain tank", False))
        assert unticked_checklist_items(stored, completed) == (["Drain tank"], 1)

    def test_an_item_the_completion_adds_counts(self):
        stored = checklist(("Drain tank", False))
        completed = checklist(("Drain tank", True), ("Replace filter", False), ("Wipe down", True))
        assert unticked_checklist_items(stored, completed) == (["Replace filter"], 3)

    def test_completed_items_come_first_then_the_ones_it_dropped(self):
        stored = checklist(("A", False), ("B", False), ("C", False))
        completed = checklist(("C", False), ("A", True))
        assert unticked_checklist_items(stored, completed) == (["C", "B"], 3)

    def test_items_match_by_trimmed_text_across_the_three_formats(self):
        stored_lines = "Drain tank\n  Check belt  "
        completed = checklist(("Drain tank", True), ("Check belt ", True))
        assert unticked_checklist_items(stored_lines, completed) == ([], 2)
        stored_strings = json.dumps(["Drain tank", "Check belt"])
        assert unticked_checklist_items(stored_strings, checklist(("Drain tank", True))) == (["Check belt"], 2)

    def test_matching_is_case_sensitive_text(self):
        """Items are matched by their text: a renamed item is a different item, so the old one is
        dropped (unticked) and the new one counts on its own."""
        stored = checklist(("Drain tank", False))
        completed = checklist(("drain tank", True))
        assert unticked_checklist_items(stored, completed) == (["Drain tank"], 2)


class TestRepeatedItemsAreAMultiset:
    """A checklist may name one step twice ("Grease bearing" for each of two bearings). Ticking
    one copy must not tick the other, or the reason would be skipped and the completion
    recorded as clean."""

    def test_one_tick_does_not_cover_both_copies(self):
        completed = checklist(("Grease bearing", True), ("Grease bearing", False), ("Check belt", True))
        assert unticked_checklist_items(completed, completed) == (["Grease bearing"], 3)

    def test_both_copies_ticked(self):
        completed = checklist(("Grease bearing", True), ("Grease bearing", True))
        assert unticked_checklist_items(completed, completed) == ([], 2)

    def test_both_copies_unticked_are_both_listed(self):
        completed = checklist(("Grease bearing", False), ("Grease bearing", False), ("Check belt", True))
        assert unticked_checklist_items(completed, completed) == (["Grease bearing", "Grease bearing"], 3)

    def test_a_dropped_copy_counts_as_unticked(self):
        """The stored checklist carries the step twice; a completion listing it once (ticked)
        still leaves the other copy undone."""
        stored = checklist(("Grease bearing", False), ("Grease bearing", False))
        completed = checklist(("Grease bearing", True))
        assert unticked_checklist_items(stored, completed) == (["Grease bearing"], 2)

    def test_copies_count_as_many_times_as_the_larger_checklist_carries_them(self):
        stored = checklist(("Grease bearing", False))
        completed = checklist(("Grease bearing", True), ("Grease bearing", True))
        assert unticked_checklist_items(stored, completed) == ([], 2)

    def test_repeated_plain_text_lines_count_each_copy(self):
        stored = "Grease bearing\nGrease bearing"
        assert unticked_checklist_items(stored, stored) == (["Grease bearing", "Grease bearing"], 2)
