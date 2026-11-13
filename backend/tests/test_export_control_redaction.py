"""The export-control redaction engine, pure (Claude-plugin brief P0.3).

``app.services.export_control_redaction`` takes a decoded JSON response and an index and
returns what an AI-external token may see. No database here: every index is built by hand.
Pinned: the keep-allowlist, descendants, route shapes, path parameters, the residual text
pass (cleared records, error details, object keys), fail-closed ids, hit deduplication, and
that the input is never mutated.
"""

from __future__ import annotations

import copy

import pytest

from app.core.export_control import REDACTED_MARKER_KEY, REDACTED_VALUE
from app.services.export_control_redaction import (
    MIN_SENSITIVE_TOKEN_LENGTH,
    UNLINKABLE,
    Control,
    ControlledIndex,
    compile_sensitive_pattern,
    customer_control,
    normalize_key,
    redact_payload,
    scrub_text,
    worst,
)

pytestmark = pytest.mark.unit

ITAR = customer_control("itar_export_controlled", 7, 70)
CUI = customer_control("cui", 8, 80)
R = REDACTED_VALUE


def _index(**overrides) -> ControlledIndex:
    """WO 1 (ITAR), WO 2 (cleared), WO 3 (unlinkable); part 10 ITAR, part 11 cleared; CAR 5 ITAR."""
    base = dict(
        company_id=1,
        controlled={
            "work_order": {1: ITAR, 3: UNLINKABLE},
            "part": {10: ITAR},
            "car": {5: ITAR},
            "customer": {7: ITAR},
            "operation": {},
            "ncr": {},
        },
        known={
            "work_order": frozenset({1, 2, 3}),
            "part": frozenset({10, 11}),
            "car": frozenset({5, 6}),
            "customer": frozenset({7, 9}),
            "operation": frozenset({100, 101}),
            "ncr": frozenset({20}),
        },
        numbers={"work_order": {"wo-1": 1, "wo-3": 3}, "part": {"pn-secret-10": 10, "alias-10": 10}},
        customer_names={"acme defense": (7, ITAR), "acmed": (7, ITAR)},
        sensitive_tokens=("PN-SECRET-10", "ALIAS-10", "Acme Defense", "ACMED"),
    )
    base.update(overrides)
    return ControlledIndex(**base)


class TestKeepAllowlist:
    def test_a_controlled_object_keeps_only_structural_values(self):
        record = {
            "id": 1,
            "work_order_id": 1,
            "work_order_number": "WO-1",
            "operation_number": "10",
            "lot_number": "LOT-9",
            "work_center_name": "Brake",
            "status": "in_progress",
            "inspection_status": "pending",
            "priority": 2,
            "sequence": 10,
            "version": 3,
            "due_date": "2026-11-20",
            "created_at": "2026-11-13T10:00:00Z",
            "clock_in_time": "08:00",
            "quantity_ordered": 7,
            "quantity_scrapped": "1.500",
            "is_active": True,
            "notes": "anything at all",
            "description": "the bracket",
            "part_number": "123456",
            "customer_part_number": "42",
            "external_ref_id": "PN-SECRET-10",
            "missing": None,
        }
        out = redact_payload(record, _index()).payload
        assert out[REDACTED_MARKER_KEY] is True
        for key in (
            "id",
            "work_order_id",
            "work_order_number",
            "operation_number",
            "lot_number",
            "work_center_name",
            "status",
            "inspection_status",
            "priority",
            "sequence",
            "version",
            "due_date",
            "created_at",
            "clock_in_time",
            "quantity_ordered",
            "quantity_scrapped",
            "is_active",
            "missing",
        ):
            assert out[key] == record[key], key
        assert out["notes"] == R and out["description"] == R
        assert out["part_number"] == R, "a purely numeric part number is still text"
        assert out["customer_part_number"] == R
        assert out["external_ref_id"] == R, "an *_id key keeps only id-shaped values"

    def test_descendants_of_a_controlled_object_are_redacted(self):
        record = {
            "id": 1,
            "work_order_number": "WO-1",
            "operations": [{"id": 100, "name": "Weld out", "setup_instructions": "torque 40", "status": "ready"}],
            "part": {"id": 11, "name": "cleared part, but inside a controlled job"},
            "tags": ["one", "two"],
        }
        out = redact_payload(record, _index()).payload
        op = out["operations"][0]
        assert op == {"id": 100, "name": R, "setup_instructions": R, "status": "ready", REDACTED_MARKER_KEY: True}
        assert out["part"] == {"id": 11, "name": R, REDACTED_MARKER_KEY: True}
        assert out["tags"] == [R, R]

    def test_a_cleared_object_passes_untouched(self):
        record = {"id": 2, "work_order_id": 2, "part_id": 11, "notes": "plain words", "customer_name": "Open Co"}
        out = redact_payload(record, _index()).payload
        assert out == record
        assert REDACTED_MARKER_KEY not in out

    def test_the_input_is_never_mutated(self):
        payload = [{"id": 1, "work_order_id": 1, "notes": "PN-SECRET-10"}, {"id": 2, "notes": "PN-SECRET-10"}]
        before = copy.deepcopy(payload)
        redact_payload(payload, _index())
        scrub_text(payload, _index())
        assert payload == before


class TestIdentification:
    @pytest.mark.parametrize(
        "record,expected",
        [
            ({"work_order_id": 1}, ("work_order", 1)),
            ({"parent_work_order_id": 1}, ("work_order", 1)),
            ({"component_part_id": 10}, ("part", 10)),
            ({"work_order_number": "  wo-1 "}, ("work_order", 1)),
            ({"part_number": "ALIAS-10"}, ("part", 10)),
            ({"customer_name": "ACME   defense"}, ("customer", 7)),
            ({"customer": "acmed"}, ("customer", 7)),
            ({"car_id": 5}, ("car", 5)),
        ],
    )
    def test_each_identifying_key_hits(self, record, expected):
        result = redact_payload({"text": "x", **record}, _index())
        assert result.payload["text"] == R
        assert [(hit.record_type, hit.record_id) for hit in result.hits] == [expected]

    def test_an_id_the_index_never_saw_fails_closed(self):
        result = redact_payload({"work_order_id": 999, "notes": "x"}, _index())
        assert result.payload["notes"] == R
        assert result.hits[0].control is UNLINKABLE

    def test_an_unindexed_record_type_and_an_unknown_number_are_no_opinion(self):
        out = redact_payload({"vendor_id": 999, "part_number": "never-seen", "notes": "x"}, _index()).payload
        assert out["notes"] == "x"

    def test_the_worst_control_wins(self):
        result = redact_payload({"work_order_id": 3, "part_id": 10, "notes": "x"}, _index())
        assert {hit.control for hit in result.hits} == {UNLINKABLE, ITAR}
        assert worst(UNLINKABLE, CUI, ITAR) is ITAR
        assert worst(None, UNLINKABLE) is UNLINKABLE
        assert worst(None, None) is None

    def test_hits_are_deduplicated(self):
        payload = [{"work_order_id": 1}, {"work_order_id": 1, "work_order_number": "WO-1"}, {"part_id": 10}]
        hits = redact_payload(payload, _index()).hits
        assert sorted((hit.record_type, hit.record_id) for hit in hits) == [("part", 10), ("work_order", 1)]

    def test_a_route_shape_identifies_records_by_id(self):
        """CAR responses carry no FK key at all: only the route says ``id`` is a CAR."""
        payload = [{"id": 5, "title": "secret CAR"}, {"id": 6, "title": "plain CAR"}]
        assert redact_payload(payload, _index()).payload[0]["title"] == "secret CAR", "no shape, no opinion"
        out = redact_payload(payload, _index(), route_path="/quality/car").payload
        assert out[0]["title"] == R and out[1]["title"] == "plain CAR"

    def test_a_wrapper_applies_the_shape_only_to_its_record_list(self):
        payload = {"operations": [{"id": 100, "name": "a"}, {"id": 777, "name": "b"}], "notices": [{"id": 777}]}
        out = redact_payload(payload, _index(), route_path="/shop-floor/operations").payload
        assert out["operations"][0]["name"] == "a"
        assert out["operations"][1]["name"] == R, "an operation the index never saw"
        assert REDACTED_MARKER_KEY not in out["notices"][0], "not a record list: no shape"

    def test_a_path_parameter_controls_the_whole_response(self):
        payload = {"events": [{"label": "started", "actor": "Pat"}], "next_cursor": None}
        out = redact_payload(payload, _index(), path_params={"work_order_id": "1"}).payload
        assert out["events"][0]["label"] == R and out[REDACTED_MARKER_KEY] is True
        cleared = redact_payload(payload, _index(), path_params={"work_order_id": "2"}).payload
        assert cleared == payload
        by_number = redact_payload(payload, _index(), path_params={"wo_number": "WO-1"}).payload
        assert by_number["events"][0]["label"] == R
        maintenance = redact_payload(payload, _index(), path_params={"wo_id": "1"}).payload
        assert maintenance == payload, "wo_id is a MAINTENANCE work order, not a production one"


class TestResidualTextPass:
    def test_tokens_are_removed_from_cleared_strings_case_insensitively(self):
        out = redact_payload({"id": 2, "notes": "ran pn-secret-10 for ACME DEFENSE"}, _index()).payload
        assert out["notes"] == f"ran {R} for {R}"

    def test_error_details_are_scrubbed(self):
        assert scrub_text({"detail": "Part PN-SECRET-10 not found"}, _index()) == {"detail": f"Part {R} not found"}

    def test_object_keys_are_scrubbed_without_losing_values(self):
        out = scrub_text({"PN-SECRET-10": 3, f"{R}": 4, "other": 1}, _index())
        assert sorted(out.values()) == [1, 3, 4], "two keys collapsing into one must not drop a count"
        assert not any("SECRET" in key for key in out)

    def test_dates_numbers_and_ids_are_never_rewritten(self):
        index = _index(sensitive_tokens=("2026", "1500"))
        payload = {"id": 2, "due_date": "2026-11-13", "created_at": "2026-11-13T08:00:00Z", "quantity": "1500.0"}
        assert redact_payload(payload, index).payload == payload

    def test_short_tokens_are_ignored(self):
        assert MIN_SENSITIVE_TOKEN_LENGTH == 4
        assert compile_sensitive_pattern(["ABC", "  ", ""]) is None
        pattern = compile_sensitive_pattern(["ABCD", "ABCDEF"])
        assert pattern.sub("#", "xABCDEFx ABCD") == "x#x #", "longest token first"

    def test_no_tokens_no_pattern(self):
        index = _index(sensitive_tokens=())
        payload = {"id": 2, "notes": "PN-SECRET-10"}
        assert redact_payload(payload, index).payload == payload


def test_normalize_key():
    assert normalize_key("  ACME   Aero\t") == "acme aero"
    assert normalize_key(None) == "" and normalize_key("") == ""


def test_control_rank_orders_itar_over_cui_over_unlinkable():
    assert ITAR.rank > CUI.rank > UNLINKABLE.rank
    assert isinstance(UNLINKABLE, Control) and UNLINKABLE.reason == "unlinkable_record"
