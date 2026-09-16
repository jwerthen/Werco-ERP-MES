"""Independent reference cases for the new fabrication cost domain.

Dollar figures here are synthetic arithmetic fixtures, never production rates.
The tests need no database, controller, network or production machine profile.
"""

import json
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.fabrication_quote.engine import evaluate_plan
from app.fabrication_quote.schemas import QuotePlan

DAY = date(2026, 9, 15)
REVIEW = {"reviewed": True, "source": "Synthetic independent reference case"}


def part(part_id, **extra):
    return {"id": part_id, "costing_complete": True, "evidence": REVIEW, **extra}


def plan(quantity="1"):
    return {
        "parts": [part("A")],
        "roots": [{"part_id": "A", "quantity": quantity}],
        "target_margin": "0.25",
    }


def operation(**extra):
    return {
        "id": "O1",
        "part_id": "A",
        "setup_labor_seconds": "600",
        "setup_machine_seconds": "300",
        "labor_rate_per_hour": "36",
        "machine_rate_per_hour": "72",
        "consumables_cost_per_run": "0",
        "outside_cost_per_run": "0",
        "recipe": {"kind": "manual", "labor_seconds": "60", "machine_seconds": "30"},
        "evidence": REVIEW,
        **extra,
    }


def offer(**extra):
    return {
        "id": "offer-1",
        "manufacturer": "ACME",
        "mpn": "BOLT-1",
        "supplier": "Supplier",
        "price_unit_quantity": "100",
        "price_breaks": [
            {"minimum_quantity": "0", "price": "200"},
            {"minimum_quantity": "100", "price": "150"},
        ],
        "pack_quantity": 25,
        "minimum_order_quantity": 60,
        "order_multiple": 10,
        "quoted_on": str(DAY),
        "valid_until": str(DAY + timedelta(days=10)),
        "applicable": True,
        "freight": "10",
        "evidence": REVIEW,
        **extra,
    }


def hardware(**extra):
    return {
        "id": "H1",
        "part_id": "A",
        "manufacturer": "ACME",
        "mpn": "BOLT-1",
        "quantity_per_part": "1",
        "stock_available": 10,
        "stock_unit_value": "1.20",
        "offer": offer(),
        "evidence": REVIEW,
        **extra,
    }


def codes(result):
    return {issue["code"] for issue in result["issues"] if issue["severity"] == "blocking"}


def test_reference_assembly_shared_subassemblies_and_buy_boundary():
    p = plan("2")
    p["parts"] = [
        part("A"),
        part("B"),
        part("C", make_or_buy="buy", purchase_unit_cost="50"),
        part("D"),
    ]
    p["bom"] = [
        {"id": "AB", "parent_id": "A", "child_id": "B", "quantity": "3"},
        {"id": "AC", "parent_id": "A", "child_id": "C", "quantity": "1"},
        {"id": "BD", "parent_id": "B", "child_id": "D", "quantity": "2"},
        {"id": "CD", "parent_id": "C", "child_id": "D", "quantity": "100"},
    ]
    p["materials"] = [
        {
            "id": "M",
            "part_id": "D",
            "consumed_quantity": "0.5",
            "unit_cost": "4",
            "evidence": REVIEW,
        }
    ]
    p["operations"] = [operation(part_id="B")]
    result = evaluate_plan(p, DAY)
    assert result["can_approve"]
    assert {row["part_id"]: row["quantity"] for row in result["demand"]} == {
        "A": "2",
        "B": "6",
        "C": "2",
        "D": "12",
    }
    # 12 D x 0.5 kg x $4/kg = $24; two purchased C = $100.
    # B setup + six runs: 960 labor seconds x $36/hr = $9.60;
    # 480 machine seconds x $72/hr = $9.60. Total $143.20.
    assert result["totals"]["total_cost"] == "143.200000"
    assert result["totals"]["selling_price"] == "190.933333"
    assert result["totals"]["purchased_parts_cost"] == "100.000000"


def test_shared_diamond_demand_uses_all_paths_without_repeating_setup():
    p = plan("2")
    p["parts"] += [part("B"), part("C"), part("D")]
    p["bom"] = [
        {"id": parent + child, "parent_id": parent, "child_id": child, "quantity": qty}
        for parent, child, qty in [
            ("A", "B", "2"),
            ("A", "C", "3"),
            ("B", "D", "4"),
            ("C", "D", "5"),
        ]
    ]
    p["operations"] = [operation(part_id="D")]
    result = evaluate_plan(p, DAY)
    assert next(row["quantity"] for row in result["demand"] if row["part_id"] == "D") == "46"
    assert result["operation_lines"][0]["setup_count"] == "1"
    assert result["totals"]["total_cost"] == "67.200000"


def test_batch_material_and_run_basis_charge_partial_batch_once():
    p = plan("11")
    p["materials"] = [
        {
            "id": "M",
            "part_id": "A",
            "quantity_basis": "per_batch",
            "batch_size": "5",
            "consumed_quantity": "2",
            "unit": "sheet",
            "unit_cost": "20",
            "evidence": REVIEW,
        }
    ]
    p["operations"] = [operation(batch_size="5", setup_basis="per_batch", run_basis="per_batch")]
    result = evaluate_plan(p, DAY)
    assert result["material_lines"][0]["consumed_quantity"] == "6"
    assert result["operation_lines"][0]["setup_count"] == "3"
    assert result["operation_lines"][0]["run_multiplier"] == "3"
    assert result["totals"]["total_cost"] == "159.600000"


def test_hardware_reference_pack_price_units_stock_and_cash_reconcile():
    p = plan("80")
    p["hardware"] = [hardware()]
    result = evaluate_plan(p, DAY)
    assert result["can_approve"]
    row = result["hardware_lines"][0]
    # Need 80, issue 10 @ $1.20; buy 70 rounded to LCM(25,10)=50 => 100.
    # 100-unit break is $150 per 100, plus $10 freight = $160 cash.
    # Consumed purchased units carry $1.60 each; leftover 30 carry $48.
    assert row["purchase_quantity"] == "100"
    assert row["stock_issued_cost"] == "12.000000"
    assert row["procurement_cash"] == "160.000000"
    assert row["consumed_cost"] == "124.000000"
    assert row["excess_inventory_quantity"] == "30"
    assert row["excess_inventory_value"] == "48.000000"
    assert result["totals"]["total_cost"] == "124.000000"
    assert result["totals"]["hardware_procurement_cash"] == "160.000000"


def test_hardware_shared_bom_rows_pool_breaks_stock_and_freight_once():
    p = plan("40")
    p["hardware"] = [hardware(), hardware(id="H2")]
    result = evaluate_plan(p, DAY)
    assert result["can_approve"]
    assert len(result["hardware_lines"]) == 1
    assert result["hardware_lines"][0]["required_quantity"] == "80"
    assert result["totals"]["hardware_consumed_cost"] == "124.000000"


def test_stock_has_consumed_cost_even_without_procurement_offer():
    p = plan("5")
    p["hardware"] = [hardware(offer=None)]
    result = evaluate_plan(p, DAY)
    assert result["can_approve"]
    assert result["totals"]["hardware_consumed_cost"] == "6.000000"
    assert result["totals"]["hardware_procurement_cash"] == "0.000000"


@pytest.mark.parametrize(
    "patch, expected",
    [
        ({"manufacturer": "OTHER"}, "hardware_identity_mismatch"),
        ({"currency": "EUR"}, "currency_mismatch"),
        ({"quoted_on": "2020-01-01"}, "stale_or_undated_offer"),
        ({"applicable": False}, "inapplicable_offer"),
        ({"freight": None}, "unpriced_input"),
        ({"price_breaks": []}, "missing_price_break"),
    ],
)
def test_unusable_hardware_offer_blocks_and_never_becomes_zero(patch, expected):
    p = plan("80")
    p["hardware"] = [hardware(offer=offer(**patch))]
    result = evaluate_plan(p, DAY)
    assert expected in codes(result)
    assert result["totals"]["hardware_consumed_cost"] is None
    assert result["totals"]["total_cost"] is None
    assert result["totals"]["selling_price"] is None


def test_cycle_and_missing_references_block_without_recursive_failure():
    p = plan()
    p["parts"].append(part("B"))
    p["bom"] = [
        {"id": "AB", "parent_id": "A", "child_id": "B", "quantity": "1"},
        {"id": "BA", "parent_id": "B", "child_id": "A", "quantity": "1"},
    ]
    result = evaluate_plan(p, DAY)
    assert "bom_cycle" in codes(result)
    assert result["demand"] == []
    assert result["totals"]["total_cost"] is None
    p["bom"][1]["child_id"] = "missing"
    assert "missing_part_reference" in codes(evaluate_plan(p, DAY))


def test_deep_bom_is_iterative_and_bounded():
    p = plan()
    p["parts"] = [part(str(i)) for i in range(1100)]
    p["roots"] = [{"part_id": "0", "quantity": "1"}]
    p["bom"] = [{"id": str(i), "parent_id": str(i), "child_id": str(i + 1), "quantity": "1"} for i in range(1099)]
    assert evaluate_plan(p, DAY)["can_approve"]
    p["bom"][0]["quantity"] = "1000000000000"
    p["bom"][1]["quantity"] = "2"
    assert "demand_limit" in codes(evaluate_plan(p, DAY))


def test_unknown_manual_time_and_rate_preserve_unknown_total():
    p = plan()
    p["operations"] = [
        operation(
            recipe={"kind": "manual", "labor_seconds": None, "machine_seconds": "0"},
            labor_rate_per_hour=None,
        )
    ]
    result = evaluate_plan(p, DAY)
    assert not result["can_approve"]
    assert result["totals"]["labor_cost"] is None
    assert result["totals"]["total_cost"] is None
    # The known machine setup cost remains inspectable, never an implied total.
    assert result["totals"]["known_cost"] == "6.000000"


def test_laser_recipe_independent_time_reference_and_dynamics_guard():
    p = plan("2")
    p["operations"] = [
        operation(
            setup_labor_seconds="0",
            setup_machine_seconds="0",
            recipe={
                "kind": "laser",
                "cuts": [
                    {
                        "cut_length_mm": "1000",
                        "speed_mm_per_second": "20",
                        "pierces": 10,
                        "pierce_seconds": "0.5",
                    }
                ],
                "noncut_machine_seconds": "10",
                "labor_seconds": "15",
                "speed_includes_dynamics": False,
                "dynamics_allowance_seconds": "5",
            },
        )
    ]
    result = evaluate_plan(p, DAY)
    # 50 cutting + 5 piercing + 10 noncut + 5 dynamics = 70 s/part.
    assert result["operation_lines"][0]["machine_seconds"] == "140.0"
    assert result["totals"]["total_cost"] == "3.100000"
    p["operations"][0]["recipe"]["speed_includes_dynamics"] = True
    assert "double_count_dynamics" in codes(evaluate_plan(p, DAY))


def test_brake_crew_and_machine_occupancy_are_separate():
    p = plan("2")
    p["operations"] = [
        operation(
            setup_labor_seconds="0",
            setup_machine_seconds="0",
            recipe={
                "kind": "brake",
                "hits": 3,
                "seconds_per_hit": "10",
                "handling_seconds": "20",
                "inspection_seconds": "10",
                "crew_size": 2,
                "machine_seconds": "45",
                "feasibility_reviewed": True,
            },
        )
    ]
    result = evaluate_plan(p, DAY)
    assert result["operation_lines"][0]["labor_seconds"] == "240"
    assert result["operation_lines"][0]["machine_seconds"] == "90"
    assert result["totals"]["total_cost"] == "4.200000"


@pytest.mark.parametrize("process", ["MIG", "TIG", "fiber_laser"])
def test_weld_routes_require_explicit_procedure_specific_speed_and_size(process):
    p = plan()
    p["operations"] = [
        operation(
            setup_labor_seconds="0",
            setup_machine_seconds="0",
            recipe={
                "kind": "weld",
                "process": process,
                "weld_length_mm": "600",
                "weld_size_mm": "3",
                "travel_speed_mm_per_second": "5",
                "nonweld_labor_seconds": "480",
                "nonweld_machine_seconds": "60",
            },
        )
    ]
    result = evaluate_plan(p, DAY)
    assert result["operation_lines"][0]["labor_seconds"] == "600"
    assert result["operation_lines"][0]["machine_seconds"] == "180"
    assert result["totals"]["total_cost"] == "9.600000"
    p["operations"][0]["recipe"].pop("weld_size_mm")
    assert "missing_recipe_input" in codes(evaluate_plan(p, DAY))


def test_decimal_hash_is_stable_normalized_and_sensitive_to_date_and_inputs():
    p = plan("2.00")
    p["operations"] = [operation()]
    first = evaluate_plan(p, DAY)
    equivalent = deepcopy(p)
    equivalent["roots"][0]["quantity"] = "2"
    equivalent["operations"][0]["labor_rate_per_hour"] = Decimal("36.000")
    assert evaluate_plan(equivalent, DAY)["input_hash"] == first["input_hash"]
    assert evaluate_plan(p, DAY + timedelta(days=1))["input_hash"] != first["input_hash"]
    equivalent["operations"][0]["labor_rate_per_hour"] = "37"
    assert evaluate_plan(equivalent, DAY)["input_hash"] != first["input_hash"]
    json.dumps(first, allow_nan=False)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "1000000000001", "0.0000000001", True])
def test_invalid_decimal_inputs_rejected(value):
    p = plan()
    p["parts"][0]["purchase_unit_cost"] = value
    with pytest.raises(ValidationError):
        QuotePlan.model_validate(p)


def test_required_estimator_review_does_not_hide_computable_costs():
    p = plan()
    p["operations"] = [operation(evidence={"reviewed": False, "source": "Pending shop review"})]
    result = evaluate_plan(p, DAY)
    assert "review_required" in codes(result)
    assert result["totals"]["total_cost"] == "13.200000"
    assert result["can_approve"] is False


def test_conflicting_shared_stock_values_block_consumed_total():
    p = plan("5")
    p["hardware"] = [hardware(), hardware(id="H2", stock_available=20)]
    result = evaluate_plan(p, DAY)
    assert "conflicting_hardware_supply" in codes(result)
    assert result["totals"]["hardware_consumed_cost"] is None


def test_margin_is_margin_not_markup():
    p = plan()
    p["parts"][0].update(make_or_buy="buy", purchase_unit_cost="75")
    result = evaluate_plan(p, DAY)
    assert result["totals"]["selling_price"] == "100.000000"
    assert result["totals"]["gross_profit"] == "25.000000"


def test_empty_plan_is_blocked_not_a_zero_dollar_quote():
    result = evaluate_plan({}, DAY)
    assert {"missing_root_demand", "missing_margin"} <= codes(result)
    assert result["totals"]["total_cost"] is None


def test_purchased_part_missing_price_and_incomplete_route_block():
    p = plan()
    p["parts"][0].update(make_or_buy="buy", purchase_unit_cost=None, costing_complete=False)
    result = evaluate_plan(p, DAY)
    assert {"unpriced_input", "incomplete_part_costing"} <= codes(result)
    assert result["totals"]["purchased_parts_cost"] is None


def test_explicit_zero_work_does_not_require_a_fictitious_rate():
    p = plan()
    p["operations"] = [
        operation(
            setup_labor_seconds="0",
            setup_machine_seconds="0",
            labor_rate_per_hour=None,
            machine_rate_per_hour=None,
            recipe={"kind": "manual", "labor_seconds": "0", "machine_seconds": "0"},
        )
    ]
    result = evaluate_plan(p, DAY)
    assert result["can_approve"]
    assert result["totals"]["total_cost"] == "0.000000"


def test_unreviewed_and_duplicate_source_dispositions_block():
    p = plan()
    p["parts"][0]["evidence"] = {"reviewed": True, "source": "  "}
    p["assumptions"] = [{"id": "ASM", "description": "Fixture time pending", "reviewed": False}]
    p["source_reviews"] = [
        {
            "file_id": 1,
            "sha256": "a" * 64,
            "disposition": "reviewed",
            "note": "Reviewed drawing",
        },
        {"file_id": 1, "sha256": "a" * 64, "disposition": "excluded", "note": " "},
    ]
    assert {
        "source_required",
        "assumption_review_required",
        "duplicate_source_review",
        "source_note_required",
    } <= codes(evaluate_plan(p, DAY))


def test_duplicate_definition_never_silently_overwrites_cost():
    p = plan()
    p["parts"].append(part("A", make_or_buy="buy", purchase_unit_cost="100"))
    result = evaluate_plan(p, DAY)
    assert "duplicate_id" in codes(result)
    assert result["totals"]["total_cost"] is None


def test_fractional_each_hardware_is_not_silently_rounded_into_valid_quote():
    p = plan("1.5")
    p["hardware"] = [hardware()]
    result = evaluate_plan(p, DAY)
    assert "fractional_hardware_demand" in codes(result)
    assert result["totals"]["hardware_consumed_cost"] is None


def test_missing_offer_and_unvalued_stock_both_remain_unknown():
    p = plan("80")
    p["hardware"] = [hardware(offer=None, stock_unit_value=None)]
    result = evaluate_plan(p, DAY)
    assert {"missing_hardware_offer", "unpriced_input"} <= codes(result)
    assert result["totals"]["hardware_procurement_cash"] is None


def test_future_offer_and_duplicate_quantity_breaks_are_blocked():
    p = plan("80")
    p["hardware"] = [
        hardware(
            offer=offer(
                quoted_on=str(DAY + timedelta(days=2)),
                price_breaks=[
                    {"minimum_quantity": "0", "price": "1"},
                    {"minimum_quantity": "0", "price": "2"},
                ],
            )
        )
    ]
    assert {"stale_or_undated_offer", "duplicate_price_break"} <= codes(evaluate_plan(p, DAY))


def test_bounded_extreme_recipe_returns_finite_json_instead_of_decimal_crash():
    p = plan("1000000000000")
    p["operations"] = [
        operation(
            run_basis="per_batch",
            batch_size="0.000000001",
            labor_rate_per_hour="1000000000000",
            machine_rate_per_hour="1000000000000",
            recipe={
                "kind": "laser",
                "cuts": [
                    {
                        "cut_length_mm": "1000000000000",
                        "speed_mm_per_second": "0.000000001",
                        "pierces": 0,
                    }
                ],
                "noncut_machine_seconds": "1000000000000",
                "labor_seconds": "1000000000000",
                "speed_includes_dynamics": True,
            },
        )
    ]
    result = evaluate_plan(p, DAY)
    assert result["can_approve"]
    assert Decimal(result["totals"]["total_cost"]).is_finite()
    json.dumps(result, allow_nan=False)


def test_model_copy_cannot_bypass_numeric_validation():
    malformed = QuotePlan.model_validate(plan()).model_copy(update={"target_margin": Decimal("1")})
    with pytest.raises(ValidationError):
        evaluate_plan(malformed, DAY)


def test_calculation_requires_explicit_date_not_timestamp():
    from datetime import datetime

    with pytest.raises(TypeError):
        evaluate_plan(plan(), datetime(2026, 9, 15))
