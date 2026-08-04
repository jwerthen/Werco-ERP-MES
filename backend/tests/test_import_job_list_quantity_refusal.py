"""``scripts/import_job_list.py`` must never hand the DB a non-positive quantity.

This is the go-live open-work-order loader (docs/EXCEL_MIGRATION_RUNBOOK.md): it reads
the legacy job-list .xlsx and creates Customer / Part / WorkOrder rows in ONE
transaction, with a dry-run-then-commit discipline.

Before migration 080 it degraded a blank or non-numeric QTY cell to ``0.0`` and loaded
the work order anyway, on the theory that a human would fix it later. 080 restored
``chk_work_orders_quantity_ordered_positive`` (``work_orders.quantity_ordered > 0``),
which makes that state unrepresentable -- and because the loader commits as a single
transaction, one degraded row would abort the ENTIRE import with an IntegrityError at
flush, after the operator had already reviewed a dry-run plan that looked fine.

So the loader now refuses the individual row: it appends a warning naming the row and
the bad value, and drops it from ``wos_to_create``. Two properties are load-bearing and
asserted here:

* the bad row never reaches the DB (no non-positive ``quantity_ordered`` is ever built),
  and
* refusing it does NOT take the rest of the load down -- the good rows in the same
  spreadsheet still plan normally, which is the whole reason for per-row refusal over a
  hard abort.

The warning is surfaced during the dry run, i.e. BEFORE ``--commit``, so the operator
fixes the cell and re-runs rather than discovering it mid-transaction.
"""

import pytest

from app.models.company import Company
from app.models.work_order import WorkOrder
from scripts.import_job_list import build_plan


def _row(row_number: int, wo_number: str, qty, part_number: str = "PN-100"):
    """A minimal already-parsed job-list row, as ``read_rows`` would hand it over.

    ``final_wo_number`` is normally assigned by ``assign_final_wo_numbers``; the rows
    here have unique job numbers, so it is just the job number.
    """
    return {
        "_row": row_number,
        "customer": "ACME AEROSPACE",
        "qty": qty,
        "part_number": part_number,
        "rev": "A",
        "serial": None,
        "description": "BRACKET, LOWER",
        "wo_number": wo_number,
        "final_wo_number": wo_number,
        "po_number": "PO-9001",
        "po_line_item": "1",
        "po_date": None,
        "due_date": None,
    }


@pytest.fixture
def company(db_session) -> Company:
    company = Company(name="Werco Import Test", slug="werco-import-test")
    db_session.add(company)
    db_session.commit()
    db_session.refresh(company)
    return company


@pytest.mark.unit
class TestImportJobListQuantityRefusal:
    """QTY <= 0 / non-numeric is refused per row, never degraded to 0 and loaded."""

    @pytest.mark.parametrize(
        "bad_qty",
        [
            pytest.param(0, id="zero"),
            pytest.param(0.0, id="zero_float"),
            pytest.param(-5, id="negative"),
            pytest.param(None, id="blank_cell"),
            pytest.param("", id="empty_string"),
            pytest.param("N/A", id="non_numeric_text"),
        ],
    )
    def test_a_non_positive_or_unparseable_qty_row_is_refused(self, db_session, company, bad_qty):
        """The row is dropped and named; nothing with quantity_ordered <= 0 is planned.

        Fails against the pre-080 loader, which appended "using 0" and planned the WO
        with ``quantity_ordered = 0.0`` -- the row that would abort the whole commit.
        """
        rows = [_row(2, "W1000", bad_qty)]

        _customers, _parts, wos_to_create, warnings = build_plan(rows, db_session, company.id)

        assert wos_to_create == [], f"QTY {bad_qty!r} must not be planned as a work order"
        assert any(
            "Row 2" in warning and "SKIPPED" in warning for warning in warnings
        ), f"the refusal must name the row so the dry run surfaces it: {warnings}"
        # The old degrade path is gone: no warning may promise the row was loaded.
        assert not any("using 0" in warning for warning in warnings), warnings

    def test_refusing_one_row_does_not_drop_the_rest_of_the_load(self, db_session, company):
        """Per-row refusal, not a hard abort -- the good rows still plan.

        This is the property that makes refusal the right call over letting the
        IntegrityError fly: a 400-row go-live spreadsheet with one bad cell still loads
        399 work orders, and the operator sees exactly which cell to fix.
        """
        rows = [
            _row(2, "W1001", 10, part_number="PN-A"),
            _row(3, "W1002", 0, part_number="PN-B"),
            _row(4, "W1003", 2.5, part_number="PN-C"),
        ]

        _customers, parts_to_create, wos_to_create, warnings = build_plan(rows, db_session, company.id)

        planned = {wo["work_order_number"]: wo["quantity_ordered"] for wo in wos_to_create}
        assert planned == {"W1001": 10.0, "W1003": 2.5}
        assert any("Row 3" in warning for warning in warnings), warnings
        # The refusal is scoped to the WORK ORDER. The part was already registered above
        # the QTY check (same as the pre-existing duplicate-WO branch), and creating a
        # part is harmless and idempotent -- asserted so a future refactor that moves the
        # check has to make that call deliberately.
        assert ("PN-B", "A") in parts_to_create

    def test_every_planned_quantity_satisfies_the_restored_db_check(self, db_session, company):
        """The contract, stated as the CHECK states it: quantity_ordered > 0.

        Ties this test to ``chk_work_orders_quantity_ordered_positive`` rather than to
        the loader's current spelling of the guard.
        """
        rows = [
            _row(2, "W2001", 1),
            _row(3, "W2002", -0.001, part_number="PN-D"),
            _row(4, "W2003", 0.5, part_number="PN-E"),
            _row(5, "W2004", "  ", part_number="PN-F"),
        ]

        _customers, _parts, wos_to_create, _warnings = build_plan(rows, db_session, company.id)

        assert wos_to_create, "the valid rows must still plan"
        for wo in wos_to_create:
            assert wo["quantity_ordered"] > 0, f"{wo['work_order_number']} violates the restored CHECK: {wo}"

    def test_a_refused_row_is_refused_before_any_work_order_is_written(self, db_session, company):
        """``build_plan`` is pure planning -- it must not write, refusal or not.

        The dry run calls this same function, so a write here would mean ``--commit``
        was not the only thing that touches the DB.
        """
        before = db_session.query(WorkOrder).count()

        build_plan([_row(2, "W3001", 0)], db_session, company.id)

        assert db_session.query(WorkOrder).count() == before
