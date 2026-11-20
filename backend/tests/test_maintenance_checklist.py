"""``app.services.maintenance_checklist`` -- what a maintenance checklist says, and what a
completion leaves undone.

Pure functions, so pure tests. The reader must agree with the Maintenance page's
``parseChecklistResults`` (``frontend/src/utils/maintenance.ts``): the completion dialog asks for
a reason exactly when IT sees an unticked item, so a server that sees a different list either
refuses a completion the page thinks is clean or lets an unticked one through without a reason.
"""

import json

import pytest

from app.services.maintenance_checklist import (
    ChecklistEntry,
    checklist_item_key,
    describe_items,
    normalize_checklist_item,
    parse_checklist,
    parse_checklist_results,
    removed_checklist_items,
    removed_items,
    unticked_checklist_items,
)

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
        would also split here, and the server would count two items the dialog shows as one. The lone
        ``\\r`` is whitespace, so the normalizer collapses it to one space: still ONE item."""
        assert parse_checklist_results("Drain tank\rCheck belt") == [ChecklistEntry("Drain tank Check belt", False)]

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

    def test_matching_ignores_case(self):
        """Owner decision 2026-11-20: items match on their normalized text CASE-INSENSITIVELY, so
        "drain tank" ticks "Drain tank" -- one item, ticked."""
        stored = checklist(("Drain tank", False))
        completed = checklist(("drain tank", True))
        assert unticked_checklist_items(stored, completed) == ([], 1)

    def test_a_real_rename_is_still_a_different_item(self):
        """Case and spacing are not a rename; different words are: the old item is dropped
        (unticked) and the new one counts on its own."""
        stored = checklist(("Drain tank", False))
        completed = checklist(("Drain tanks", True))
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


class TestParityWithThePagesReader:
    """``String(value ?? '').trim()`` and the page's handling of a nested array."""

    def test_booleans_read_as_the_page_writes_them(self):
        assert [e.item for e in parse_checklist_results("[true, false]")] == ["true", "false"]

    def test_a_whole_float_reads_without_its_decimal(self):
        assert [e.item for e in parse_checklist_results("[1.0, 2.5]")] == ["1", "2.5"]

    def test_a_nested_array_entry_is_dropped(self):
        assert parse_checklist_results('[[1, 2], "Check belt"]') == [ChecklistEntry("Check belt", False)]


class TestRequiredItems:
    """``required``: a PM schedule's checklist, judged whatever the work order's says."""

    def test_a_required_item_the_work_order_lacks_is_unticked(self):
        assert unticked_checklist_items(
            checklist(("Lubricate ways", True)),
            checklist(("Lubricate ways", True)),
            ["Lubricate ways", "Check coolant"],
        ) == (["Check coolant"], 2)

    def test_a_required_item_ticked_on_the_work_order_is_done(self):
        done = checklist(("Lubricate ways", True), ("Check coolant", True))
        assert unticked_checklist_items(done, done, ["Lubricate ways", "Check coolant"]) == ([], 2)

    def test_required_copies_count_as_a_multiset(self):
        assert unticked_checklist_items(
            None, checklist(("Grease bearing", True)), ["Grease bearing", "Grease bearing"]
        ) == (["Grease bearing"], 2)

    def test_blank_required_items_are_ignored(self):
        assert unticked_checklist_items(None, None, ["", "Check belt"]) == (["Check belt"], 1)


class TestRemovedChecklistItems:
    def test_ticking_unticking_or_adding_removes_nothing(self):
        stored = checklist(("A", False), ("B", True))
        assert removed_checklist_items(stored, checklist(("A", True), ("B", False), ("C", False))) == []

    @pytest.mark.parametrize("new", [None, "", "[]"])
    def test_clearing_removes_every_item(self, new):
        assert removed_checklist_items(checklist(("A", False), ("B", False)), new) == ["A", "B"]

    def test_a_rename_removes_the_old_text(self):
        assert removed_checklist_items(checklist(("Check belt", False)), checklist(("Check belts", False))) == [
            "Check belt"
        ]

    def test_one_copy_of_a_repeated_item_removed_counts(self):
        stored = checklist(("Grease bearing", False), ("Grease bearing", False))
        assert removed_checklist_items(stored, checklist(("Grease bearing", False))) == ["Grease bearing"]


class TestRemovedItemsAndDescribe:
    def test_removed_items_is_a_multiset_difference(self):
        assert removed_items(["A", "B", "B"], ["B", "C"]) == ["A", "B"]
        assert removed_items(["A"], ["A", "A"]) == []

    def test_describe_items_summarizes_a_long_list(self):
        assert describe_items(["a", "b"]) == "a; b"
        assert describe_items([str(n) for n in range(12)], limit=10) == "0; 1; 2; 3; 4; 5; 6; 7; 8; 9; and 2 more"


class TestItemsAreNormalizedOnEveryRead:
    """Owner decision 2026-11-20: every reader normalizes item text (whitespace runs, leading bullets)
    and items MATCH case-insensitively, keeping the first spelling for display. Spelling, never
    meaning. The per-string contract is the shared fixture (test_maintenance_checklist_normalization_
    parity); these pin what the readers and matchers do with it."""

    def test_the_schedule_reader_agrees_across_json_objects_and_lines(self):
        expected = ["Check oil", "Grease bearing", "1. Drain tank", "-40 F cold start"]
        as_strings = json.dumps(["- Check oil", "Grease\u00a0 bearing", "1. Drain tank", "-40 F cold start"])
        as_objects = json.dumps(
            [
                {"item": "\u2022 Check oil"},
                {"task": "Grease  bearing"},
                {"name": "1. Drain tank"},
                {"text": "-40 F cold start"},
            ]
        )
        as_lines = "- Check oil\r\n  Grease\tbearing\n\n* 1. Drain tank\n-40 F cold start\n"
        assert parse_checklist(as_strings) == parse_checklist(as_objects) == parse_checklist(as_lines) == expected

    def test_the_work_order_reader_agrees_with_the_schedule_reader(self):
        raw = json.dumps([{"item": "- Check  oil", "done": True}, "\u2022 Grease bearing"])
        assert parse_checklist_results(raw) == [
            ChecklistEntry("Check oil", True),
            ChecklistEntry("Grease bearing", False),
        ]
        assert [
            entry.item for entry in parse_checklist_results("- Check  oil\n\u2022 Grease bearing")
        ] == parse_checklist("- Check  oil\n\u2022 Grease bearing")

    @pytest.mark.parametrize(
        "raw,item",
        [
            ("1. Check oil", "1. Check oil"),
            ("2) Grease bearing", "2) Grease bearing"),
            ("-40 F cold start", "-40 F cold start"),
            ("- -40 F cold start", "-40 F cold start"),
            ("Check oil - both sides", "Check oil - both sides"),
        ],
    )
    def test_numbering_and_a_signed_number_are_kept(self, raw, item):
        assert normalize_checklist_item(raw) == item
        assert parse_checklist(json.dumps([raw])) == [item]
        assert [entry.item for entry in parse_checklist_results(json.dumps([raw]))] == [item]

    def test_case_is_kept_for_display_and_ignored_for_matching(self):
        assert normalize_checklist_item("CHECK OIL") == "CHECK OIL"
        assert checklist_item_key("CHECK OIL") == checklist_item_key("- check  oil") == "check oil"

    def test_a_case_and_spacing_variant_ticks_the_step(self):
        stored = checklist(("Check Oil", False))
        completed = checklist(("- check  OIL", True))
        assert unticked_checklist_items(stored, completed) == ([], 1)
        assert unticked_checklist_items(None, completed, ["\u2022 CHECK oil"]) == ([], 1)

    def test_the_multiset_holds_after_normalization(self):
        """Two copies of one step, spelled two ways, one ticked: still one unticked copy."""
        completed = checklist(("Grease bearing", True), ("- grease  BEARING", False))
        assert unticked_checklist_items(completed, completed) == (["Grease bearing"], 2)
        assert unticked_checklist_items(
            None, checklist(("grease bearing", True)), ["Grease bearing", "GREASE BEARING"]
        ) == (
            ["grease bearing"],
            2,
        )

    def test_the_first_spelling_met_is_the_one_displayed(self):
        """The completed checklist's spelling, then the stored one's, then the required list's."""
        assert unticked_checklist_items(checklist(("CHECK OIL", False)), None, ["check oil"]) == (["CHECK OIL"], 1)
        assert unticked_checklist_items(
            checklist(("CHECK OIL", False)), checklist(("Check Oil", False)), ["check oil"]
        ) == (["Check Oil"], 1)
        assert unticked_checklist_items(None, None, ["- check  oil", "CHECK OIL"]) == (["check oil", "check oil"], 2)

    def test_removed_items_ignores_spacing_bullets_and_case(self):
        before = ["Check oil", "Grease bearing", "Drain tank"]
        assert removed_items(before, ["- check  OIL", "\u2022 GREASE bearing", "drain\u00a0tank"]) == []
        # A real removal is still one, named as it was spelled before.
        assert removed_items(before, ["CHECK OIL", "grease bearing"]) == ["Drain tank"]
        # ...and the multiset still counts copies.
        assert removed_items(["Grease bearing", "Grease bearing"], ["GREASE  BEARING"]) == ["Grease bearing"]

    def test_removed_checklist_items_ignores_spacing_bullets_and_case(self):
        stored = checklist(("Check oil", False), ("Grease bearing", True))
        assert removed_checklist_items(stored, checklist(("- CHECK  oil", True), ("grease bearing", False))) == []

    def test_a_legacy_stored_row_matches_as_it_is(self):
        """Rows stored before normalization (stray spacing, bullets, other case) are read through the
        normalizer: they match the normalized steps with no rewrite of the stored text."""
        legacy_schedule = "  - Check   oil \n\u2022Grease bearing\n"
        legacy_work_order = json.dumps(
            [{"item": "  check OIL ", "done": True}, {"item": "* grease bearing", "done": True}]
        )
        assert unticked_checklist_items(legacy_work_order, legacy_work_order, parse_checklist(legacy_schedule)) == (
            [],
            2,
        )
        assert removed_items(parse_checklist(legacy_schedule), ["Check oil", "Grease bearing"]) == []
