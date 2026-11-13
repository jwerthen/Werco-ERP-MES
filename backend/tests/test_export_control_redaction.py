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

from app.core.export_control import REDACTED_MARKER_KEY, REDACTED_VALUE, ai_external_error_detail
from app.services.export_control_redaction import (
    MIN_SENSITIVE_TOKEN_LENGTH,
    UNLINKABLE,
    VALIDATION_ERROR_MESSAGE,
    Control,
    ControlledIndex,
    ai_external_error_body,
    compile_sensitive_pattern,
    customer_control,
    is_masked_lookup,
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


class TestNumbersInsideAWithheldRecord:
    """Compliance review P0, finding 1 (blocker): dimensions and measured values are technical data."""

    def test_numeric_strings_stay_only_under_count_quantity_money_and_duration_keys(self):
        record = {
            "work_order_id": 1,
            "nominal": "0.2500",
            "tolerance_plus": "0.0005",
            "tolerance_minus": "-0.0005",
            "actual_value": "0.2502",
            "required_value": "7.3291",
            "thickness": "0.125",
            "unknown_new_column": "42",
            "quantity_scrapped": "1.500",
            "quantity_affected": "2",
            "unit_price": "12.50",
            "line_total": "87.50",
            "standard_cost": "3.10",
            "setup_hours": "1.5",
            "lead_time_days": "10",
            "reorder_point": "5",
            "safety_stock": "2",
        }
        out = redact_payload(record, _index()).payload
        for key in ("nominal", "tolerance_plus", "tolerance_minus", "actual_value", "required_value", "thickness"):
            assert out[key] == R, key
        assert out["unknown_new_column"] == R, "an allowlist: an unknown key's number is withheld"
        for key in (
            "quantity_scrapped",
            "quantity_affected",
            "unit_price",
            "line_total",
            "standard_cost",
            "setup_hours",
            "lead_time_days",
            "reorder_point",
            "safety_stock",
        ):
            assert out[key] == record[key], key

    def test_json_numbers_stay_true_except_under_a_measurement_key(self):
        record = {
            "work_order_id": 1,
            "quantity_ordered": 7,
            "priority": 2,
            "actual_hours": 3.5,
            "target_quantity": 10,
            "char_number": 1,
            "total": 1234.5,
            "nominal": 0.25,
            "upper_limit": 0.2505,
            "measured_value": 0.2502,
            "diameter": 0.5,
            "weight": 1.2,
            "is_conforming": True,
        }
        out = redact_payload(record, _index()).payload
        for key in ("quantity_ordered", "priority", "actual_hours", "target_quantity", "char_number", "total"):
            assert out[key] == record[key], key
        for key in ("nominal", "upper_limit", "measured_value", "diameter", "weight"):
            assert out[key] == R, key
        assert out["is_conforming"] is True

    def test_a_cleared_record_keeps_its_dimensions(self):
        record = {"work_order_id": 2, "nominal": "0.2500", "diameter": 0.5}
        assert redact_payload(record, _index()).payload == record


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

    def test_a_nested_list_of_operations_is_classified_by_id(self):
        """Compliance review P0, finding 3: ``all_operations`` lists siblings by ``id`` alone."""
        index = _index(controlled={**_index().controlled, "operation": {101: UNLINKABLE}})
        payload = {
            "operation": {"id": 100, "name": "Form"},
            "all_operations": [{"id": 100, "name": "Form"}, {"id": 101, "name": "Laser ITAR widget", "sequence": 20}],
        }
        out = redact_payload(payload, index, route_path="/shop-floor/operations/{operation_id}").payload
        assert out["all_operations"][0] == {"id": 100, "name": "Form"}
        assert out["all_operations"][1] == {"id": 101, "name": R, "sequence": 20, REDACTED_MARKER_KEY: True}
        # Another route's ``all_operations`` gets no opinion (its ids may be another table's).
        assert redact_payload(payload, index, route_path="/routing/{routing_id}").payload == payload

    def test_a_timeline_item_is_classified_by_the_operation_it_links(self):
        index = _index(controlled={**_index().controlled, "operation": {101: UNLINKABLE}})
        payload = {
            "items": [
                {
                    "id": "operation-start:101",
                    "detail": "20 · Laser ITAR widget",
                    "source_url": "/work-orders/2#operation-101",
                },
                {"id": "operation-start:100", "detail": "10 · Form", "source_url": "/work-orders/2#operation-100"},
                {"id": "job-created:2", "detail": "WO-2", "source_url": "/work-orders/2"},
            ],
            "next_cursor": None,
        }
        out = redact_payload(payload, index, route_path="/work-orders/{work_order_id}/timeline").payload
        assert out["items"][0]["detail"] == R and out["items"][0][REDACTED_MARKER_KEY] is True
        assert out["items"][1] == payload["items"][1] and out["items"][2] == payload["items"][2]

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

    def test_short_tokens_are_not_matched_inside_longer_strings(self):
        assert MIN_SENSITIVE_TOKEN_LENGTH == 4
        assert compile_sensitive_pattern(["ABC", "  ", ""]) is None
        pattern = compile_sensitive_pattern(["ABCD", "ABCDEF"])
        assert pattern.sub("#", "xABCDEFx ABCD") == "x#x #", "longest token first"

    def test_a_short_token_is_still_withheld_as_a_whole_value(self):
        """Code review P0, finding 2: a controlled customer coded "BAE" went out as "BAE"."""
        index = _index(sensitive_tokens=("BAE", "PN-SECRET-10"), customer_names={"bae": (7, ITAR)})
        payload = {
            "customers": ["BAE", " bae ", "Open Commerce Co"],
            "items": [{"id": 2, "customer_po": "Bae", "notes": "via BAE dock"}],
            "group_totals": {"BAE": 3, "Open Commerce Co": 1},
        }
        out = redact_payload(payload, index, route_path="/work-orders/browse").payload
        assert out["customers"] == [R, R, "Open Commerce Co"]
        assert out["items"][0]["customer_po"] == R
        assert out["items"][0]["notes"] == "via BAE dock", "inside a longer string: the 4-character floor"
        assert out["group_totals"] == {R: 3, "Open Commerce Co": 1}
        assert scrub_text({"detail": "bae"}, index) == {"detail": R}

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


class TestErrorBodiesAndMaskedLookups:
    """Code review P0, finding 1: an error that echoes the request is an oracle once scrubbed."""

    def test_a_route_error_becomes_one_fixed_text(self):
        echo = {"detail": "Part PN-SECRET-10 not found"}
        assert ai_external_error_body(404, echo) == {"detail": ai_external_error_detail(404)}
        assert ai_external_error_body(404, {"detail": "anything else"}) == ai_external_error_body(404, echo)
        assert "404 Not Found" in ai_external_error_detail(404)
        assert ai_external_error_detail(799).startswith("Request failed (799 Error)")

    def test_a_422_keeps_location_and_type_never_the_input(self):
        payload = {
            "detail": [
                {
                    "type": "int_parsing",
                    "loc": ["path", "part_id"],
                    "msg": "Input should be ...",
                    "input": "PN-SECRET-10",
                },
                {
                    "type": "value_error",
                    "loc": ["body", "PN-SECRET-10", "x"],
                    "msg": "Value error, PN-SECRET-10",
                    "ctx": {},
                },
                "not a dict",
            ]
        }
        body = ai_external_error_body(422, payload)
        assert body["detail"] == ai_external_error_detail(422)
        assert body["errors"] == [
            {"type": "int_parsing", "loc": ["path", "part_id"], "msg": VALIDATION_ERROR_MESSAGE},
            {"type": "value_error", "loc": ["body"], "msg": VALIDATION_ERROR_MESSAGE},
        ]
        assert "PN-SECRET-10" not in str(body), "a body key may be the caller's: never echoed"
        assert "input" not in str(body) and "Value error" not in str(body)

    def test_only_a_withheld_record_on_a_masked_route_is_masked(self):
        withheld = redact_payload(
            {"id": 10, "part_number": "PN-SECRET-10"}, _index(), route_path="/parts/by-number/{part_number}"
        )
        cleared = redact_payload(
            {"id": 11, "part_number": "OPEN-1"}, _index(), route_path="/parts/by-number/{part_number}"
        )
        assert is_masked_lookup("/parts/by-number/{part_number}", withheld) is True
        assert is_masked_lookup("/parts/by-number/{part_number}", cleared) is False
        # A work-order number is a KEPT identifier: that lookup is never masked.
        assert is_masked_lookup("/work-orders/by-number/{wo_number}", withheld) is False
