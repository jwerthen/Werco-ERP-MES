"""Sheet-stock suggestions on the laser-nest PREVIEW response.

The review grid used to open with 42 empty sheet pickers. It now opens with the
matcher's answer already in each row, under ``nests[].sheet_suggestion``, plus
three header roll-ups on the response (``suggested_row_count`` /
``shortlist_row_count`` / ``short_stock_row_count``).

What is pinned here -- all of it boundary behavior, not matcher scoring (that
lives with ``sheet_stock_matcher``):

1. **Both preview routes carry it.** The ``{work_order_id}`` preview and the
   standalone preview run the same suggestion pass; a planner must not get a
   different answer depending on which wizard entry point they came in through.

2. **The IMPORT response does NOT.** Its echoed package is built from
   ``ParsedLaserNest.as_dict()`` with no ``sheet_matches``, so every row's
   ``sheet_suggestion`` is null and the three roll-ups are 0. The import path is
   where a tie is actually written; it must not also be a place suggestions
   appear, or "the server suggested it" starts reading like "the server did it".

3. **A suggestion may never fail a preview.** With ``match_sheet_parts`` raising,
   the preview returns exactly the pre-feature payload at HTTP 200. A planner who
   uploaded a 42-nest package gets their rows even when the catalog read, the LLM
   or the resolver falls over -- degrading costs 42 manual picks, which IS
   today's behavior; raising costs the whole upload.

4. **Tenant isolation (invariant 1).** A perfectly-matching sheet part belonging
   to another company is never suggested, never shortlisted, and never named in a
   reason string. The standalone route grew a ``db`` session for this feature, so
   it gets its own scoping test rather than inheriting the parent-addressed one's.

5. **``alloy_score`` never reaches the wire.** It is the matcher's internal alloy
   agreement weight behind ``score``; it is dropped at serialization so nothing
   downstream can start depending on it.

6. **``sheet_match_provenance`` is audit-only.** It lands on the WO-level audit
   row's ``extra_data`` and nowhere else -- it creates no allocation, and a
   malformed value is discarded rather than 400'd (refusing an import over a
   telemetry field would punish the planner for a wizard bug).

Offline by contract: the per-PDF AI extractor is monkeypatched (as in
``test_laser_nest_pdf_import.py``), and the AI disambiguation resolver is
monkeypatched to a recording no-op so no Anthropic call is reachable from any
test in this module.
"""

import io
import json
import zipfile

import pytest
from fastapi import status
from sqlalchemy.orm import Session

import app.api.endpoints.work_orders as work_orders_endpoint
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.inventory import InventoryItem
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.models.work_order_material import WorkOrderMaterialAllocation

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"sheetsug-{n}@co{company_id}.test",
        employee_id=f"SHEETSUG-{n:05d}",
        first_name="Sheet",
        last_name=f"Co{company_id}",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_laser_work_center(db: Session, *, company_id: int = COMPANY_A) -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        name=f"Laser {n}",
        code=f"LASER-SUG-{n}",
        work_center_type="laser",
        description="laser fixture",
        hourly_rate=120,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_parent_work_order(db: Session, *, company_id: int = COMPANY_A) -> WorkOrder:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"SUG-ASM-{n:05d}",
        name="Sheet suggestion assembly",
        part_type="assembly",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    wo = WorkOrder(
        work_order_number=f"SUG-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=1,
        status=WorkOrderStatus.RELEASED,
        priority=3,
        company_id=company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def make_sheet_part(
    db: Session,
    *,
    part_number: str,
    company_id: int = COMPANY_A,
    name: str = "Sheet stock",
    uom: str = "sheets",
) -> Part:
    """A raw-material part numbered on the shop's ``thk-WxL-grade`` convention.

    ``0.250X48X96-A36`` is what ``derive_sheet_spec`` reads as thickness 0.250 /
    size 48x96 and ``canonical_alloy`` reads as A36 -- i.e. the anchored triple
    the matcher gates on. Numbering matters here; a name-only part is a different
    (deliberately weaker) path through the parser.
    """
    _ensure_company(db, company_id)
    part = Part(
        part_number=part_number,
        name=name,
        part_type="raw_material",
        unit_of_measure=uom,
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def stock(db: Session, part: Part, quantity: float) -> InventoryItem:
    item = InventoryItem(
        part_id=part.id,
        company_id=part.company_id,
        location="RACK-1",
        warehouse="MAIN",
        quantity_on_hand=quantity,
        quantity_allocated=0.0,
        quantity_available=quantity,
        is_active=True,
        status="available",
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _pdf_zip(*names: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, b"%PDF-1.4\n%stub nest report\n")
    return buf.getvalue()


# The one descriptor triple every test in this module previews with, unless it
# says otherwise: 1/4" A36 on a 48x96 sheet, three runs.
A36_QUARTER = {
    "cnc_number": "05749",
    "material": "A36",
    "thickness": "0.250",
    "sheet_size": "48x96",
    "planned_runs": 3,
    "extraction_confidence": "high",
    "source": "ai",
    "warning": None,
}


@pytest.fixture(autouse=True)
def upload_dir(tmp_path, monkeypatch):
    """Local storage + laser-package roots under a tmp dir (both read UPLOAD_DIR)."""
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def mock_pdf_extraction(monkeypatch):
    """Every previewed PDF extracts to the same A36 1/4" 48x96 triple."""

    def _fake_extract(pdf_path, file_name, company_id=None, **kwargs):
        return {**A36_QUARTER, "cnc_number": file_name.rsplit(".", 1)[0]}

    monkeypatch.setattr(work_orders_endpoint, "extract_nest_fields_from_pdf", _fake_extract)


@pytest.fixture(autouse=True)
def no_ai_resolver(monkeypatch):
    """Patch the AI disambiguation leg to a recording no-op.

    The real resolver never raises and degrades cleanly without an API key, but
    patching it keeps every test here hermetic AND gives the wiring test something
    to assert against. Returns the call log.
    """
    calls = []

    def _fake_resolve(suggestions, *, company_id):
        calls.append((dict(suggestions), company_id))

    monkeypatch.setattr(work_orders_endpoint, "resolve_ambiguous_sheet_matches", _fake_resolve)
    return calls


def _preview_wo(client, headers, wo_id, zip_bytes, *, name="nests.zip"):
    return client.post(
        f"/api/v1/work-orders/{wo_id}/laser-nest-packages/preview",
        headers=headers,
        files={"file": (name, io.BytesIO(zip_bytes), "application/zip")},
    )


def _preview_standalone(client, headers, zip_bytes, *, name="nests.zip"):
    return client.post(
        "/api/v1/work-orders/laser-nest-packages/standalone/preview",
        headers=headers,
        files={"file": (name, io.BytesIO(zip_bytes), "application/zip")},
    )


def _import_standalone(client, headers, zip_bytes, *, rows, work_center_id=None, provenance=None):
    data = {"rows": json.dumps(rows)}
    if work_center_id is not None:
        data["work_center_id"] = str(work_center_id)
    if provenance is not None:
        data["sheet_match_provenance"] = provenance
    return client.post(
        "/api/v1/work-orders/laser-nest-packages/standalone/import",
        headers=headers,
        data=data,
        files={"file": ("nests.zip", io.BytesIO(zip_bytes), "application/zip")},
    )


def _row(source_file="05749.pdf", **overrides):
    row = {
        "source_file": source_file,
        "cnc_number": source_file.rsplit(".", 1)[0],
        "nest_name": source_file.rsplit(".", 1)[0],
        "planned_runs": 3,
        "material": "A36",
        "thickness": "0.250",
        "sheet_size": "48x96",
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------- #
# The suggestion is present, and it is the deterministic gate's answer
# --------------------------------------------------------------------------- #
class TestPreviewCarriesSuggestion:
    def test_wo_addressed_preview_prefills_the_one_matching_sheet(self, client, db_session):
        admin = make_user(db_session)
        parent = make_parent_work_order(db_session)
        sheet = make_sheet_part(db_session, part_number="0.250X48X96-A36", name="A36 sheet 1/4")
        stock(db_session, sheet, 10)

        resp = _preview_wo(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        data = resp.json()
        assert data["nest_count"] == 1

        suggestion = data["nests"][0]["sheet_suggestion"]
        assert suggestion is not None
        assert suggestion["status"] == "matched"
        assert suggestion["auto_fill_part_id"] == sheet.id
        assert [c["part_id"] for c in suggestion["candidates"]] == [sheet.id]

        candidate = suggestion["candidates"][0]
        assert candidate["part_number"] == "0.250X48X96-A36"
        assert candidate["basis"] == "deterministic"
        assert candidate["score"] == 100.0
        assert candidate["is_sheet_like"] is True
        assert candidate["reason"]
        # 10 on hand, 3 runs -> covered, and the projection is what is left.
        assert candidate["on_hand_known"] is True
        assert candidate["on_hand"] == 10.0
        assert candidate["demand"] == 3.0
        assert candidate["projected_on_hand"] == 7.0
        assert candidate["stock_state"] == "covered"

        # Header roll-ups.
        assert data["suggested_row_count"] == 1
        assert data["shortlist_row_count"] == 0
        assert data["short_stock_row_count"] == 0

    def test_standalone_preview_returns_the_same_suggestion(self, client, db_session):
        """Same package, no work order in the path -- same answer.

        The wizard reaches the standalone route when the planner is creating a
        fresh part-less laser WO. A different (or absent) suggestion there would
        mean the pre-fill depends on which entry point was used.
        """
        admin = make_user(db_session)
        sheet = make_sheet_part(db_session, part_number="0.250X48X96-A36")
        stock(db_session, sheet, 10)

        resp = _preview_standalone(client, headers_for(admin), _pdf_zip("05749.pdf"))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        data = resp.json()
        suggestion = data["nests"][0]["sheet_suggestion"]
        assert suggestion["status"] == "matched"
        assert suggestion["auto_fill_part_id"] == sheet.id
        assert data["suggested_row_count"] == 1

    def test_ambiguous_rows_shortlist_and_prefill_nothing(self, client, db_session):
        """Two equally-good sheets: shortlist both, pre-fill neither.

        This is the case that actually produces wrong-lot depletion, so it is the
        one where the server must decline. ``auto_fill_part_id`` stays null and
        the row is counted as a shortlist, not a suggestion.
        """
        admin = make_user(db_session)
        parent = make_parent_work_order(db_session)
        first = make_sheet_part(db_session, part_number="0.250X48X96-A36")
        second = make_sheet_part(db_session, part_number="0.250-48X96-A36-ALT")
        stock(db_session, first, 10)
        stock(db_session, second, 10)

        resp = _preview_wo(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        data = resp.json()
        suggestion = data["nests"][0]["sheet_suggestion"]
        assert suggestion["status"] == "ambiguous"
        assert suggestion["auto_fill_part_id"] is None
        assert {c["part_id"] for c in suggestion["candidates"]} == {first.id, second.id}
        assert suggestion["diagnostic"]
        assert data["suggested_row_count"] == 0
        assert data["shortlist_row_count"] == 1

    def test_unmatched_row_carries_a_gate_diagnostic_and_no_candidates(self, client, db_session):
        """No sheet of that thickness in the rack -> ``unmatched``, never a guess."""
        admin = make_user(db_session)
        parent = make_parent_work_order(db_session)
        make_sheet_part(db_session, part_number="0.500X48X96-A36")  # wrong thickness

        resp = _preview_wo(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        data = resp.json()
        suggestion = data["nests"][0]["sheet_suggestion"]
        assert suggestion["status"] == "unmatched"
        assert suggestion["auto_fill_part_id"] is None
        assert suggestion["candidates"] == []
        assert data["suggested_row_count"] == 0
        assert data["shortlist_row_count"] == 0
        assert data["short_stock_row_count"] == 0

    def test_package_demand_accumulates_into_the_short_stock_count(self, client, db_session):
        """Two nests, three runs each, four sheets on the rack.

        The shortage is visible at REVIEW time rather than at completion when the
        lot goes negative -- and it is advisory: the row is still matched and
        still pre-filled, because refusing to tie a right-spec sheet ships the
        nest untied, which is the failure this feature exists to close.
        """
        admin = make_user(db_session)
        parent = make_parent_work_order(db_session)
        sheet = make_sheet_part(db_session, part_number="0.250X48X96-A36")
        stock(db_session, sheet, 4)

        resp = _preview_wo(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf", "05750.pdf"))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        data = resp.json()
        assert data["suggested_row_count"] == 2
        # Row 1 draws 3 of 4 (covered); row 2 draws the package past the rack.
        assert data["short_stock_row_count"] == 1
        states = [row["sheet_suggestion"]["candidates"][0]["stock_state"] for row in data["nests"]]
        assert sorted(states) == ["covered", "short"]
        # Still pre-filled: short stock annotates, it never withholds the match.
        assert all(row["sheet_suggestion"]["auto_fill_part_id"] == sheet.id for row in data["nests"])

    def test_resolver_runs_over_the_matcher_output_for_the_active_company(self, client, db_session, no_ai_resolver):
        """The AI leg is invoked with the matcher's map and the TOKEN's company.

        ``company_id`` is mandatory on that call: it is what enforces the
        per-company ``allow_ai_egress`` CUI kill switch and scopes the usage row.
        """
        admin = make_user(db_session)
        parent = make_parent_work_order(db_session)
        make_sheet_part(db_session, part_number="0.250X48X96-A36")

        resp = _preview_wo(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert len(no_ai_resolver) == 1
        suggestions, company_id = no_ai_resolver[0]
        assert company_id == COMPANY_A
        assert set(suggestions) == {"05749.pdf"}


# --------------------------------------------------------------------------- #
# The import response is not a suggestion surface
# --------------------------------------------------------------------------- #
class TestImportResponseCarriesNoSuggestion:
    def test_import_echo_has_null_suggestions_and_zero_rollups(self, client, db_session):
        admin = make_user(db_session)
        make_laser_work_center(db_session)
        sheet = make_sheet_part(db_session, part_number="0.250X48X96-A36")
        stock(db_session, sheet, 10)

        resp = _import_standalone(client, headers_for(admin), _pdf_zip("05749.pdf"), rows=[_row()])

        assert resp.status_code == status.HTTP_200_OK, resp.text
        package = resp.json()["package"]
        assert package["nest_count"] == 1
        assert all(row["sheet_suggestion"] is None for row in package["nests"])
        assert package["suggested_row_count"] == 0
        assert package["shortlist_row_count"] == 0
        assert package["short_stock_row_count"] == 0

    def test_import_creates_no_tie_from_a_suggestion(self, client, db_session):
        """A perfectly-matching sheet exists; the row does not name it.

        Invariant 6(d): an untied import stays byte-identical to its pre-feature
        behavior. The suggestion is advisory -- only ``material_part_id`` on a
        planner-confirmed row creates a tie, and this row has none.
        """
        admin = make_user(db_session)
        make_laser_work_center(db_session)
        sheet = make_sheet_part(db_session, part_number="0.250X48X96-A36")
        stock(db_session, sheet, 10)

        resp = _import_standalone(client, headers_for(admin), _pdf_zip("05749.pdf"), rows=[_row()])

        assert resp.status_code == status.HTTP_200_OK, resp.text
        wo_id = resp.json()["child_work_order"]["id"]
        ties = (
            db_session.query(WorkOrderMaterialAllocation)
            .filter(WorkOrderMaterialAllocation.work_order_id == wo_id)
            .all()
        )
        assert ties == []
        assert sheet.id is not None  # the match existed and was still not acted on


# --------------------------------------------------------------------------- #
# A suggestion may never fail a preview
# --------------------------------------------------------------------------- #
class TestMatcherFailureDegrades:
    def test_matcher_exception_returns_the_pre_feature_payload(self, client, db_session, monkeypatch):
        """``match_sheet_parts`` blowing up must cost 42 manual picks, not the upload."""

        def _boom(*args, **kwargs):
            raise RuntimeError("catalog read exploded")

        monkeypatch.setattr(work_orders_endpoint, "match_sheet_parts", _boom)

        admin = make_user(db_session)
        parent = make_parent_work_order(db_session)
        make_sheet_part(db_session, part_number="0.250X48X96-A36")

        resp = _preview_wo(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf", "05750.pdf"))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        data = resp.json()
        assert data["nest_count"] == 2
        assert data["total_planned_runs"] == 6
        assert {row["cnc_number"] for row in data["nests"]} == {"05749", "05750"}
        # Exactly today's shape: no suggestion anywhere, all roll-ups zero.
        assert all(row["sheet_suggestion"] is None for row in data["nests"])
        assert data["suggested_row_count"] == 0
        assert data["shortlist_row_count"] == 0
        assert data["short_stock_row_count"] == 0

    def test_resolver_exception_also_degrades_without_losing_the_rows(self, client, db_session, monkeypatch):
        """The resolver contracts never to raise; the preview does not rely on that.

        If it ever does, the whole suggestion pass is dropped rather than half-
        applied -- a partially re-ranked shortlist is a worse thing to show a
        planner than no shortlist.
        """

        def _boom(*args, **kwargs):
            raise RuntimeError("resolver exploded")

        monkeypatch.setattr(work_orders_endpoint, "resolve_ambiguous_sheet_matches", _boom)

        admin = make_user(db_session)
        parent = make_parent_work_order(db_session)
        make_sheet_part(db_session, part_number="0.250X48X96-A36")

        resp = _preview_wo(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        data = resp.json()
        assert data["nest_count"] == 1
        assert data["nests"][0]["sheet_suggestion"] is None
        assert data["suggested_row_count"] == 0


# --------------------------------------------------------------------------- #
# Tenant isolation (invariant 1)
# --------------------------------------------------------------------------- #
class TestTenantScoping:
    def test_cross_tenant_sheet_is_never_suggested_on_the_wo_route(self, client, db_session):
        admin_a = make_user(db_session, company_id=COMPANY_A)
        parent = make_parent_work_order(db_session, company_id=COMPANY_A)
        foreign = make_sheet_part(
            db_session, part_number="0.250X48X96-A36", company_id=COMPANY_B, name="Other tenant sheet"
        )
        stock(db_session, foreign, 500)

        resp = _preview_wo(client, headers_for(admin_a), parent.id, _pdf_zip("05749.pdf"))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        data = resp.json()
        suggestion = data["nests"][0]["sheet_suggestion"]
        assert suggestion["status"] == "unmatched"
        assert suggestion["auto_fill_part_id"] is None
        assert suggestion["candidates"] == []
        # And the other tenant's part number is not leaked in prose either.
        assert "Other tenant sheet" not in resp.text
        assert foreign.id is not None

    def test_standalone_preview_is_tenant_scoped(self, client, db_session):
        """The standalone route grew a ``db`` session for this feature.

        It has no work-order id to scope against, so the ONLY thing standing
        between it and another tenant's catalog is ``get_current_company_id``
        being threaded to the matcher. Pin it directly.
        """
        admin_a = make_user(db_session, company_id=COMPANY_A)
        foreign = make_sheet_part(db_session, part_number="0.250X48X96-A36", company_id=COMPANY_B)
        stock(db_session, foreign, 500)

        resp = _preview_standalone(client, headers_for(admin_a), _pdf_zip("05749.pdf"))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        suggestion = resp.json()["nests"][0]["sheet_suggestion"]
        assert suggestion["candidates"] == []
        assert suggestion["auto_fill_part_id"] is None

    def test_each_tenant_sees_only_its_own_matching_sheet(self, client, db_session):
        """Positive control for the two negatives above.

        Same part number in both tenants: each caller must be pre-filled with
        THEIR row id, which is what proves the empty results above come from
        scoping rather than from the fixture failing to match at all.
        """
        admin_a = make_user(db_session, company_id=COMPANY_A)
        admin_b = make_user(db_session, company_id=COMPANY_B)
        sheet_a = make_sheet_part(db_session, part_number="0.250X48X96-A36", company_id=COMPANY_A)
        sheet_b = make_sheet_part(db_session, part_number="0.250X48X96-A36", company_id=COMPANY_B)
        assert sheet_a.id != sheet_b.id

        resp_a = _preview_standalone(client, headers_for(admin_a), _pdf_zip("05749.pdf"))
        resp_b = _preview_standalone(client, headers_for(admin_b), _pdf_zip("05749.pdf"))

        assert resp_a.status_code == status.HTTP_200_OK, resp_a.text
        assert resp_b.status_code == status.HTTP_200_OK, resp_b.text
        assert resp_a.json()["nests"][0]["sheet_suggestion"]["auto_fill_part_id"] == sheet_a.id
        assert resp_b.json()["nests"][0]["sheet_suggestion"]["auto_fill_part_id"] == sheet_b.id


# --------------------------------------------------------------------------- #
# The internal score never reaches the wire
# --------------------------------------------------------------------------- #
class TestInternalFieldsNotSerialized:
    @pytest.mark.parametrize("second_sheet", [None, "0.250-48X96-A36-ALT"])
    def test_alloy_score_is_absent_from_the_serialized_preview(self, client, db_session, second_sheet):
        """Matched AND ambiguous rows: ``alloy_score`` is dropped either way.

        Asserted against the raw response TEXT, not the parsed dict, so a nested
        occurrence anywhere in the payload fails the test.
        """
        admin = make_user(db_session)
        parent = make_parent_work_order(db_session)
        make_sheet_part(db_session, part_number="0.250X48X96-A36")
        if second_sheet:
            make_sheet_part(db_session, part_number=second_sheet)

        resp = _preview_wo(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert "alloy_score" not in resp.text
        candidates = resp.json()["nests"][0]["sheet_suggestion"]["candidates"]
        assert candidates, "fixture must produce at least one candidate for this to mean anything"
        for candidate in candidates:
            assert "alloy_score" not in candidate

    def test_suggestion_carries_no_datetime_field(self, client, db_session):
        """``prior_tie_count`` is the history signal; there is no timestamp here.

        ``UTCModel``'s ``Z``-suffixing encoder is model-level config, and these
        schemas nest inside a plain ``BaseModel`` response -- a datetime would not
        reliably serialize as UTC ``Z``. Guard the shape so one cannot be added
        without this failing.
        """
        admin = make_user(db_session)
        parent = make_parent_work_order(db_session)
        make_sheet_part(db_session, part_number="0.250X48X96-A36")

        resp = _preview_wo(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        suggestion = resp.json()["nests"][0]["sheet_suggestion"]
        assert set(suggestion) == {"status", "auto_fill_part_id", "candidates", "diagnostic"}
        candidate = suggestion["candidates"][0]
        assert "prior_tie_count" in candidate
        assert not [key for key in candidate if key.endswith("_at") or key.endswith("_date")]


# --------------------------------------------------------------------------- #
# sheet_match_provenance: audit-only, never a resolver, never a tie
# --------------------------------------------------------------------------- #
def _wo_import_audit(db: Session, wo_id: int) -> AuditLog:
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == "work_order", AuditLog.resource_id == wo_id)
        .order_by(AuditLog.id.desc())
        .first()
    )


class TestSheetMatchProvenance:
    def test_valid_provenance_lands_on_the_wo_audit_row(self, client, db_session):
        admin = make_user(db_session)
        make_laser_work_center(db_session)

        resp = _import_standalone(
            client,
            headers_for(admin),
            _pdf_zip("05749.pdf"),
            rows=[_row()],
            provenance=json.dumps({"05749.pdf": "auto"}),
        )

        assert resp.status_code == status.HTTP_200_OK, resp.text
        wo_id = resp.json()["child_work_order"]["id"]
        entry = _wo_import_audit(db_session, wo_id)
        assert entry is not None
        assert entry.extra_data.get("sheet_match_provenance") == {"05749.pdf": "auto"}

    def test_absent_provenance_leaves_extra_data_untouched(self, client, db_session):
        """No key at all -- not an empty dict.

        An import from a client that never heard of suggestions must write the
        exact ``extra_data`` it wrote before the feature existed.
        """
        admin = make_user(db_session)
        make_laser_work_center(db_session)

        resp = _import_standalone(client, headers_for(admin), _pdf_zip("05749.pdf"), rows=[_row()])

        assert resp.status_code == status.HTTP_200_OK, resp.text
        wo_id = resp.json()["child_work_order"]["id"]
        entry = _wo_import_audit(db_session, wo_id)
        assert "sheet_match_provenance" not in (entry.extra_data or {})

    @pytest.mark.parametrize(
        "provenance",
        [
            "{not json at all",
            json.dumps(["05749.pdf"]),
            json.dumps({"05749.pdf": "made-up-value"}),
            json.dumps({"05749.pdf": {"nested": "object"}}),
        ],
    )
    def test_malformed_provenance_is_discarded_not_rejected(self, client, db_session, provenance):
        """Telemetry never fails an import, and never smuggles free text into audit."""
        admin = make_user(db_session)
        make_laser_work_center(db_session)

        resp = _import_standalone(
            client, headers_for(admin), _pdf_zip("05749.pdf"), rows=[_row()], provenance=provenance
        )

        assert resp.status_code == status.HTTP_200_OK, resp.text
        wo_id = resp.json()["child_work_order"]["id"]
        entry = _wo_import_audit(db_session, wo_id)
        assert "sheet_match_provenance" not in (entry.extra_data or {})

    def test_provenance_creates_no_material_tie(self, client, db_session):
        """It is a breadcrumb, not an instruction.

        Naming a row ``auto`` says the planner accepted the server's pre-fill; it
        does not, on its own, tie anything. Only ``material_part_id`` does that.
        """
        admin = make_user(db_session)
        make_laser_work_center(db_session)
        make_sheet_part(db_session, part_number="0.250X48X96-A36")

        resp = _import_standalone(
            client,
            headers_for(admin),
            _pdf_zip("05749.pdf"),
            rows=[_row()],
            provenance=json.dumps({"05749.pdf": "auto"}),
        )

        assert resp.status_code == status.HTTP_200_OK, resp.text
        wo_id = resp.json()["child_work_order"]["id"]
        assert (
            db_session.query(WorkOrderMaterialAllocation)
            .filter(WorkOrderMaterialAllocation.work_order_id == wo_id)
            .count()
            == 0
        )
