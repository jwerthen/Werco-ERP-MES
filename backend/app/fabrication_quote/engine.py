"""Deterministic assembly costing, independent of ORM, HTTP and machine services.

Internal arithmetic is Decimal at 100-digit precision. Display monetary totals
are rounded once to six decimal places (ROUND_HALF_UP), not at every operation.
Hardware procurement cash is a separate measure and is never added a second
time to manufacturing cost. Freight is allocated to consumed purchased units;
excess purchased inventory retains the remaining acquisition value.
"""

import hashlib
import json
from collections import deque
from datetime import date
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, localcontext
from math import lcm

from .recipes import evaluate_recipe
from .schemas import Evidence, QuotePlan

ENGINE_VERSION = "fabrication-1.0.0"
ZERO = Decimal("0")
MAX_DEMAND = Decimal("1000000000000")
COST_KEYS = (
    "material_cost",
    "purchased_parts_cost",
    "labor_cost",
    "machine_cost",
    "consumables_cost",
    "outside_cost",
    "hardware_consumed_cost",
)


def _text(value: Decimal | None, *, money=False):
    if value is None:
        return None
    if money:
        value = value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    return format(value, "f")


def _canonical(value):
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    return value


def evaluate_plan(plan: QuotePlan | dict, as_of: date) -> dict:
    """Calculate a complete or explicitly blocked draft with JSON-safe values.

    `input_hash` includes the entire normalized plan, engine version, and as_of
    date. Offer validity is evaluated on that date, never the machine's clock.
    """
    # Revalidate model instances too: model_copy(update=...) and mutations to
    # contained lists can otherwise bypass Pydantic's assignment validation.
    plan = QuotePlan.model_validate(plan.model_dump() if isinstance(plan, QuotePlan) else plan)
    if type(as_of) is not date:
        raise TypeError("as_of must be a date, not a timestamp or string")
    with localcontext() as ctx:
        ctx.prec = 100
        return _evaluate(plan, as_of)


def _evaluate(plan, as_of):
    issues = []

    def issue(code, path, message, severity="blocking"):
        issues.append({"severity": severity, "code": code, "message": message, "path": path})

    def review(evidence: Evidence, path):
        if not evidence.reviewed:
            issue("review_required", path, "Estimator review is required.")
        if not evidence.source or not evidence.source.strip():
            issue(
                "source_required",
                path + ".source",
                "Record a source or the basis for an explicit estimator assumption.",
            )

    def missing(value, path):
        if value is None:
            issue(
                "unpriced_input",
                path,
                "Required quantity, time, or cost is unknown; enter a supported value or explicit zero.",
            )
        return value

    structural = False
    for field in ("parts", "bom", "materials", "operations", "hardware", "assumptions"):
        seen = set()
        for i, row in enumerate(getattr(plan, field)):
            if row.id in seen:
                issue("duplicate_id", f"{field}.{i}.id", f"Duplicate {field} ID {row.id}.")
                structural = True
            seen.add(row.id)
    review_ids = set()
    for i, row in enumerate(plan.source_reviews):
        if row.file_id in review_ids:
            issue(
                "duplicate_source_review",
                f"source_reviews.{i}.file_id",
                "A source file may have only one disposition.",
            )
        review_ids.add(row.file_id)
        if not row.note.strip():
            issue(
                "source_note_required",
                f"source_reviews.{i}.note",
                "Source disposition requires a meaningful note.",
            )
    for i, assumption in enumerate(plan.assumptions):
        if not assumption.reviewed or not assumption.source or not assumption.source.strip():
            issue(
                "assumption_review_required",
                f"assumptions.{i}",
                "Every assumption requires review and a recorded basis.",
            )

    parts = {part.id: part for part in plan.parts}
    children = {part_id: [] for part_id in parts}
    indegree = {part_id: 0 for part_id in parts}
    for i, edge in enumerate(plan.bom):
        if edge.parent_id not in parts or edge.child_id not in parts:
            issue(
                "missing_part_reference",
                f"bom.{i}",
                "BOM parent and child must reference defined parts.",
            )
            structural = True
            continue
        children[edge.parent_id].append(edge)
        indegree[edge.child_id] += 1
    for group in ("materials", "operations", "hardware", "roots"):
        for i, row in enumerate(getattr(plan, group)):
            if row.part_id not in parts:
                issue(
                    "missing_part_reference",
                    f"{group}.{i}.part_id",
                    f"Unknown part {row.part_id}.",
                )
                structural = True
    queue = deque(p for p in parts if indegree[p] == 0)
    order = []
    while queue:
        part_id = queue.popleft()
        order.append(part_id)
        for edge in children[part_id]:
            indegree[edge.child_id] -= 1
            if indegree[edge.child_id] == 0:
                queue.append(edge.child_id)
    if len(order) != len(parts):
        issue(
            "bom_cycle",
            "bom",
            "BOM contains a cycle; recursive assembly demand cannot be calculated.",
        )
        structural = True
    if not plan.roots:
        issue(
            "missing_root_demand",
            "roots",
            "At least one root assembly or part quantity is required.",
        )
        structural = True
    demand = {part_id: ZERO for part_id in parts}
    if not structural:
        for root in plan.roots:
            demand[root.part_id] += root.quantity
        for part_id in order:
            if demand[part_id] > MAX_DEMAND:
                issue(
                    "demand_limit",
                    "bom",
                    "Rolled-up demand exceeds the supported limit of 1,000,000,000,000 units.",
                )
                structural = True
                break
            if parts[part_id].make_or_buy == "buy":
                if children[part_id] and demand[part_id]:
                    issue(
                        "buy_boundary",
                        "bom",
                        f"Children of purchased part {part_id} are excluded from its cost and demand.",
                        "warning",
                    )
                continue
            for edge in children[part_id]:
                demand[edge.child_id] += demand[part_id] * edge.quantity

    totals = {key: ZERO for key in COST_KEYS}
    unknown = set()
    procurement_cash = ZERO
    procurement_unknown = False
    material_lines, operation_lines, hardware_lines, purchased_part_lines = (
        [],
        [],
        [],
        [],
    )

    def add(key, value):
        if value is None:
            unknown.add(key)
        else:
            totals[key] += value

    def active(part_id):
        return not structural and demand.get(part_id, ZERO) > ZERO and parts[part_id].make_or_buy == "make"

    def multiplier(quantity, basis, size):
        return quantity if basis == "per_unit" else (quantity / size).to_integral_value(rounding=ROUND_CEILING)

    if not structural:
        for i, part in enumerate(plan.parts):
            quantity = demand[part.id]
            if not quantity:
                continue
            review(part.evidence, f"parts.{i}.evidence")
            if not part.costing_complete:
                issue(
                    "incomplete_part_costing",
                    f"parts.{i}.costing_complete",
                    "Confirm that every required material, process and secondary operation is represented.",
                )
            if part.make_or_buy == "buy":
                value = missing(part.purchase_unit_cost, f"parts.{i}.purchase_unit_cost")
                cost = None if value is None else value * quantity
                add("purchased_parts_cost", cost)
                purchased_part_lines.append(
                    {
                        "part_id": part.id,
                        "quantity": _text(quantity),
                        "cost": _text(cost, money=True),
                    }
                )

    for i, material in enumerate(plan.materials):
        if not active(material.part_id):
            continue
        path = f"materials.{i}"
        review(material.evidence, path + ".evidence")
        mult = multiplier(demand[material.part_id], material.quantity_basis, material.batch_size)
        used = missing(material.consumed_quantity, path + ".consumed_quantity")
        consumed = None if used is None else used * mult
        rate = material.unit_cost
        if consumed != ZERO:
            missing(rate, path + ".unit_cost")
        cost = ZERO if consumed == ZERO else None if consumed is None or rate is None else consumed * rate
        add("material_cost", cost)
        material_lines.append(
            {
                "id": material.id,
                "part_id": material.part_id,
                "multiplier": _text(mult),
                "consumed_quantity": _text(consumed),
                "unit": material.unit,
                "cost": _text(cost, money=True),
            }
        )

    for i, op in enumerate(plan.operations):
        if not active(op.part_id):
            continue
        path = f"operations.{i}"
        review(op.evidence, path + ".evidence")
        batches = multiplier(demand[op.part_id], "per_batch", op.batch_size)
        setups = Decimal("1") if op.setup_basis == "per_quote" else batches
        runs = multiplier(demand[op.part_id], op.run_basis, op.batch_size)
        recipe = evaluate_recipe(op.recipe)
        for code, field, message in recipe.issues:
            issue(code, path + ".recipe." + field, message)
        setup_labor = missing(op.setup_labor_seconds, path + ".setup_labor_seconds")
        setup_machine = missing(op.setup_machine_seconds, path + ".setup_machine_seconds")
        labor_seconds = (
            None
            if setup_labor is None or recipe.labor_seconds is None
            else setup_labor * setups + recipe.labor_seconds * runs
        )
        machine_seconds = (
            None
            if setup_machine is None or recipe.machine_seconds is None
            else setup_machine * setups + recipe.machine_seconds * runs
        )

        def resource_cost(seconds, rate, field):
            if seconds == ZERO:
                return ZERO
            missing(rate, path + "." + field)
            return None if seconds is None or rate is None else seconds * rate / Decimal("3600")

        labor_cost = resource_cost(labor_seconds, op.labor_rate_per_hour, "labor_rate_per_hour")
        machine_cost = resource_cost(machine_seconds, op.machine_rate_per_hour, "machine_rate_per_hour")
        consumables = missing(op.consumables_cost_per_run, path + ".consumables_cost_per_run")
        outside = missing(op.outside_cost_per_run, path + ".outside_cost_per_run")
        consumables_cost = None if consumables is None else consumables * runs
        outside_cost = None if outside is None else outside * runs
        values = {
            "labor_cost": labor_cost,
            "machine_cost": machine_cost,
            "consumables_cost": consumables_cost,
            "outside_cost": outside_cost,
        }
        for key, value in values.items():
            add(key, value)
        operation_lines.append(
            {
                "id": op.id,
                "part_id": op.part_id,
                "setup_count": _text(setups),
                "run_multiplier": _text(runs),
                "labor_seconds": _text(labor_seconds),
                "machine_seconds": _text(machine_seconds),
                **{key: _text(value, money=True) for key, value in values.items()},
                "cost": _text(
                    (None if any(v is None for v in values.values()) else sum(values.values(), ZERO)),
                    money=True,
                ),
            }
        )

    # Pool exact-identity hardware demand before applying one stock balance,
    # supplier break, pack/MOQ rounding and freight charge per selected offer.
    hardware_groups = {}
    for i, hw in enumerate(plan.hardware):
        if not active(hw.part_id):
            continue
        review(hw.evidence, f"hardware.{i}.evidence")
        identity = (hw.manufacturer, hw.mpn)
        group = hardware_groups.setdefault(identity, [])
        group.append((i, hw, demand[hw.part_id] * hw.quantity_per_part))
    for identity, group in hardware_groups.items():
        index, first, _ = group[0]
        path = f"hardware.{index}"
        quantity = sum((x[2] for x in group), ZERO)

        # Stock and purchasing data must be shared values, not independent
        # inventories accidentally repeated on every assembly occurrence.
        def settings(hw):
            return (
                hw.stock_available,
                hw.stock_unit_value,
                hw.offer.model_dump() if hw.offer else None,
            )

        compatible = all(settings(hw) == settings(first) for _, hw, _ in group)
        if not compatible:
            issue(
                "conflicting_hardware_supply",
                path,
                "Repeated exact hardware identities must use the same stock balance, inventory value and selected supplier offer.",
            )
        if quantity != quantity.to_integral_value():
            issue(
                "fractional_hardware_demand",
                path,
                "Hardware each demand must be an integer; review BOM quantities.",
            )
            compatible = False
        stock_used = min(quantity, Decimal(first.stock_available))
        purchased_consumed = quantity - stock_used
        stock_value = first.stock_unit_value
        if stock_used > ZERO:
            missing(stock_value, path + ".stock_unit_value")
        stock_cost = ZERO if stock_used == ZERO else None if stock_value is None else stock_used * stock_value
        purchase_qty = ZERO
        unit_price = None
        freight = ZERO
        purchase_cost = ZERO
        cash = ZERO
        purchase_valid = compatible
        offer = first.offer
        if purchased_consumed > ZERO:
            if offer is None:
                issue(
                    "missing_hardware_offer",
                    path + ".offer",
                    "An exact applicable offer is required for hardware demand beyond valued inventory.",
                )
                purchase_valid = False
            else:
                review(offer.evidence, path + ".offer.evidence")
                if (offer.manufacturer, offer.mpn) != identity:
                    issue(
                        "hardware_identity_mismatch",
                        path + ".offer",
                        "Offer manufacturer and MPN must exactly match the BOM requirement; substitution is not automatic.",
                    )
                    purchase_valid = False
                if offer.currency != plan.currency:
                    issue(
                        "currency_mismatch",
                        path + ".offer.currency",
                        "Offer currency differs from the quote currency; FX conversion is not supported.",
                    )
                    purchase_valid = False
                if not offer.applicable:
                    issue(
                        "inapplicable_offer",
                        path + ".offer.applicable",
                        "Confirm this offer applies to the customer, job, item revision and purchase terms.",
                    )
                    purchase_valid = False
                if (
                    offer.quoted_on is None
                    or offer.quoted_on > as_of
                    or (as_of - offer.quoted_on).days > offer.max_age_days
                    or (offer.valid_until is not None and offer.valid_until < as_of)
                ):
                    issue(
                        "stale_or_undated_offer",
                        path + ".offer",
                        "Offer must be dated, current and within its stated freshness and validity limits.",
                    )
                    purchase_valid = False
                if offer.quoted_on and offer.valid_until and offer.valid_until < offer.quoted_on:
                    issue(
                        "invalid_offer_dates",
                        path + ".offer.valid_until",
                        "Offer expiry precedes its quotation date.",
                    )
                    purchase_valid = False
                multiple = Decimal(lcm(offer.pack_quantity, offer.order_multiple))
                purchase_qty = (
                    max(purchased_consumed, Decimal(offer.minimum_order_quantity)) / multiple
                ).to_integral_value(rounding=ROUND_CEILING) * multiple
                if purchase_qty > MAX_DEMAND:
                    issue(
                        "procurement_limit",
                        path + ".offer",
                        "Rounded hardware purchase quantity exceeds the supported limit.",
                    )
                    purchase_valid = False
                thresholds = [br.minimum_quantity for br in offer.price_breaks]
                if len(set(thresholds)) != len(thresholds):
                    issue(
                        "duplicate_price_break",
                        path + ".offer.price_breaks",
                        "Each price-break threshold must be unique.",
                    )
                    purchase_valid = False
                eligible = [br for br in offer.price_breaks if br.minimum_quantity <= purchase_qty]
                if not eligible:
                    issue(
                        "missing_price_break",
                        path + ".offer.price_breaks",
                        "No supplier price break applies to the rounded order quantity.",
                    )
                    purchase_valid = False
                else:
                    selected = max(eligible, key=lambda br: br.minimum_quantity)
                    unit_price = selected.price / offer.price_unit_quantity
                freight = missing(offer.freight, path + ".offer.freight")
                if freight is None:
                    purchase_valid = False
                if purchase_valid:
                    cash = purchase_qty * unit_price + freight
                    purchase_cost = purchased_consumed * (unit_price + freight / purchase_qty)
            if not purchase_valid:
                cash = purchase_cost = None
        elif not compatible:
            purchase_cost = cash = None
        consumed_cost = None if stock_cost is None or purchase_cost is None else stock_cost + purchase_cost
        add("hardware_consumed_cost", consumed_cost)
        if cash is None:
            procurement_unknown = True
        else:
            procurement_cash += cash
        excess_quantity = purchase_qty - purchased_consumed if purchase_qty >= purchased_consumed else None
        excess_value = None if cash is None or purchase_cost is None else cash - purchase_cost
        hardware_lines.append(
            {
                "id": first.id,
                "line_ids": [hw.id for _, hw, _ in group],
                "manufacturer": identity[0],
                "mpn": identity[1],
                "required_quantity": _text(quantity),
                "stock_issued": _text(stock_used),
                "purchased_consumed_quantity": _text(purchased_consumed),
                "purchase_quantity": _text(purchase_qty),
                "unit_price": _text(unit_price),
                "stock_issued_cost": _text(stock_cost, money=True),
                "consumed_cost": _text(consumed_cost, money=True),
                "procurement_cash": _text(cash, money=True),
                "excess_inventory_quantity": _text(excess_quantity),
                "excess_inventory_value": _text(excess_value, money=True),
            }
        )

    if structural:
        unknown.update(COST_KEYS)
    known_cost = sum(totals.values(), ZERO)
    total_cost = None if unknown else known_cost
    if plan.target_margin is None:
        issue(
            "missing_margin",
            "target_margin",
            "Enter the reviewed target gross margin as a fraction, such as 0.25 for 25%.",
        )
    selling_price = (
        None if total_cost is None or plan.target_margin is None else total_cost / (Decimal("1") - plan.target_margin)
    )
    gross_profit = None if selling_price is None else selling_price - total_cost
    result_totals = {key: _text(None if key in unknown else value, money=True) for key, value in totals.items()}
    result_totals.update(
        {
            "hardware_procurement_cash": _text(
                None if procurement_unknown or structural else procurement_cash,
                money=True,
            ),
            "known_cost": _text(known_cost, money=True),
            "total_cost": _text(total_cost, money=True),
            "selling_price": _text(selling_price, money=True),
            "gross_profit": _text(gross_profit, money=True),
            "target_margin": _text(plan.target_margin),
        }
    )
    payload = {
        "engine_version": ENGINE_VERSION,
        "as_of": as_of.isoformat(),
        "plan": _canonical(plan.model_dump()),
    }
    input_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    return {
        "engine_version": ENGINE_VERSION,
        "input_hash": input_hash,
        "as_of": as_of.isoformat(),
        "currency": plan.currency,
        "can_approve": not any(x["severity"] == "blocking" for x in issues),
        "issues": issues,
        "demand": (
            [
                {
                    "part_id": p,
                    "quantity": _text(demand[p]),
                    "make_or_buy": parts[p].make_or_buy,
                }
                for p in sorted(parts)
                if demand[p] > ZERO
            ]
            if not structural
            else []
        ),
        "material_lines": material_lines,
        "operation_lines": operation_lines,
        "hardware_lines": hardware_lines,
        "purchased_part_lines": purchased_part_lines,
        "totals": result_totals,
    }
