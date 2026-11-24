"""AI-external API tokens at the API layer (Claude-plugin brief P0.3).

An API token minted ``ai_external`` (the default for a read-only token) answers to an
outside AI service. Pinned here, against a seeded ITAR customer whose every text field
carries a CANARY:

- **Canary conformance, per allowlisted route.** Every entry of
  ``AI_EXTERNAL_ALLOWED_ROUTES`` is called as an AI-external token and the canary must be
  absent from the RAW response bytes (gzip too), while a person's session still sees it.
  A guard test fails CI when an allowlisted route has no case here.
- **Redacted, not dropped.** The ITAR work order, part, NCR and quote come back with their
  ids, statuses, dates and quantities intact, every other text ``"[export-controlled]"`` and
  ``"export_controlled": true`` -- and in full to a person and to a full token.
- **Positive clearance.** A cleared customer's records pass in full (a name matched by
  code, case and whitespace ignored); a typo'd name, and records the ERP cannot tie to a
  customer (standalone laser work order, an NCR naming nothing, a CAR nothing references),
  are redacted as unlinkable.
- **Refusals.** Egress off -> 403 on every route (gate 1), a route off the allowlist -> 403
  (gate 4), writes and denied tags keep the read-only texts (gates 2 / 3), non-JSON bytes
  -> 403 for every read-only token. Each AI-external refusal is ledgered.
- **The ledger.** One ``controlled_access_events`` row per withheld record per request,
  naming the token; a failed ledger write (or index build) is 503 with nothing sent.
- ``If-None-Match`` never earns an API token a 304, and tenants never see each other's
  classification.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterator, List, Tuple

import pytest
from fastapi import Depends
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import event, func
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.api import deps
from app.core.config import settings
from app.core.credentials import READ_ONLY_ROUTE_DENIED_DETAIL, READ_ONLY_ROUTE_WRITES_DETAIL
from app.core.export_control import (
    AI_EGRESS_DISABLED_DETAIL,
    AI_EXTERNAL_ALLOWED_ROUTES,
    AI_EXTERNAL_QUERY_DENIED_DETAIL,
    AI_EXTERNAL_QUERY_PARAMS,
    AI_EXTERNAL_ROUTE_DENIED_DETAIL,
    EXPORT_CONTROL_FAILED_DETAIL,
    EXPORT_CONTROL_STATE_KEY,
    READ_ONLY_FILE_REFUSED_DETAIL,
    REDACTED_VALUE,
    ExportControlMarker,
    ai_external_error_detail,
)
from app.core.security import create_api_token
from app.core.time_utils import central_today
from app.main import app
from app.middleware.export_control import (
    CONDITIONAL_REQUEST_HEADERS,
    ExportControlMiddleware,
    bearer_is_api_token,
    bearer_is_narrowed_api_token,
)
from app.models.api_token import ApiToken
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.downtime import DowntimeEvent
from app.models.governance import ControlledAccessEvent, CustomerContract
from app.models.inventory import InventoryTransaction
from app.models.maintenance import MaintenanceAsset, MaintenanceWorkOrder
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.quality import NonConformanceReport
from app.models.rfq_quote import QuoteEstimate, QuoteLineSummary, RfqPackage
from app.models.user import User, UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker
from app.services import export_control_service, llm_client, notification_outbox
from app.services.notification_catalog import SOURCE_EVENT_TYPE_TO_KEY
from tests.api.completion_test_helpers import make_closed_time_entry
from tests.api.export_control_helpers import (
    CANARY,
    CLEAR_CUSTOMER_CODE,
    CLEAR_TEXT,
    NUMERIC_CANARIES,
    World,
    bearer,
    contains_canary,
    contains_numeric_canary,
    could_occur_in_iso_timestamp,
    find_record,
    iso_timestamp_containing,
    issue_token,
    make_viewer,
    seed_world,
    strip_iso_instants,
)
from tests.api.kiosk_test_helpers import user_headers

pytestmark = pytest.mark.api

API = settings.API_V1_PREFIX.rstrip("/")


@pytest.fixture
def world(db_session: Session) -> World:
    return seed_world(db_session)


@pytest.fixture
def viewer(db_session: Session) -> User:
    """The "Werco Assistant" user the connector's token is bound to: a Viewer."""
    return make_viewer(db_session)


@pytest.fixture
def ai_token(client: TestClient, admin_headers: dict, viewer: User) -> Tuple[int, str]:
    """(row id, token): read-only -- and therefore, by default, AI-external."""
    token_id, token = issue_token(client, admin_headers, viewer, scope="read_only")
    return token_id, token


def _events(db: Session, **filters: Any) -> List[ControlledAccessEvent]:
    db.expire_all()
    query = db.query(ControlledAccessEvent)
    for key, value in filters.items():
        query = query.filter(getattr(ControlledAccessEvent, key) == value)
    return query.order_by(ControlledAccessEvent.id).all()


# --------------------------------------------------------------------------- canary conformance

UrlFor = Callable[[World], str]


def _url(template: str) -> UrlFor:
    def build(w: World) -> str:
        return API + template.format(
            work_order_id=w.itar_wo.id,
            wo_number=w.itar_wo.work_order_number,
            operation_id=w.itar_op.id,
            event_id=w.itar_downtime.id,
            work_center_id=w.work_center.id,
            record_id=w.oee_record.id,
            part_id=w.itar_part.id,
            part_number=w.itar_part.part_number,
            routing_id=w.itar_routing.id,
            ncr_id=w.itar_ncr.id,
            car_id=w.itar_car.id,
            fai_id=w.itar_fai.id,
            quote_id=w.itar_quote.id,
            vendor_id=w.vendor.id,
            po_id=w.itar_po.id,
            receipt_id=w.itar_receipt.id,
            wo_id=w.maintenance_wo.id,
        )

    return build


# (METHOD, allowlisted template) -> (URL builder, does a PERSON's response carry the canary?)
# "True" proves the case is meaningful: the controlled text WOULD be in the bytes unredacted.
CANARY_CASES: Dict[Tuple[str, str], Tuple[UrlFor, bool]] = {
    ("GET", "/work-orders/"): (_url("/work-orders/"), True),
    ("GET", "/work-orders/browse"): (_url("/work-orders/browse"), True),
    ("GET", "/work-orders/{work_order_id}"): (_url("/work-orders/{work_order_id}"), True),
    ("GET", "/work-orders/by-number/{wo_number}"): (_url("/work-orders/by-number/{wo_number}"), True),
    ("GET", "/work-orders/{work_order_id}/timeline"): (_url("/work-orders/{work_order_id}/timeline"), True),
    ("GET", "/shop-floor/operations"): (_url("/shop-floor/operations"), True),
    ("GET", "/shop-floor/operations/{operation_id}"): (_url("/shop-floor/operations/{operation_id}"), True),
    ("GET", "/downtime/"): (_url("/downtime/"), True),
    ("GET", "/downtime/active"): (_url("/downtime/active"), True),  # primed: an ITAR event still open
    ("GET", "/downtime/by-work-center"): (_url("/downtime/by-work-center"), False),
    ("GET", "/downtime/reason-codes"): (_url("/downtime/reason-codes"), False),
    ("GET", "/downtime/{event_id}"): (_url("/downtime/{event_id}"), True),
    ("GET", "/work-centers/"): (_url("/work-centers/"), False),
    ("GET", "/work-centers/types"): (_url("/work-centers/types"), False),
    ("GET", "/work-centers/{work_center_id}"): (_url("/work-centers/{work_center_id}"), False),
    ("GET", "/oee/records"): (_url("/oee/records"), True),
    ("GET", "/oee/records/{record_id}"): (_url("/oee/records/{record_id}"), True),
    ("GET", "/oee/dashboard"): (_url("/oee/dashboard"), False),
    ("GET", "/oee/trends"): (_url("/oee/trends"), False),
    ("GET", "/oee/six-big-losses/{work_center_id}"): (_url("/oee/six-big-losses/{work_center_id}"), False),
    ("GET", "/oee/targets"): (_url("/oee/targets"), False),
    ("GET", "/parts/"): (_url("/parts/"), True),
    ("GET", "/parts/{part_id}"): (_url("/parts/{part_id}"), True),
    ("GET", "/parts/by-number/{part_number}"): (_url("/parts/by-number/{part_number}"), True),
    ("GET", "/routing/"): (_url("/routing/"), True),
    ("GET", "/routing/{routing_id}"): (_url("/routing/{routing_id}"), True),
    ("GET", "/routing/by-part/{part_id}"): (_url("/routing/by-part/{part_id}"), True),
    ("GET", "/quality/ncr"): (_url("/quality/ncr"), True),
    ("GET", "/quality/ncr/{ncr_id}"): (_url("/quality/ncr/{ncr_id}"), True),
    ("GET", "/quality/car"): (_url("/quality/car"), True),
    ("GET", "/quality/car/{car_id}"): (_url("/quality/car/{car_id}"), True),
    ("GET", "/quality/fai"): (_url("/quality/fai"), True),
    ("GET", "/quality/fai/{fai_id}"): (_url("/quality/fai/{fai_id}"), True),
    ("GET", "/quality/fai/log"): (_url("/quality/fai/log"), True),
    ("GET", "/quotes/"): (_url("/quotes/"), True),
    ("GET", "/quotes/{quote_id}"): (_url("/quotes/{quote_id}"), True),
    ("GET", "/purchasing/vendors"): (_url("/purchasing/vendors"), False),
    ("GET", "/purchasing/vendors/{vendor_id}"): (_url("/purchasing/vendors/{vendor_id}"), False),
    # Primed: the vendor's re-evaluation is overdue and its ASL notes name the ITAR part number.
    ("GET", "/purchasing/vendors/reevaluation-due"): (_url("/purchasing/vendors/reevaluation-due"), True),
    ("GET", "/purchasing/purchase-orders"): (_url("/purchasing/purchase-orders"), False),
    ("GET", "/purchasing/purchase-orders/{po_id}"): (_url("/purchasing/purchase-orders/{po_id}"), True),
    ("GET", "/receiving/history"): (_url("/receiving/history"), True),
    ("GET", "/receiving/receipt/{receipt_id}"): (_url("/receiving/receipt/{receipt_id}"), True),
    ("GET", "/maintenance/work-orders"): (_url("/maintenance/work-orders"), True),
    ("GET", "/maintenance/work-orders/overdue"): (_url("/maintenance/work-orders/overdue"), True),
    ("GET", "/maintenance/work-orders/{wo_id}"): (_url("/maintenance/work-orders/{wo_id}"), True),
    # Reference data with no customer linkage: always served cleared (TestProductionCalendar).
    ("GET", "/production-calendar"): (_url("/production-calendar"), False),
    ("GET", "/production-calendar/resolve"): (_url("/production-calendar/resolve"), False),
}


def _open_itar_downtime(db: Session, w: World) -> DowntimeEvent:
    """A downtime event on the ITAR work order that is still OPEN (no end_time): what /downtime/active lists."""
    event = DowntimeEvent(
        work_center_id=w.work_center.id,
        work_order_id=w.itar_wo.id,
        start_time=datetime.utcnow() - timedelta(minutes=5),
        category="mechanical",
        description=f"{CANARY} open downtime description",
        reported_by=w.reporter.id,
        company_id=w.company_id,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _vendor_reevaluation_overdue(db: Session, w: World) -> None:
    """Put the seed vendor on the overdue re-evaluation list (P2.2) with ASL notes a person typed
    naming the controlled part number. The vendor is reference data -- cleared, served in full --
    so only the residual text pass stands between that part number and the token."""
    vendor = w.vendor
    vendor.approval_status = "approved"
    vendor.approved_scopes = ["machining"]
    vendor.approval_bases = ["as9100"]
    vendor.reevaluation_due = central_today() - timedelta(days=30)
    vendor.approval_notes = f"Approved for {w.itar_part.part_number} machining"
    db.commit()


# The status an AI-external token gets where it is not 200. A by-number part lookup that
# finds a WITHHELD part answers the same 404 a number nobody carries gets: a 200, even
# fully redacted, would confirm the probed part number exists (code review P0, finding 1).
CANARY_EXPECTED_STATUS: Dict[Tuple[str, str], int] = {
    ("GET", "/parts/by-number/{part_number}"): 404,
}

# Routes whose person-view carries the NUMERIC canaries (the ITAR FAI's nominal, tolerances and
# measured value; the ITAR NCR's actual / required value): proves the numeric check is not vacuous.
NUMERIC_CANARY_ROUTES = (
    "/quality/fai/{fai_id}",
    "/quality/ncr",
    "/quality/ncr/{ncr_id}",
)

# Extra rows a case needs before its controlled text can be in the response at all (the seed's
# downtime events are resolved, so without this /downtime/active would be empty -- a vacuous case).
CANARY_PRIMERS: Dict[Tuple[str, str], Callable[[Session, World], Any]] = {
    ("GET", "/downtime/active"): _open_itar_downtime,
    ("GET", "/purchasing/vendors/reevaluation-due"): _vendor_reevaluation_overdue,
}


class TestCanaryConformance:
    def test_every_allowlisted_route_has_a_canary_case(self):
        """The guard: an allowlist entry without a conformance case fails CI."""
        missing = sorted(AI_EXTERNAL_ALLOWED_ROUTES - set(CANARY_CASES))
        assert missing == [], f"allowlisted route(s) with no canary conformance case: {missing}"
        assert set(CANARY_CASES) == set(AI_EXTERNAL_ALLOWED_ROUTES), "a case for a route that is not allowlisted"

    def test_every_allowlisted_route_is_a_live_get_route(self):
        live = {
            (method, route.path_format)
            for route in app.routes
            if isinstance(route, APIRoute)
            for method in route.methods
        }
        stale = sorted(op for op in deps.AI_EXTERNAL_ALLOWED_OPERATIONS if op not in live)
        assert stale == [], f"allowlist entries no route serves: {stale}"
        assert {method for method, _path in AI_EXTERNAL_ALLOWED_ROUTES} == {"GET"}

    @pytest.mark.parametrize("operation", sorted(CANARY_CASES), ids=lambda op: op[1])
    def test_the_canary_never_reaches_an_ai_external_token(
        self, client: TestClient, db_session: Session, world: World, ai_token, auth_headers: dict, operation
    ):
        url_for, person_sees_canary = CANARY_CASES[operation]
        url = url_for(world)
        primer = CANARY_PRIMERS.get(operation)
        if primer is not None:
            primer(db_session, world)
        _id, token = ai_token
        # Prime the job's history the way a day on the floor does: a person opening the work
        # order reconciles it (its labor evidence starts the operation), which is what the
        # timeline then reports -- operation names and all.
        assert client.get(f"{API}/work-orders/{world.itar_wo.id}", headers=auth_headers).status_code == 200

        redacted = client.get(url, headers=bearer(token))
        assert redacted.status_code == CANARY_EXPECTED_STATUS.get(operation, 200), (url, redacted.text[:500])
        assert redacted.headers["content-type"].startswith("application/json")
        assert not contains_canary(redacted.content), f"{url} leaked the canary: {redacted.text[:800]}"
        assert not contains_numeric_canary(redacted.content), f"{url} leaked a dimension: {redacted.text[:800]}"
        assert "etag" not in redacted.headers

        person = client.get(url, headers=auth_headers)
        assert person.status_code == 200, (url, person.text[:500])
        if person_sees_canary:
            assert contains_canary(person.content), f"{url}: the case is vacuous -- a person sees no canary either"

    @pytest.mark.parametrize("template", NUMERIC_CANARY_ROUTES)
    def test_dimensions_and_measured_values_are_withheld_not_just_text(
        self, client: TestClient, world: World, ai_token, auth_headers: dict, template: str
    ):
        """Compliance review P0, finding 1 (blocker): inside a withheld record a numeric STRING
        under a non-text key passed -- an ITAR FAI's nominal, tolerances and measured value, an
        NCR's actual / required value. Numeric strings now stay only under a count / quantity /
        money / duration key."""
        url = _url(template)(world)
        person = client.get(url, headers=auth_headers)
        assert person.status_code == 200 and contains_numeric_canary(person.content), "vacuous: no dimension"
        _id, token = ai_token
        redacted = client.get(url, headers=bearer(token))
        assert redacted.status_code == 200
        assert not contains_numeric_canary(redacted.content), redacted.text[:800]
        if template == "/quality/fai/{fai_id}":
            body = redacted.json()
            (char,) = [c for c in body["characteristics"] if c["char_number"] == 1]
            for key in ("nominal", "tolerance_plus", "tolerance_minus", "actual_value"):
                assert char[key] == REDACTED_VALUE, key
            assert char["char_number"] == 1 and char["export_controlled"] is True

    def test_gzip_responses_are_redacted_before_compression(self, client: TestClient, world: World, ai_token):
        _id, token = ai_token
        response = client.get(f"{API}/work-orders/browse", headers={**bearer(token), "Accept-Encoding": "gzip"})
        assert response.status_code == 200
        assert response.headers.get("content-encoding") == "gzip", "large enough to be compressed"
        assert not contains_canary(response.content)  # httpx decodes; the compressed bytes are the redacted body
        assert not contains_canary(gzip.compress(response.content))


# --------------------------------------------------------------------------- redacted, not dropped


class TestRedactedRecords:
    @pytest.mark.parametrize(
        "path,record",
        [
            ("/work-orders/{id}", "itar_wo"),
            ("/parts/{id}", "itar_part"),
            ("/quality/ncr/{id}", "itar_ncr"),
            ("/quotes/{id}", "itar_quote"),
        ],
    )
    def test_the_brief_records_keep_ids_status_dates_and_quantities(
        self, client: TestClient, world: World, ai_token, auth_headers: dict, admin_headers: dict, path, record
    ):
        _id, token = ai_token
        row = getattr(world, record)
        url = API + path.format(id=row.id)
        person = client.get(url, headers=auth_headers).json()
        redacted = client.get(url, headers=bearer(token)).json()

        assert contains_canary(json.dumps(person).encode()), "a person's session gets the full record"
        assert not contains_canary(json.dumps(redacted).encode())
        assert redacted["export_controlled"] is True
        assert redacted["id"] == person["id"] == row.id
        assert set(redacted) == set(person) | {"export_controlled"}, "redacted, never dropped: every key is still there"
        for key, value in person.items():
            if isinstance(value, (int, float, bool)) or value is None:
                assert redacted[key] == value, f"{key}: numbers, booleans and nulls stay true"
            elif key == "status" or key.endswith(("_at", "_date")) or key in ("work_order_number", "ncr_number"):
                assert redacted[key] == value, f"{key}: statuses, dates and business numbers stay true"
        text_keys = {
            "itar_wo": ("customer_name", "notes", "special_instructions", "customer_po"),
            "itar_part": ("part_number", "name", "description", "customer_name", "drawing_number"),
            "itar_ncr": ("title", "description", "specification", "actual_value"),
            "itar_quote": ("customer_name", "notes", "customer_contact"),
        }[record]
        for key in text_keys:
            assert redacted[key] == REDACTED_VALUE, key

        # A full token is unaffected.
        full_id, full = issue_token(client, admin_headers, world.reporter)
        assert contains_canary(client.get(url, headers=bearer(full)).content)

    def test_quantities_on_the_work_order_list_stay_true(self, client: TestClient, world: World, ai_token):
        _id, token = ai_token
        rows = client.get(f"{API}/work-orders/", headers=bearer(token)).json()
        itar = find_record(rows, key="id", value=world.itar_wo.id)
        assert itar["export_controlled"] is True
        assert itar["work_order_number"] == world.itar_wo.work_order_number
        assert itar["quantity_ordered"] == 7.0 and itar["priority"] == 2
        assert itar["due_date"] == world.itar_wo.due_date.isoformat()
        assert itar["part_number"] == REDACTED_VALUE

    def test_a_legacy_ai_estimates_dimensions_are_withheld_as_numbers(
        self, client: TestClient, db_session: Session, world: World, ai_token, auth_headers: dict
    ):
        """Code review P0 round 2, finding 2: ``GET /quotes/{id}`` still serves a legacy AI
        estimate (no code writes them any more) as ``ai_estimate``. On a withheld quote its line
        summaries' ``flat_area`` (in2) and every number in its free-form JSON went out true --
        numbers were withheld only under a LISTED measurement key. They are an allowlist now."""
        package = RfqPackage(rfq_number="RFQ-ITAR-1", rfq_reference=f"{CANARY}-RFQ", company_id=world.company_id)
        db_session.add(package)
        db_session.flush()
        estimate = QuoteEstimate(
            rfq_package_id=package.id,
            quote_id=world.itar_quote.id,
            grand_total=5000.0,
            lead_time_min_days=5,
            lead_time_max_days=9,
            confidence_detail={"bend_radius_guess": 0.0625111, "profile_inches": 3.1415927},
            assumptions=[{"feature": "hole", "assumed_inches": 0.3125987, "assumed_mm": 7.9375013}],
            missing_specs=[{"field": "flange", "guess_in": 1.4142135}],
            company_id=world.company_id,
        )
        db_session.add(estimate)
        db_session.flush()
        db_session.add(
            QuoteLineSummary(
                quote_estimate_id=estimate.id,
                part_number=world.itar_part.part_number,
                part_name=f"{CANARY} estimate line",
                quantity=25,
                flat_area=7329.15287,
                cut_length=4321.98765,
                bend_count=7,
                hole_count=13,
                company_id=world.company_id,
            )
        )
        db_session.commit()
        dimensions = (
            b"7329.15287",
            b"4321.98765",
            b"0.3125987",
            b"7.9375013",
            b"1.4142135",
            b"3.1415927",
            b"0.0625111",
        )
        # The byte search below is only sound if no run-varying text in the response (the
        # quote's created_at / updated_at instants) can contain a needle by chance. b"7.94" and
        # b"3.141592" could, and b"7.94" did on 2026-11-24: refuse such a needle on every run.
        chance_prone = [value for value in dimensions if could_occur_in_iso_timestamp(value)]
        assert chance_prone == [], f"a timestamp can contain {chance_prone}: use 7+ fractional digits"
        url = f"{API}/quotes/{world.itar_quote.id}"
        person = client.get(url, headers=auth_headers)
        assert person.status_code == 200 and person.json()["ai_estimate"] is not None
        assert all(value in person.content for value in dimensions), "vacuous: the estimate is not served"
        _id, token = ai_token
        redacted = client.get(url, headers=bearer(token))
        assert redacted.status_code == 200
        leaked = [value for value in dimensions if value in redacted.content]
        assert leaked == [], redacted.text[:1500]
        body = redacted.json()["ai_estimate"]
        (line,) = body["line_summaries"]
        assert line["flat_area"] == REDACTED_VALUE and line["cut_length"] == REDACTED_VALUE
        assert (line["bend_count"], line["hole_count"], line["quantity"]) == (7, 13, 25.0), "counts stay true"
        assert body["totals"]["grand_total"] == 5000.0, "money stays true"
        assert (body["lead_time"]["min_days"], body["lead_time"]["max_days"]) == (5, 9)
        assert not contains_canary(redacted.content)

    def test_cleared_records_pass_in_full(self, client: TestClient, world: World, ai_token):
        _id, token = ai_token
        rows = client.get(f"{API}/work-orders/", headers=bearer(token)).json()
        clear = find_record(rows, key="id", value=world.clear_wo.id)
        assert "export_controlled" not in clear
        assert clear["part_number"] == world.clear_part.part_number
        assert clear["customer_name"] == world.clear_customer.name
        ncr = client.get(f"{API}/quality/ncr/{world.clear_ncr.id}", headers=bearer(token)).json()
        assert "export_controlled" not in ncr and ncr["title"].startswith(CLEAR_TEXT)

    def test_free_text_naming_a_controlled_part_is_scrubbed_where_the_record_itself_passes(
        self, client: TestClient, world: World, ai_token
    ):
        """OEE and maintenance records are not customer data, but a person typed the part number."""
        _id, token = ai_token
        mwo = client.get(f"{API}/maintenance/work-orders/{world.maintenance_wo.id}", headers=bearer(token)).json()
        assert "export_controlled" not in mwo
        assert mwo["title"] == "Replace hydraulic seal"
        assert mwo["description"] == f"found while running {REDACTED_VALUE}"


# --------------------------------------------------------------------------- P2a: CAR form, quote Rev, FAI log

# The CAR form's text fields (migration 122). Inside a withheld CAR every one is withheld;
# the cost buckets and their computed total are money and stay true.
CAR_FORM_TEXT_KEYS = (
    "initiation_reason_other",
    "rma_number",
    "vendor_po_number",
    "management_action",
    "vendor_approval_name",
    "vendor_approval_title",
    "customer_approval_name",
)
CAR_MONEY_KEYS = ("cost_labor", "cost_material", "cost_outside_services", "cost_shipping", "cost_total")
CAR_LEAK = "CARLEAKTEXT"


class TestCarFormQuoteRevisionAndFaiLog:
    """The P2a records (CAR form fields, quote-line drawing revisions, the FAI log of record),
    built blind to P0's export control: their new text is withheld inside a withheld record,
    their money stays true, and a CAR is judged by its OWN part and customer too."""

    def test_a_withheld_car_withholds_every_form_text_and_keeps_its_money(
        self, client: TestClient, world: World, ai_token, auth_headers: dict
    ):
        url = f"{API}/quality/car/{world.itar_car.id}"
        person = client.get(url, headers=auth_headers).json()
        for key in CAR_FORM_TEXT_KEYS:
            assert CANARY in person[key], f"vacuous: {key} carries no canary"
        assert (person["cost_labor"], person["cost_total"]) == ("125.50", "165.50")
        _id, token = ai_token
        response = client.get(url, headers=bearer(token))
        assert response.status_code == 200
        assert not contains_canary(response.content), response.text[:800]
        body = response.json()
        assert body["export_controlled"] is True and body["id"] == world.itar_car.id
        for key in CAR_FORM_TEXT_KEYS + ("title", "problem_description", "root_cause", "initiation_reason"):
            assert body[key] == REDACTED_VALUE, key
        for key in CAR_MONEY_KEYS:
            assert body[key] == person[key], f"{key}: money stays true"
        assert (body["car_number"], body["status"], body["priority"]) == (
            person["car_number"],
            person["status"],
            person["priority"],
        )

    def test_a_car_escalated_from_a_cleared_ncr_is_withheld_once_its_customer_is_controlled(
        self, client: TestClient, db_session: Session, world: World, ai_token, admin_headers: dict
    ):
        """The lead's cross-stream finding: P0 placed a CAR only through the NCRs and complaints
        that reference it, but P2a gave a CAR its own customer and part. A CAR raised from a
        CLEARED NCR whose customer is then set to the ITAR customer must be withheld."""
        created = client.post(f"{API}/quality/ncr/{world.clear_ncr.id}/create-car", headers=admin_headers)
        assert created.status_code == 200, created.text
        car_id = created.json()["id"]
        assert created.json()["part_id"] == world.clear_part.id, "prefilled from the cleared NCR"
        url = f"{API}/quality/car/{car_id}"
        form = {
            "rma_number": f"{CAR_LEAK}-RMA",
            "management_action": f"{CAR_LEAK} management action",
            "vendor_approval_name": f"{CAR_LEAK} vendor approver",
            "customer_approval_name": f"{CAR_LEAK} customer approver",
            "cost_labor": "80.00",
            "version": 0,  # required by CARUpdate, ignored (CARs carry no version column)
        }
        assert client.put(url, json=form, headers=admin_headers).status_code == 200
        _id, token = ai_token

        cleared = client.get(url, headers=bearer(token))
        assert cleared.status_code == 200
        assert "export_controlled" not in cleared.json(), "every link clears: served in full"
        assert cleared.json()["rma_number"] == f"{CAR_LEAK}-RMA"

        moved = client.put(url, json={"customer_id": world.itar_customer.id, "version": 0}, headers=admin_headers)
        assert moved.status_code == 200, moved.text
        leaks = (
            CAR_LEAK.encode(),
            world.itar_customer.name.encode(),
            world.clear_part.part_number.encode(),
        )
        person = client.get(url, headers=admin_headers)
        assert person.json()["customer_name"] == world.itar_customer.name
        assert person.json()["part_number"] == world.clear_part.part_number
        for raw in leaks:
            assert raw in person.content, f"a person sees the CAR in full: {raw!r}"

        for path in (url, f"{API}/quality/car"):
            redacted = client.get(path, headers=bearer(token))
            assert redacted.status_code == 200, path
            for raw in leaks:
                assert raw not in redacted.content, f"{path} leaked {raw!r}: {redacted.text[:800]}"
            assert not contains_canary(redacted.content)
        body = client.get(url, headers=bearer(token)).json()
        assert body["export_controlled"] is True
        assert (body["id"], body["customer_id"], body["part_id"]) == (
            car_id,
            world.itar_customer.id,
            world.clear_part.id,
        )
        for key in ("customer_name", "part_number", "rma_number", "management_action", "vendor_approval_name"):
            assert body[key] == REDACTED_VALUE, key
        assert body["customer_approval_name"] == REDACTED_VALUE
        assert body["cost_labor"] == "80.00" and body["cost_total"] == "80.00", "money stays true"
        events = _events(db_session, record_type="car", record_id=car_id)
        assert events and {event.denial_reason for event in events} == {"export_controlled_customer"}
        assert {event.data_classification for event in events} == {"itar_export_controlled"}

    def test_quote_line_revisions_are_withheld_inside_a_withheld_quote(
        self, client: TestClient, world: World, ai_token, auth_headers: dict
    ):
        url = f"{API}/quotes/{world.itar_quote.id}"
        (person_line,) = client.get(url, headers=auth_headers).json()["lines"]
        assert CANARY in person_line["part_revision"]
        assert person_line["part_current_revision"] == "A", "the part's revision today (a part revision is 1-5 chars)"
        _id, token = ai_token
        response = client.get(url, headers=bearer(token))
        assert response.status_code == 200 and not contains_canary(response.content)
        (line,) = response.json()["lines"]
        assert line["part_revision"] == REDACTED_VALUE
        assert line["part_current_revision"] == REDACTED_VALUE
        assert (line["quantity"], line["line_number"]) == (person_line["quantity"], person_line["line_number"])
        clear = client.get(f"{API}/quotes/{world.clear_quote.id}", headers=bearer(token)).json()
        assert "export_controlled" not in clear, "a cleared quote keeps its line revisions"

    def test_the_fai_log_judges_each_row_like_the_fai_it_names(
        self, client: TestClient, db_session: Session, world: World, ai_token, auth_headers: dict
    ):
        person = client.get(f"{API}/quality/fai/log", headers=auth_headers)
        assert person.status_code == 200
        person_itar = find_record(person.json(), key="fai_id", value=world.itar_fai.id)
        assert person_itar is not None and CANARY in person_itar["part_number"]
        assert CANARY in person_itar["customer"] and CANARY in person_itar["part_revision"]

        _id, token = ai_token
        response = client.get(f"{API}/quality/fai/log", headers=bearer(token))
        assert response.status_code == 200
        assert not contains_canary(response.content), response.text[:800]
        rows = response.json()
        assert len(rows) == len(person.json()), "rows are redacted, never dropped"
        itar = find_record(rows, key="fai_id", value=world.itar_fai.id)
        assert itar["export_controlled"] is True
        for key in ("part_number", "part_name", "customer", "part_revision", "part_current_revision"):
            assert itar[key] == REDACTED_VALUE, key
        for key in (
            "fai_number",
            "job_number",
            "status",
            "log_date",
            "part_id",
            "work_order_id",
            "total_characteristics",
            "is_complete",
            "customer_approved",
        ):
            assert itar[key] == person_itar[key], f"{key}: ids, numbers, statuses, dates and counts stay true"
        assert itar["job_number"] == world.itar_wo.work_order_number

        clear = find_record(rows, key="fai_id", value=world.clear_fai.id)
        assert "export_controlled" not in clear
        assert clear["part_number"] == world.clear_part.part_number
        assert clear["customer"] == world.clear_customer.name
        events = _events(db_session, record_type="fai", record_id=world.itar_fai.id)
        assert events and {event.denial_reason for event in events} == {"export_controlled_customer"}

    def test_the_car_and_quote_pdfs_never_reach_a_read_only_token(
        self, client: TestClient, world: World, admin_headers: dict, auth_headers: dict, viewer: User
    ):
        """GET /quality/car/{id}/pdf answers PDF bytes: the read-only byte rule refuses them, and
        an AI-external token never reaches the route (it is off the allowlist). The quote PDF is
        a POST: the read-only method gate refuses it before the send gate is even consulted."""
        car_pdf = f"{API}/quality/car/{world.itar_car.id}/pdf"
        person = client.get(car_pdf, headers=auth_headers)
        assert person.status_code == 200 and person.headers["content-type"] == "application/pdf"

        _id, read_only = issue_token(client, admin_headers, viewer, scope="read_only", ai_external=False)
        refused = client.get(car_pdf, headers=bearer(read_only))
        assert refused.status_code == 403
        assert refused.json() == {"detail": READ_ONLY_FILE_REFUSED_DETAIL}
        assert b"%PDF" not in refused.content

        _id, ai_external = issue_token(client, admin_headers, viewer, scope="read_only", label="ai")
        refused = client.get(car_pdf, headers=bearer(ai_external))
        assert refused.status_code == 403
        assert refused.json() == {"detail": AI_EXTERNAL_ROUTE_DENIED_DETAIL}

        for token in (read_only, ai_external):
            quote_pdf = client.post(f"{API}/quotes/{world.clear_quote.id}/generate-pdf", headers=bearer(token))
            assert quote_pdf.status_code == 403
            assert quote_pdf.json() == {"detail": READ_ONLY_ROUTE_WRITES_DETAIL}


MOVE_LEAK = "MOVEDLINKLEAK"


class TestCarLinksThatMove:
    """Red-team finding (2026-11-16): a CAR raised from an ITAR record copies its title and
    description, but the link that placed it MOVES -- the CAR is voided and the record
    escalated again, or the NCR's ``car_id`` is re-pointed or cleared. Judged by its CURRENT
    referrers plus its own generic-stock part (or cleared customer), the first CAR was then
    served in full to an AI-external token. Each case below is the ITAR job's record with a
    generic-stock part (a raw-material sheet, blank customer), so only the work order
    controls it; the CAR must stay withheld AS ITAR (not merely as unlinkable) once the link
    has moved, while a person still reads it in full."""

    @staticmethod
    def _itar_job_ncr(client: TestClient, admin_headers: dict, world: World) -> int:
        response = client.post(
            f"{API}/quality/ncr",
            json={
                "part_id": world.material_part.id,
                "work_order_id": world.itar_wo.id,
                "source": "in_process",
                "title": f"{MOVE_LEAK} cracked sheet on the controlled bracket",
                "description": f"{MOVE_LEAK} program detail typed by the inspector",
            },
            headers=admin_headers,
        )
        assert response.status_code in (200, 201), response.text
        return response.json()["id"]

    @staticmethod
    def _escalate(client: TestClient, admin_headers: dict, ncr_id: int) -> int:
        response = client.post(f"{API}/quality/ncr/{ncr_id}/create-car", headers=admin_headers)
        assert response.status_code == 200, response.text
        return response.json()["id"]

    @staticmethod
    def _void(client: TestClient, admin_headers: dict, car_id: int) -> None:
        response = client.post(
            f"{API}/quality/car/{car_id}/void", json={"reason": "raised in error"}, headers=admin_headers
        )
        assert response.status_code == 200, response.text

    @staticmethod
    def _assert_withheld_as_itar(
        client: TestClient, db_session: Session, token: str, admin_headers: dict, car_id: int
    ) -> None:
        url = f"{API}/quality/car/{car_id}"
        person = client.get(url, headers=admin_headers)
        assert person.status_code == 200 and MOVE_LEAK.encode() in person.content, "vacuous: no leak text"
        db_session.expire_all()
        before = db_session.query(func.max(ControlledAccessEvent.id)).scalar() or 0
        for path in (url, f"{API}/quality/car"):
            response = client.get(path, headers=bearer(token))
            assert response.status_code == 200, path
            assert MOVE_LEAK.encode() not in response.content, f"{path} leaked: {response.text[:800]}"
        body = client.get(url, headers=bearer(token)).json()
        assert body["export_controlled"] is True and body["id"] == car_id
        assert body["title"] == REDACTED_VALUE and body["problem_description"] == REDACTED_VALUE
        events = [event for event in _events(db_session, record_type="car", record_id=car_id) if event.id > before]
        assert events, "no ledger row for the withheld CAR"
        # ITAR, not "unknown": the CAR is placed by the ITAR record it came from, not merely
        # withheld because nothing else ties it to a customer.
        assert {event.data_classification for event in events} == {"itar_export_controlled"}
        assert {event.denial_reason for event in events} == {"export_controlled_customer"}

    def test_a_voided_car_stays_withheld_after_the_ncr_is_escalated_again(
        self, client: TestClient, db_session: Session, world: World, ai_token, admin_headers: dict
    ):
        _id, token = ai_token
        ncr_id = self._itar_job_ncr(client, admin_headers, world)
        first = self._escalate(client, admin_headers, ncr_id)
        self._assert_withheld_as_itar(client, db_session, token, admin_headers, first)

        self._void(client, admin_headers, first)
        second = self._escalate(client, admin_headers, ncr_id)
        assert second != first
        for car_id in (first, second):
            self._assert_withheld_as_itar(client, db_session, token, admin_headers, car_id)

    def test_a_car_stays_withheld_after_the_ncr_is_re_pointed_or_unlinked(
        self, client: TestClient, db_session: Session, world: World, ai_token, admin_headers: dict
    ):
        _id, token = ai_token
        ncr_id = self._itar_job_ncr(client, admin_headers, world)
        first = self._escalate(client, admin_headers, ncr_id)
        # Consolidate under another (open) CAR: the first CAR is open and has no referrer now.
        moved = client.put(
            f"{API}/quality/ncr/{ncr_id}", json={"version": 0, "car_id": world.orphan_car.id}, headers=admin_headers
        )
        assert moved.status_code == 200, moved.text
        self._assert_withheld_as_itar(client, db_session, token, admin_headers, first)

        # Type ITAR detail into the CAR the NCR now names, then unlink the NCR from it too.
        typed = client.put(
            f"{API}/quality/car/{world.orphan_car.id}",
            json={"management_action": f"{MOVE_LEAK} action typed while linked", "version": 0},
            headers=admin_headers,
        )
        assert typed.status_code == 200, typed.text
        cleared = client.put(f"{API}/quality/ncr/{ncr_id}", json={"version": 0, "car_id": None}, headers=admin_headers)
        assert cleared.status_code == 200, cleared.text
        db_session.expire_all()
        assert db_session.get(NonConformanceReport, ncr_id).car_id is None
        for car_id in (first, world.orphan_car.id):
            self._assert_withheld_as_itar(client, db_session, token, admin_headers, car_id)

    def test_a_voided_car_stays_withheld_after_the_complaint_raises_another(
        self, client: TestClient, db_session: Session, world: World, ai_token, admin_headers: dict
    ):
        """The complaint names the CLEARED customer, so the CAR's own customer and snapshot
        clear: only the complaint's ITAR work order ever placed it."""
        _id, token = ai_token
        created = client.post(
            f"{API}/complaints/complaints/",
            json={
                "customer_id": world.clear_customer.id,
                "customer_name": world.clear_customer.name,
                "part_id": world.material_part.id,
                "work_order_id": world.itar_wo.id,
                "title": f"{MOVE_LEAK} returned brackets",
                "description": f"{MOVE_LEAK} customer report naming the program",
            },
            headers=admin_headers,
        )
        assert created.status_code == 200, created.text
        complaint_id = created.json()["id"]

        def raise_car() -> int:
            response = client.post(f"{API}/complaints/complaints/{complaint_id}/create-car", headers=admin_headers)
            assert response.status_code == 200, response.text
            return response.json()["car_id"]

        first = raise_car()
        self._assert_withheld_as_itar(client, db_session, token, admin_headers, first)
        self._void(client, admin_headers, first)
        second = raise_car()
        assert second != first
        for car_id in (first, second):
            self._assert_withheld_as_itar(client, db_session, token, admin_headers, car_id)


# --------------------------------------------------------------------------- positive clearance


class TestPositiveClearance:
    def test_a_typo_in_the_customer_name_is_withheld_as_unlinkable(
        self, client: TestClient, db_session: Session, world: World, ai_token
    ):
        world.clear_wo.customer_name = "Open Comerce Co"  # nobody's name: cannot be cleared
        db_session.commit()
        _id, token = ai_token
        body = client.get(f"{API}/work-orders/{world.clear_wo.id}", headers=bearer(token)).json()
        assert body["export_controlled"] is True and body["notes"] == REDACTED_VALUE
        events = _events(db_session, record_type="work_order", record_id=world.clear_wo.id)
        assert events and events[-1].denial_reason == "unlinkable_record"
        assert events[-1].data_classification == "unknown"
        # The unresolvable name itself may be a controlled customer misspelled: it never
        # surfaces anywhere -- not even in the browse page's customer facet.
        browse = client.get(f"{API}/work-orders/browse", headers=bearer(token))
        assert browse.status_code == 200
        assert b"Open Comerce Co" not in browse.content

    def test_a_name_matching_the_customer_code_any_case_or_spacing_clears(
        self, client: TestClient, db_session: Session, world: World, ai_token
    ):
        world.clear_wo.customer_name = f"  {CLEAR_CUSTOMER_CODE.lower()}  "
        db_session.commit()
        _id, token = ai_token
        body = client.get(f"{API}/work-orders/{world.clear_wo.id}", headers=bearer(token)).json()
        assert "export_controlled" not in body
        assert body["notes"].startswith(CLEAR_TEXT)

    def test_records_the_erp_cannot_tie_to_a_customer_are_redacted(
        self, client: TestClient, db_session: Session, world: World, ai_token
    ):
        _id, token = ai_token
        laser = client.get(f"{API}/work-orders/{world.laser_wo.id}", headers=bearer(token)).json()
        assert laser["export_controlled"] is True and laser["notes"] == REDACTED_VALUE
        assert laser["status"] == "released" and laser["quantity_ordered"] == 3.0
        ncr = client.get(f"{API}/quality/ncr/{world.orphan_ncr.id}", headers=bearer(token)).json()
        assert ncr["export_controlled"] is True and ncr["title"] == REDACTED_VALUE
        car = client.get(f"{API}/quality/car/{world.orphan_car.id}", headers=bearer(token)).json()
        assert car["export_controlled"] is True and car["title"] == REDACTED_VALUE
        for record_type, record_id in (
            ("work_order", world.laser_wo.id),
            ("ncr", world.orphan_ncr.id),
            ("car", world.orphan_car.id),
        ):
            events = _events(db_session, record_type=record_type, record_id=record_id)
            assert events and {event.denial_reason for event in events} == {"unlinkable_record"}

    def test_the_same_customer_without_a_controlling_contract_is_served_in_full(
        self, client: TestClient, db_session: Session, ai_token
    ):
        world = seed_world(db_session, controlled=False)
        _id, token = ai_token
        body = client.get(f"{API}/work-orders/{world.itar_wo.id}", headers=bearer(token))
        assert contains_canary(body.content), "no export-control flag: nothing to withhold"
        assert "export_controlled" not in body.json()


# --------------------------------------------------------------------------- refusals


class TestRefusals:
    def test_egress_off_refuses_every_route_and_ledgers_it(
        self, client: TestClient, db_session: Session, world: World, ai_token, auth_headers: dict, monkeypatch
    ):
        monkeypatch.setattr(llm_client, "_ai_egress_allowed", lambda company_id=None: False)
        token_id, token = ai_token
        for url in (f"{API}/work-orders/", f"{API}/parts/{world.itar_part.id}", f"{API}/search/?q=x"):
            response = client.get(url, headers=bearer(token))
            assert response.status_code == 403, url
            assert response.json()["detail"] == AI_EGRESS_DISABLED_DETAIL
            assert not contains_canary(response.content)
        # A write is refused for egress first, too: gate 1 precedes the read-only gates.
        write = client.post(f"{API}/parts/", json={}, headers=bearer(token))
        assert write.status_code == 403 and write.json()["detail"] == AI_EGRESS_DISABLED_DETAIL

        refused = _events(db_session, action="ai_external_refused")
        assert len(refused) == 4
        assert {event.denial_reason for event in refused} == {"ai_egress_disabled"}
        assert all(event.record_id is None and event.allowed is False for event in refused)
        assert refused[0].extra_data["credential"]["api_token_id"] == token_id
        assert _events(db_session, action="ai_external_redacted") == []

        # A person, and a read-only token that is NOT AI-external, are unaffected.
        assert client.get(f"{API}/work-orders/", headers=auth_headers).status_code == 200

    def test_a_read_only_token_that_is_not_ai_external_ignores_egress(
        self, client: TestClient, admin_headers: dict, viewer: User, world: World, monkeypatch
    ):
        monkeypatch.setattr(llm_client, "_ai_egress_allowed", lambda company_id=None: False)
        _id, token = issue_token(client, admin_headers, viewer, scope="read_only", ai_external=False)
        response = client.get(f"{API}/work-orders/{world.itar_wo.id}", headers=bearer(token))
        assert response.status_code == 200
        assert contains_canary(response.content), "only an AI-external token is redacted"

    def test_a_route_off_the_allowlist_is_403_and_ledgered(
        self, client: TestClient, db_session: Session, world: World, ai_token
    ):
        token_id, token = ai_token
        for url in (f"{API}/search/?q=bracket", f"{API}/shop-floor/dashboard", f"{API}/customers/"):
            response = client.get(url, headers=bearer(token))
            assert response.status_code == 403, url
            assert response.json()["detail"] == AI_EXTERNAL_ROUTE_DENIED_DETAIL
        refused = _events(db_session, action="ai_external_refused", denial_reason="route_not_allowlisted")
        assert len(refused) == 3
        assert refused[0].extra_data["route"] == f"{API}/search/"

    def test_the_downtime_pareto_is_refused_because_it_cannot_be_redacted(
        self, client: TestClient, db_session: Session, world: World, ai_token, auth_headers: dict
    ):
        """Regression (found by the P0 test gate): ``/downtime/summary``'s ``top_reasons`` falls
        back to an event's free-text ``description`` when it has no ``reason_code``, in an object
        that carries no record key -- so a controlled job's downtime text could not be linked and
        went out verbatim. Unlinkable means refuse (the brief), so the route is off the allowlist;
        the per-event downtime reads stay on it and are redacted."""
        world.itar_downtime.reason_code = None
        db_session.commit()
        url = f"{API}/downtime/summary"
        person = client.get(url, headers=auth_headers)
        assert person.status_code == 200
        reasons = [entry["reason"] for entry in person.json()["top_reasons"]]
        assert any(CANARY in reason for reason in reasons), "the case is vacuous: no controlled text in the Pareto"

        _id, token = ai_token
        response = client.get(url, headers=bearer(token))
        assert response.status_code == 403
        assert response.json()["detail"] == AI_EXTERNAL_ROUTE_DENIED_DETAIL
        assert not contains_canary(response.content)
        assert ("GET", "/downtime/summary") not in AI_EXTERNAL_ALLOWED_ROUTES

        # The same event through the per-event read: withheld, not refused.
        listed = client.get(f"{API}/downtime/", headers=bearer(token))
        assert listed.status_code == 200 and not contains_canary(listed.content)
        event = next(row for row in listed.json() if row["id"] == world.itar_downtime.id)
        assert event["export_controlled"] is True and event["description"] == REDACTED_VALUE

    def test_an_ai_external_row_is_read_only_whatever_its_scope_says(
        self, client: TestClient, db_session: Session, admin_headers: dict, viewer: User, world: World
    ):
        """The mint refuses AI-external + full (422), so this row exists only by tampering -- with a
        JWT re-signed to match it. It must not widen anything: writes 403, the allowlist applies,
        and what it reads is redacted."""
        token_id, _token = issue_token(client, admin_headers, viewer)
        row = db_session.get(ApiToken, token_id)
        row.ai_external = True  # scope stays "full"
        db_session.commit()
        forged = create_api_token(
            jti=row.jti, user_id=row.user_id, company_id=row.company_id, scope="full", ai_external=True
        )
        headers = bearer(forged)

        write = client.post(f"{API}/parts/", json={"part_number": "X-1", "name": "x"}, headers=headers)
        assert write.status_code == 403 and write.json()["detail"] == READ_ONLY_ROUTE_WRITES_DETAIL
        off_list = client.get(f"{API}/customers/", headers=headers)
        assert off_list.status_code == 403 and off_list.json()["detail"] == AI_EXTERNAL_ROUTE_DENIED_DETAIL
        read = client.get(f"{API}/work-orders/{world.itar_wo.id}", headers=headers)
        assert read.status_code == 200 and not contains_canary(read.content)
        assert read.json()["export_controlled"] is True

    def test_head_on_an_allowlisted_route_is_never_served(self, client: TestClient, world: World, ai_token):
        """HEAD is not on the allowlist (GET only); FastAPI does not route it to a GET either."""
        _id, token = ai_token
        response = client.head(f"{API}/work-orders/", headers=bearer(token))
        assert response.status_code in (403, 405)

    def test_writes_and_denied_tags_keep_the_read_only_texts(
        self, client: TestClient, db_session: Session, world: World, ai_token
    ):
        _id, token = ai_token
        write = client.put(f"{API}/shop-floor/operations/{world.itar_op.id}/start", headers=bearer(token))
        assert write.status_code == 403 and write.json()["detail"] == READ_ONLY_ROUTE_WRITES_DETAIL
        denied = client.get(f"{API}/audit/", headers=bearer(token))
        assert denied.status_code == 403 and denied.json()["detail"] == READ_ONLY_ROUTE_DENIED_DETAIL
        assert _events(db_session) == [], "gates 2 and 3 are not export-control refusals"

    def test_people_and_full_tokens_are_untouched(
        self, client: TestClient, admin_headers: dict, world: World, test_user: User, auth_headers: dict
    ):
        _id, full = issue_token(client, admin_headers, test_user)
        for headers in (auth_headers, bearer(full)):
            assert client.get(f"{API}/search/?q=x", headers=headers).status_code == 200
            response = client.get(f"{API}/work-orders/{world.itar_wo.id}", headers=headers)
            assert contains_canary(response.content)


# --------------------------------------------------------------------------- reads that write

# Every GET that reconciles on read (``_reconcile_and_commit`` in work_orders.py and
# shop_floor.py). The dashboard is not on the AI-external allowlist, so an AI-external token
# is refused it 403 before the route runs -- which writes nothing either.
RECONCILE_READS: Dict[str, Callable[[WorkOrder, WorkOrderOperation], str]] = {
    "work_order": lambda wo, op: f"{API}/work-orders/{wo.id}",
    "work_order_list": lambda wo, op: f"{API}/work-orders/",
    "shop_floor_operations": lambda wo, op: f"{API}/shop-floor/operations",
    "shop_floor_operation": lambda wo, op: f"{API}/shop-floor/operations/{op.id}",
    "shop_floor_dashboard": lambda wo, op: f"{API}/shop-floor/dashboard",
}
AI_EXTERNAL_RECONCILE_READS = {"work_order", "work_order_list", "shop_floor_operations", "shop_floor_operation"}


def _reconcilable_job(db: Session, world: World, suffix: str) -> Tuple[Part, WorkOrder, WorkOrderOperation]:
    """An IN_PROGRESS work order whose one operation's closed labor already holds the full
    good count: the next reconcile-on-read completes the operation AND the work order, posts
    the finished-goods receipt and emits the completion events."""
    part = Part(
        part_number=f"RECON-PART-{suffix}",
        name="reconciled part",
        part_type="manufactured",
        customer_name=world.clear_customer.name,
        company_id=world.company_id,
    )
    db.add(part)
    db.flush()
    wo = WorkOrder(
        work_order_number=f"WO-RECON-{suffix}",
        part_id=part.id,
        quantity_ordered=5,
        status=WorkOrderStatus.IN_PROGRESS,
        customer_name=world.clear_customer.name,
        company_id=world.company_id,
    )
    db.add(wo)
    db.flush()
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=world.work_center.id,
        sequence=10,
        operation_number="10",
        name="Weld",
        status=OperationStatus.IN_PROGRESS,
        company_id=world.company_id,
    )
    db.add(op)
    db.flush()
    make_closed_time_entry(db, user=world.reporter, op=op, quantity_produced=5)
    return part, wo, op


def _db_state(db: Session) -> Dict[str, Any]:
    """Everything a reconcile-on-read can write, as a comparable value."""
    db.expire_all()
    return {
        "work_orders": sorted(
            (row.id, str(row.status), row.version, float(row.quantity_complete or 0))
            for row in db.query(WorkOrder).all()
        ),
        "operations": sorted(
            (row.id, str(row.status), row.version, float(row.quantity_complete or 0))
            for row in db.query(WorkOrderOperation).all()
        ),
        "audit_rows": db.query(AuditLog).count(),
        "operational_events": db.query(OperationalEvent).count(),
        "inventory_transactions": db.query(InventoryTransaction).count(),
    }


class TestNarrowedTokenReadsWriteNothing:
    """Lead decision 2026-11-15 ("read-only means read-only"), replacing the round-2 pin of the
    old behavior: a read-only or AI-external token's reconcile-on-read GET serves the STORED
    state and writes nothing -- no status flip, no audit row, no OperationalEvent, no inventory
    movement, no outbox enqueue -- while a person's identical GET (and a full token's) still
    reconciles exactly as before. docs/EXPORT_CONTROL_AI_EGRESS.md section 9, MCP.md 4.1 and
    API.md say so."""

    @pytest.mark.parametrize("route", sorted(RECONCILE_READS))
    @pytest.mark.parametrize("ai_external", [True, False], ids=["ai_external", "read_only"])
    def test_a_narrowed_tokens_get_writes_nothing_and_a_persons_reconciles(
        self,
        client: TestClient,
        db_session: Session,
        world: World,
        admin_headers: dict,
        viewer: User,
        ai_external: bool,
        route: str,
        monkeypatch,
    ):
        part, wo, op = _reconcilable_job(db_session, world, "0001")
        db_session.commit()
        _id, token = issue_token(client, admin_headers, viewer, scope="read_only", ai_external=ai_external)
        enqueued: List[int] = []
        monkeypatch.setattr(notification_outbox, "_enqueue_dispatch", enqueued.append)
        url = RECONCILE_READS[route](wo, op)

        before = _db_state(db_session)
        response = client.get(url, headers=bearer(token))
        if ai_external and route not in AI_EXTERNAL_RECONCILE_READS:
            assert response.status_code == 403, response.text[:400]
            assert response.json()["detail"] == AI_EXTERNAL_ROUTE_DENIED_DETAIL
        else:
            assert response.status_code == 200, response.text[:400]
        if route == "work_order":
            assert response.json()["status"] == WorkOrderStatus.IN_PROGRESS.value, "the stored state is served"

        assert _db_state(db_session) == before, "a narrowed token's GET wrote something"
        assert enqueued == [], "nothing reached the notification outbox"
        assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.IN_PROGRESS
        assert db_session.get(WorkOrder, wo.id).status == WorkOrderStatus.IN_PROGRESS

        # The same GET by the same user, signed in as a person, reconciles as it always has.
        person = client.get(url, headers=user_headers(viewer))
        assert person.status_code == 200, person.text[:400]
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.COMPLETE
        assert db_session.get(WorkOrder, wo.id).status == WorkOrderStatus.COMPLETE
        receipts = (
            db_session.query(InventoryTransaction)
            .filter(InventoryTransaction.reference_id == wo.id, InventoryTransaction.part_id == part.id)
            .all()
        )
        assert receipts and {row.created_by for row in receipts} == {viewer.id}
        events = db_session.query(OperationalEvent).filter(OperationalEvent.work_order_id == wo.id).all()
        completions = {event.event_type: event.id for event in events if event.user_id == viewer.id}
        assert {"operation_completed", "work_order_completed"} <= set(completions)
        assert {"operation_completed", "work_order_completed"} <= set(SOURCE_EVENT_TYPE_TO_KEY), "notification sources"
        assert completions["work_order_completed"] in enqueued, "the outbox enqueued its notification at commit"
        assert _db_state(db_session)["audit_rows"] > before["audit_rows"], "the person's reconcile is audited"

    def test_a_full_token_still_reconciles(
        self, client: TestClient, db_session: Session, world: World, admin_headers: dict, test_user: User
    ):
        """Only the NARROWED tokens changed: a full token's GET reconciles, recorded as its user."""
        _part, wo, op = _reconcilable_job(db_session, world, "0002")
        db_session.commit()
        _id, token = issue_token(client, admin_headers, test_user)
        response = client.get(f"{API}/work-orders/{wo.id}", headers=bearer(token))
        assert response.status_code == 200, response.text[:400]
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.COMPLETE
        assert db_session.get(WorkOrder, wo.id).status == WorkOrderStatus.COMPLETE

    def test_the_component_quantity_rederivation_is_skipped_too(
        self, client: TestClient, db_session: Session, world: World, admin_headers: dict, viewer: User, monkeypatch
    ):
        """``GET /work-orders/{id}`` also re-derives component quantities from the BOM and commits
        them; a narrowed token's GET never calls it, a person's does."""
        from app.api.endpoints import work_orders as work_orders_endpoint

        calls: List[int] = []
        real = work_orders_endpoint._reconcile_operation_component_quantities

        def spy(db, work_order, company_id):
            calls.append(work_order.id)
            return real(db, work_order, company_id)

        monkeypatch.setattr(work_orders_endpoint, "_reconcile_operation_component_quantities", spy)
        _part, wo, _op = _reconcilable_job(db_session, world, "0003")
        db_session.commit()
        _id, token = issue_token(client, admin_headers, viewer, scope="read_only", ai_external=False)
        assert client.get(f"{API}/work-orders/{wo.id}", headers=bearer(token)).status_code == 200
        assert calls == []
        assert client.get(f"{API}/work-orders/{wo.id}", headers=user_headers(viewer)).status_code == 200
        assert calls == [wo.id]

    def test_the_by_number_read_writes_nothing_either(
        self, client: TestClient, db_session: Session, world: World, admin_headers: dict, viewer: User, monkeypatch
    ):
        """P1 brought ``GET /work-orders/by-number/{n}`` to parity with the read by id (same body,
        cost and hours figures included). It never reconciles -- for anyone -- so a narrowed
        token's call to it must leave the database exactly as it found it too."""
        _part, wo, _op = _reconcilable_job(db_session, world, "0004")
        db_session.commit()
        enqueued: List[int] = []
        monkeypatch.setattr(notification_outbox, "_enqueue_dispatch", enqueued.append)
        for ai_external in (True, False):
            _id, token = issue_token(client, admin_headers, viewer, scope="read_only", ai_external=ai_external)
            before = _db_state(db_session)
            response = client.get(f"{API}/work-orders/by-number/{wo.work_order_number}", headers=bearer(token))
            assert response.status_code == 200, response.text[:400]
            assert response.json()["status"] == WorkOrderStatus.IN_PROGRESS.value
            assert _db_state(db_session) == before, "a narrowed token's by-number GET wrote something"
        assert enqueued == []


class TestProductionCalendar:
    """The plugin reads the production calendar (P1.2) to bucket its own figures by the shop's
    production day and shift. Both reads are reference data with no customer linkage -- served
    to an AI-external token exactly as to a person, nothing withheld and nothing ledgered -- and
    the two writes stay refused to every read-only token."""

    @staticmethod
    def _save_calendar(client: TestClient, admin_headers: dict, world: World) -> dict:
        document = {
            "expected_version": 0,
            "production_day_start": "05:00",
            "patterns": [
                {
                    "name": "Weld two-shift",
                    "is_default": True,
                    "work_center_ids": [world.work_center.id],
                    "shifts": [
                        {
                            "code": "W1",
                            "name": "Weld 1st",
                            "start": "05:00",
                            "end": "15:30",
                            "days": [0, 1, 2, 3, 4],
                            "planned_minutes": 570,
                        },
                        {
                            "code": "W2",
                            "name": "Weld 2nd",
                            "start": "15:30",
                            "end": "02:00",
                            "days": [0, 1, 2, 3, 4],
                            "planned_minutes": 570,
                        },
                    ],
                }
            ],
        }
        saved = client.put(f"{API}/production-calendar", json=document, headers=admin_headers)
        assert saved.status_code == 200, saved.text[:400]
        return saved.json()

    def test_both_reads_are_served_in_full_and_ledger_nothing(
        self, client: TestClient, db_session: Session, world: World, ai_token, admin_headers: dict
    ):
        saved = self._save_calendar(client, admin_headers, world)
        assert saved["configured"] is True and saved["patterns"][0]["shifts"], "vacuous: nothing configured"
        _id, token = ai_token
        before = len(_events(db_session))
        for url in (
            f"{API}/production-calendar",
            f"{API}/production-calendar/resolve?at=2026-11-11T07:30:00Z&work_center_id={world.work_center.id}",
        ):
            redacted = client.get(url, headers=bearer(token))
            person = client.get(url, headers=admin_headers)
            assert redacted.status_code == person.status_code == 200, (url, redacted.text[:400])
            body = redacted.json()
            assert REDACTED_VALUE not in redacted.text and "export_controlled" not in redacted.text, url
            if url.endswith("/production-calendar"):
                # ``today`` is computed from the wall clock; everything else is the stored document.
                body.pop("today")
                expected = person.json()
                expected.pop("today")
                assert body == expected
                assert [s["code"] for s in body["patterns"][0]["shifts"]] == ["W1", "W2"]
            else:
                assert body == person.json()
                assert body["shift"]["code"] == "W2", "01:30 Wednesday Central is Tuesday's 2nd shift"
        assert len(_events(db_session)) == before, "reference data is never ledgered as withheld"

    @pytest.mark.parametrize("ai_external", [True, False], ids=["ai_external", "read_only"])
    def test_the_writes_stay_refused_to_every_read_only_token(
        self, client: TestClient, world: World, admin_headers: dict, viewer: User, ai_external: bool
    ):
        saved = self._save_calendar(client, admin_headers, world)
        _id, token = issue_token(client, admin_headers, viewer, scope="read_only", ai_external=ai_external)
        document = {"expected_version": saved["version"], "production_day_start": "06:00", "patterns": []}
        refusals = [
            client.put(f"{API}/production-calendar", json=document, headers=bearer(token)),
            # The sync defaults to a dry run, and is still a POST: a read-only token never calls it.
            client.post(f"{API}/production-calendar/working-calendars/sync", headers=bearer(token)),
            client.post(
                f"{API}/production-calendar/working-calendars/sync?dry_run=false&expected_version={saved['version']}",
                headers=bearer(token),
            ),
        ]
        for refused in refusals:
            assert refused.status_code == 403, refused.text[:400]
            assert refused.json() == {"detail": READ_ONLY_ROUTE_WRITES_DETAIL}
        after = client.get(f"{API}/production-calendar", headers=admin_headers).json()
        assert (after["version"], after["production_day_start"]) == (saved["version"], "05:00"), "nothing changed"


class TestSupplierApprovalAndEquipmentAssets:
    """The Approved Supplier List and equipment-asset fields (P2.2 / P2.3) on routes the plugin
    reads. A purchase order's ASL warnings name a part, so inside a WITHHELD order they are
    withheld with it; the vendor's ASL record and the maintenance asset fields are reference
    data with no customer linkage and pass in full; the supplier-approval check is a POST, so no
    read-only token reaches it."""

    @staticmethod
    def _scope_mismatch(db: Session, w: World) -> None:
        """The vendor is Approved for machining only; both PO parts are bought for plating, so
        each PO carries a ``scope_not_approved`` warning naming its part."""
        w.vendor.approval_status = "approved"
        w.vendor.approved_scopes = ["machining"]
        w.itar_part.supply_scope = "special_process_plating_coating"
        w.clear_part.supply_scope = "special_process_plating_coating"
        db.commit()

    def test_a_withheld_purchase_order_withholds_its_supplier_approval_warnings(
        self, client: TestClient, db_session: Session, world: World, ai_token, auth_headers: dict
    ):
        self._scope_mismatch(db_session, world)
        part_number = world.itar_part.part_number
        url = f"{API}/purchasing/purchase-orders/{world.itar_po.id}"

        person = client.get(url, headers=auth_headers)
        assert person.status_code == 200, person.text[:400]
        (person_warning,) = [
            w for w in person.json()["supplier_approval_warnings"] if w["code"] == "scope_not_approved"
        ]
        assert person_warning["part_number"] == part_number and part_number in person_warning["message"], "vacuous"

        _id, token = ai_token
        redacted = client.get(url, headers=bearer(token))
        assert redacted.status_code == 200, redacted.text[:400]
        assert part_number.encode() not in redacted.content and part_number.lower().encode() not in redacted.content
        assert not contains_canary(redacted.content), redacted.text[:800]
        body = redacted.json()
        assert body["export_controlled"] is True
        (warning,) = [w for w in body["supplier_approval_warnings"] if w.get("part_id") == world.itar_part.id]
        assert warning["export_controlled"] is True
        for key in ("code", "message", "part_number"):
            assert warning[key] == REDACTED_VALUE, key
        # The person still gets everything.
        assert contains_canary(client.get(url, headers=auth_headers).content)

    def test_a_cleared_purchase_orders_warnings_pass_in_full(
        self, client: TestClient, db_session: Session, world: World, ai_token
    ):
        self._scope_mismatch(db_session, world)
        _id, token = ai_token
        response = client.get(f"{API}/purchasing/purchase-orders/{world.clear_po.id}", headers=bearer(token))
        assert response.status_code == 200, response.text[:400]
        (warning,) = [w for w in response.json()["supplier_approval_warnings"] if w["code"] == "scope_not_approved"]
        assert warning["part_number"] == world.clear_part.part_number
        assert world.clear_part.part_number in warning["message"] and "export_controlled" not in warning

    def test_the_vendors_asl_record_is_reference_data_served_in_full(
        self, client: TestClient, db_session: Session, world: World, ai_token, auth_headers: dict
    ):
        _vendor_reevaluation_overdue(db_session, world)
        _id, token = ai_token
        for url in (
            f"{API}/purchasing/vendors/{world.vendor.id}",
            f"{API}/purchasing/vendors/reevaluation-due",
        ):
            response = client.get(url, headers=bearer(token))
            assert response.status_code == 200, (url, response.text[:400])
            body = response.json()
            row = body if isinstance(body, dict) else next(v for v in body if v["id"] == world.vendor.id)
            assert row["approval_status"] == "approved"
            assert row["approved_scopes"] == ["machining"] and row["approval_bases"] == ["as9100"]
            assert row["reevaluation_due"] == world.vendor.reevaluation_due.isoformat()
            assert "export_controlled" not in row
            # The ASL notes stay -- only the controlled part number they name is scrubbed.
            assert row["approval_notes"] == f"Approved for {REDACTED_VALUE} machining"
            if "days_overdue" in row:
                assert row["days_overdue"] == 30
        assert _events(db_session, action="ai_external_redacted") == [], "a vendor is never a withheld record"

    def test_an_asset_targeted_maintenance_work_order_is_served_in_full(
        self, client: TestClient, db_session: Session, world: World, ai_token
    ):
        asset = MaintenanceAsset(
            asset_tag="FORK-02", name="Yard forklift", asset_type="forklift", company_id=world.company_id
        )
        db_session.add(asset)
        db_session.flush()
        mwo = MaintenanceWorkOrder(
            asset_id=asset.id,
            wo_number="MWO-ASSET-1",
            title="Mast chain lube",
            scheduled_date=datetime.utcnow().date(),
            company_id=world.company_id,
        )
        db_session.add(mwo)
        db_session.commit()

        _id, token = ai_token
        listed = client.get(f"{API}/maintenance/work-orders?asset_id={asset.id}", headers=bearer(token))
        assert listed.status_code == 200, listed.text[:400]
        (row,) = listed.json()
        assert (row["id"], row["asset_id"], row["asset_tag"], row["asset_name"]) == (
            mwo.id,
            asset.id,
            "FORK-02",
            "Yard forklift",
        )
        assert row["target_type"] == "asset" and row["work_center_id"] is None and "export_controlled" not in row
        single = client.get(f"{API}/maintenance/work-orders/{mwo.id}", headers=bearer(token))
        assert single.status_code == 200 and single.json()["asset_tag"] == "FORK-02"
        assert _events(db_session, action="ai_external_redacted") == []

    @pytest.mark.parametrize("ai_external", [True, False], ids=["ai-external", "read-only"])
    def test_the_supplier_approval_check_is_a_post_no_read_only_token_reaches(
        self, client: TestClient, world: World, admin_headers: dict, viewer: User, ai_external: bool
    ):
        _id, token = issue_token(client, admin_headers, viewer, scope="read_only", ai_external=ai_external)
        payload = {"vendor_id": world.vendor.id, "part_ids": [world.itar_part.id]}
        refused = client.post(f"{API}/purchasing/supplier-approval-check", json=payload, headers=bearer(token))
        assert refused.status_code == 403, refused.text[:400]
        assert refused.json() == {"detail": READ_ONLY_ROUTE_WRITES_DETAIL}
        assert not contains_canary(refused.content)
        # A person's session still runs it.
        assert (
            client.post(f"{API}/purchasing/supplier-approval-check", json=payload, headers=admin_headers).status_code
            == 200
        )


# --------------------------------------------------------------------------- file bytes


@pytest.fixture
def file_routes() -> Iterator[Dict[str, str]]:
    """Two throwaway GET routes under an ordinary tag: a text file and a redirect to storage."""

    def text_file(current_user: User = Depends(deps.get_current_user)):
        return PlainTextResponse(f"{CANARY} drawing bytes")

    def redirect(current_user: User = Depends(deps.get_current_user)):
        return RedirectResponse("https://storage.example.invalid/drawing.pdf")

    paths = {"file": f"{API}/__export_control_probe/file", "redirect": f"{API}/__export_control_probe/redirect"}
    before = list(app.router.routes)
    schema = app.openapi_schema  # never let the probes leak into a cached OpenAPI document
    app.add_api_route(paths["file"], text_file, methods=["GET"], tags=["Parts"], include_in_schema=False)
    app.add_api_route(paths["redirect"], redirect, methods=["GET"], tags=["Parts"], include_in_schema=False)
    try:
        yield paths
    finally:
        app.router.routes[:] = before
        app.openapi_schema = schema


class TestFileBytes:
    def test_a_read_only_token_never_receives_file_bytes(
        self, client: TestClient, db_session: Session, admin_headers: dict, viewer: User, file_routes
    ):
        _id, token = issue_token(client, admin_headers, viewer, scope="read_only", ai_external=False)
        for path in file_routes.values():
            response = client.get(path, headers=bearer(token), follow_redirects=False)
            assert response.status_code == 403, path
            assert response.json()["detail"] == READ_ONLY_FILE_REFUSED_DETAIL
            assert not contains_canary(response.content)
        assert _events(db_session) == [], "not AI-external: nothing to ledger"

    def test_an_ai_external_token_is_refused_and_ledgered(
        self, client: TestClient, db_session: Session, ai_token, file_routes, monkeypatch
    ):
        allowed = deps.AI_EXTERNAL_ALLOWED_OPERATIONS | {("GET", file_routes["file"])}
        monkeypatch.setattr(deps, "AI_EXTERNAL_ALLOWED_OPERATIONS", allowed)
        token_id, token = ai_token
        response = client.get(file_routes["file"], headers=bearer(token))
        assert response.status_code == 403
        assert response.json()["detail"] == READ_ONLY_FILE_REFUSED_DETAIL
        assert not contains_canary(response.content)
        refused = _events(db_session, action="ai_external_refused")
        assert [event.denial_reason for event in refused] == ["non_json_response"]
        assert refused[0].extra_data["credential"]["api_token_id"] == token_id

    def test_people_and_full_tokens_still_download(
        self, client: TestClient, admin_headers: dict, test_user: User, auth_headers: dict, file_routes
    ):
        _id, full = issue_token(client, admin_headers, test_user)
        for headers in (auth_headers, bearer(full)):
            response = client.get(file_routes["file"], headers=headers)
            assert response.status_code == 200 and contains_canary(response.content)


# --------------------------------------------------------------------------- the ledger


class TestLedger:
    def test_one_row_per_withheld_record_naming_the_token(
        self, client: TestClient, db_session: Session, world: World, ai_token, viewer: User
    ):
        token_id, token = ai_token
        response = client.get(f"{API}/work-orders/{world.itar_wo.id}", headers=bearer(token))
        assert response.status_code == 200
        rows = _events(db_session, action="ai_external_redacted")
        assert rows, "the redaction was ledgered"
        keys = [(row.record_type, row.record_id) for row in rows]
        assert len(keys) == len(set(keys)), "deduplicated per request"
        assert ("work_order", world.itar_wo.id) in keys
        assert ("part", world.itar_part.id) in keys

        record = db_session.get(ApiToken, token_id)
        wo_row = next(row for row in rows if row.record_type == "work_order")
        assert wo_row.allowed is False
        assert wo_row.denial_reason == "export_controlled_customer"
        assert wo_row.data_classification == "itar_export_controlled"
        assert wo_row.company_id == world.company_id
        assert wo_row.user_id == viewer.id
        assert wo_row.request_id == response.headers["X-Correlation-ID"]
        assert wo_row.source_ip
        assert wo_row.extra_data == {
            "credential": {
                "kind": "api_token",
                "api_token_id": token_id,
                "jti_prefix": record.jti_prefix,
                "label": record.label,
            },
            "route": f"{API}/work-orders/{{work_order_id}}",
            "customer_id": world.itar_customer.id,
            "contract_id": world.contract.id,
        }

    def test_a_response_with_nothing_withheld_writes_nothing(
        self, client: TestClient, db_session: Session, world: World, ai_token
    ):
        _id, token = ai_token
        assert client.get(f"{API}/work-centers/", headers=bearer(token)).status_code == 200
        assert _events(db_session) == []

    def test_a_failed_ledger_write_is_503_with_nothing_sent(
        self, client: TestClient, db_session: Session, world: World, ai_token, monkeypatch
    ):
        def broken(db, rows):
            raise RuntimeError("ledger down")

        monkeypatch.setattr(export_control_service, "insert_controlled_access_events", broken)
        _id, token = ai_token
        response = client.get(f"{API}/work-orders/{world.itar_wo.id}", headers=bearer(token))
        assert response.status_code == 503
        assert response.json() == {"detail": EXPORT_CONTROL_FAILED_DETAIL}
        assert not contains_canary(response.content)
        assert world.itar_wo.work_order_number.encode() not in response.content
        assert "etag" not in response.headers

    def test_a_failed_index_build_is_503_too(self, client: TestClient, world: World, ai_token, monkeypatch):
        def broken(db, company_id):
            raise RuntimeError("database gone")

        monkeypatch.setattr(export_control_service, "build_controlled_index", broken)
        _id, token = ai_token
        # Even a route that would withhold nothing: the check could not be made.
        response = client.get(f"{API}/work-centers/", headers=bearer(token))
        assert response.status_code == 503
        assert response.json() == {"detail": EXPORT_CONTROL_FAILED_DETAIL}

    def test_a_failed_refusal_ledger_write_is_503(self, client: TestClient, world: World, ai_token, monkeypatch):
        def broken(db, rows):
            raise RuntimeError("ledger down")

        monkeypatch.setattr(export_control_service, "insert_controlled_access_events", broken)
        _id, token = ai_token
        response = client.get(f"{API}/search/?q=x", headers=bearer(token))
        assert response.status_code == 503
        assert response.json() == {"detail": EXPORT_CONTROL_FAILED_DETAIL}

    def test_an_error_body_is_text_scrubbed(self, client: TestClient, db_session: Session, world: World, ai_token):
        _id, token = ai_token
        response = client.get(f"{API}/parts/by-number/{world.itar_part.part_number}x", headers=bearer(token))
        assert response.status_code == 404
        assert not contains_canary(response.content)


# --------------------------------------------------------------------------- conditional requests


class TestConditionalRequests:
    def test_the_bearer_sniff_reads_only_the_type_claim(
        self, client: TestClient, ai_token, auth_headers: dict, admin_headers: dict, test_user: User
    ):
        _id, token = ai_token
        assert bearer_is_api_token([(b"authorization", f"Bearer {token}".encode())]) is True
        person = auth_headers["Authorization"].encode()
        assert bearer_is_api_token([(b"authorization", person)]) is False
        assert bearer_is_api_token([(b"authorization", b"Bearer not.a.jwt")]) is False
        assert bearer_is_api_token([]) is False
        assert b"if-none-match" in CONDITIONAL_REQUEST_HEADERS and b"if-match" in CONDITIONAL_REQUEST_HEADERS
        # Only a NARROWED token loses its conditional headers (code review P0, finding 7).
        assert bearer_is_narrowed_api_token([(b"authorization", f"Bearer {token}".encode())]) is True
        _full_id, full = issue_token(client, admin_headers, test_user)
        assert bearer_is_narrowed_api_token([(b"authorization", f"Bearer {full}".encode())]) is False
        assert bearer_is_narrowed_api_token([(b"authorization", person)]) is False

    def test_a_full_token_keeps_its_304(self, client: TestClient, admin_headers: dict, test_user: User):
        """Code review P0, finding 7: full tokens are byte-identical to before -- the dashboard's
        ETag still earns them a 304. Only read-only / AI-external tokens are stripped."""
        _id, full = issue_token(client, admin_headers, test_user)
        url = f"{API}/shop-floor/dashboard"
        etag = client.get(url, headers=bearer(full)).headers.get("etag")
        assert etag
        assert client.get(url, headers={**bearer(full), "If-None-Match": etag}).status_code == 304

    def test_if_none_match_never_earns_an_api_token_a_304(
        self, client: TestClient, admin_headers: dict, viewer: User, world: World, auth_headers: dict
    ):
        url = f"{API}/shop-floor/dashboard"
        first = client.get(url, headers=auth_headers)
        etag = first.headers.get("etag")
        assert etag, "the dashboard is the one route with an ETag"
        assert client.get(url, headers={**auth_headers, "If-None-Match": etag}).status_code == 304, "people keep it"

        _id, token = issue_token(client, admin_headers, viewer, scope="read_only", ai_external=False)
        etag = client.get(url, headers=bearer(token)).headers.get("etag")
        again = client.get(url, headers={**bearer(token), "If-None-Match": etag or "*"})
        assert again.status_code == 200, "the conditional header was stripped before routing"

    def test_an_ai_external_token_gets_no_etag_and_no_304(self, client: TestClient, world: World, ai_token):
        _id, token = ai_token
        response = client.get(f"{API}/work-orders/", headers={**bearer(token), "If-None-Match": "*"})
        assert response.status_code == 200
        assert "etag" not in response.headers


# --------------------------------------------------------------------------- filter oracles

# Query parameters an allowlisted route DECLARES that an AI-external token is refused: each
# filters (free text, or an exact customer name) over a field the redaction hides. Every
# declared parameter must be here or in AI_EXTERNAL_QUERY_PARAMS -- the guard below.
REFUSED_QUERY_PARAMS: Dict[str, frozenset] = {
    "/parts/": frozenset({"search"}),
    "/work-orders/": frozenset({"search"}),
    "/work-orders/browse": frozenset({"search", "customer"}),
    "/shop-floor/operations": frozenset({"search"}),
    "/quotes/": frozenset({"customer", "search"}),
    # The FAI log (P2.5): a customer-name substring and a search over FAI #, part number /
    # description, job #, customer PO and serial both match withheld text.
    "/quality/fai/log": frozenset({"customer", "search"}),
}


def _declared_query_params(route: APIRoute) -> set:
    names, stack = set(), [route.dependant]
    while stack:
        current = stack.pop()
        names.update(param.alias or param.name for param in current.query_params)
        stack.extend(current.dependencies)
    return names


class TestFilterOracles:
    """Code review P0, finding 1: a filter is an oracle on the field it filters."""

    def test_every_declared_query_parameter_is_classified(self):
        relative = {path for _method, path in AI_EXTERNAL_ALLOWED_ROUTES}
        assert set(AI_EXTERNAL_QUERY_PARAMS) == relative, "one entry per allowlisted route, no stale ones"
        routes = {
            route.path_format[len(API) :]: route
            for route in app.routes
            if isinstance(route, APIRoute) and "GET" in route.methods and route.path_format.startswith(API)
        }
        for path in sorted(relative):
            declared = _declared_query_params(routes[path])
            allowed = set(AI_EXTERNAL_QUERY_PARAMS[path])
            refused = set(REFUSED_QUERY_PARAMS.get(path, frozenset()))
            assert allowed <= declared, f"{path}: allows parameters the route does not take: {allowed - declared}"
            assert not allowed & refused, path
            unclassified = declared - allowed - refused
            assert not unclassified, (
                f"{path} takes {sorted(unclassified)}: classify each in AI_EXTERNAL_QUERY_PARAMS "
                "(a filter over a KEPT field) or REFUSED_QUERY_PARAMS (one over withheld text)"
            )

    @pytest.mark.parametrize(
        "query",
        [
            "/parts/?search=description of the controlled",
            "/parts/?search=zzz-no-such-text",
            "/work-orders/?search=ITARCANARY-CP",
            "/work-orders/browse?search=ITAR",
            "/work-orders/browse?customer=ITARCANARY Defense Systems",
            "/work-orders/browse?sort=part",
            "/work-orders/browse?sort=customer",
            "/work-orders/browse?group=customer",
            "/work-orders/browse?group=part",
            "/shop-floor/operations?search=brake",
            "/quotes/?customer=ITARCANARY Defense Systems",
            "/quotes/?search=ITAR",
            "/parts/?not_a_parameter=1",
            "/work-orders/?status=released&search=x",
            "/quality/fai/log?customer=ITARCANARY Defense Systems",
            "/quality/fai/log?search=ITARCANARY-PN",
            "/quality/fai/log?status=pending&search=x",
        ],
    )
    def test_text_filters_are_refused_and_ledgered(
        self, client: TestClient, db_session: Session, world: World, ai_token, query: str
    ):
        token_id, token = ai_token
        response = client.get(API + query, headers=bearer(token))
        assert response.status_code == 403, response.text[:300]
        assert response.json() == {"detail": AI_EXTERNAL_QUERY_DENIED_DETAIL}
        refused = _events(db_session, action="ai_external_refused")
        assert [event.denial_reason for event in refused] == ["query_parameter_not_allowlisted"]
        assert refused[0].extra_data["credential"]["api_token_id"] == token_id

    def test_the_substring_oracle_is_closed(self, client: TestClient, world: World, ai_token, auth_headers: dict):
        """Before: a matching substring returned 1 redacted row and a non-matching one 0 -- so
        repeating the query recovered the withheld description. Now both answers are the same."""
        _id, token = ai_token
        hit = client.get(f"{API}/parts/?search=description of the controlled", headers=bearer(token))
        miss = client.get(f"{API}/parts/?search=zzz-no-such-text", headers=bearer(token))
        assert (hit.status_code, hit.content) == (miss.status_code, miss.content)
        person = client.get(f"{API}/parts/?search=description of the controlled", headers=auth_headers)
        assert person.status_code == 200 and len(person.json()) == 1, "a person still searches"

    @pytest.mark.parametrize(
        "query",
        [
            "/work-orders/?status=released&limit=5&skip=0",
            "/work-orders/browse?sort=due_date&direction=desc&group=status",
            "/work-orders/browse?sort=work_order_number&group=none&hide_cots=true",
            "/parts/?part_type=manufactured&limit=10&active_only=true",
            "/quotes/?status=draft&limit=5",
            "/shop-floor/operations?status=ready&page=1&page_size=10",
            "/oee/records?shift_id=1&limit=5",
            "/production-calendar/resolve?at=2026-11-10T12:00:00Z",
            "/quality/fai/log?status=pending&limit=5&offset=0&start_date=2026-01-01&end_date=2026-12-31",
            "/maintenance/work-orders?asset_id=1&status=scheduled",
            "/purchasing/vendors/reevaluation-due?within_days=30&include_inactive=true",
        ],
    )
    def test_filters_over_kept_fields_still_work(self, client: TestClient, world: World, ai_token, query: str):
        _id, token = ai_token
        response = client.get(API + query, headers=bearer(token))
        assert response.status_code == 200, response.text[:300]
        assert not contains_canary(response.content)


# --------------------------------------------------------------------------- probes that echo


class TestProbeEchoes:
    """Code review P0, finding 1: an answer that changes with whether a probe matched withheld text."""

    def test_a_by_number_lookup_of_a_withheld_part_is_the_not_found_404(
        self, client: TestClient, db_session: Session, world: World, ai_token, auth_headers: dict
    ):
        _id, token = ai_token
        nobody = client.get(f"{API}/parts/by-number/NO-SUCH-PART-123", headers=bearer(token))
        assert nobody.status_code == 404
        for number in (world.itar_part.part_number, world.itar_part.part_number.lower(), f"{CANARY}-ALIAS-1"):
            withheld = client.get(f"{API}/parts/by-number/{number}", headers=bearer(token))
            assert withheld.status_code == 404, number
            assert withheld.content == nobody.content, "indistinguishable from a number nobody carries"
            assert "x-resolved-from-alias" not in withheld.headers
            assert not contains_canary(withheld.content)
        assert nobody.json() == {"detail": ai_external_error_detail(404)}
        # The withheld lookup is still ledgered; a cleared part is still found, in full.
        assert ("part", world.itar_part.id) in {
            (event.record_type, event.record_id) for event in _events(db_session, action="ai_external_redacted")
        }
        cleared = client.get(f"{API}/parts/by-number/{world.clear_part.part_number}", headers=bearer(token))
        assert cleared.status_code == 200 and cleared.json()["name"].startswith(CLEAR_TEXT)
        assert (
            client.get(f"{API}/parts/by-number/{world.itar_part.part_number}", headers=auth_headers).status_code == 200
        )

    def test_a_masked_miss_does_the_work_a_withheld_hit_does(
        self, client: TestClient, db_session: Session, world: World, ai_token, monkeypatch
    ):
        """Code review P0 round 2, finding 1: the two 404s were byte-identical but not equally
        expensive. A withheld hit built the company's export-control index (about fifteen
        company-wide queries) and committed a ledger row before answering; a number nobody
        carries answered from the route's own 404 with no database work at all, several times
        faster -- so latency confirmed that a guessed ITAR part number exists. Both now build the
        index once and commit one ledger INSERT."""
        builds: List[int] = []
        real_build = export_control_service.build_controlled_index

        def counting_build(db, company_id):
            builds.append(company_id)
            return real_build(db, company_id)

        monkeypatch.setattr(export_control_service, "build_controlled_index", counting_build)
        statements: List[str] = []

        def record(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        _id, token = ai_token
        # Warm-up: the token's last_used_at touch (at most once per 5 minutes) lands here, not
        # in one of the two measured requests.
        assert client.get(f"{API}/work-centers/", headers=bearer(token)).status_code == 200

        def cost(number: str):
            builds.clear()
            statements.clear()
            event.listen(Engine, "before_cursor_execute", record)
            try:
                response = client.get(f"{API}/parts/by-number/{number}", headers=bearer(token))
            finally:
                event.remove(Engine, "before_cursor_execute", record)
            return response, len(builds), list(statements)

        hit, hit_builds, hit_sql = cost(world.itar_part.part_number)
        miss, miss_builds, miss_sql = cost("NO-SUCH-PART-ZZZ")

        assert hit.status_code == miss.status_code == 404
        assert hit.content == miss.content
        assert hit_builds == miss_builds == 1, (hit_builds, miss_builds)

        def ledger_inserts(sql: List[str]) -> int:
            return sum(1 for s in sql if s.lstrip().upper().startswith("INSERT INTO CONTROLLED_ACCESS_EVENTS"))

        assert ledger_inserts(hit_sql) == ledger_inserts(miss_sql) == 1
        # What is left is the route's own work: a hit serializes the part, a miss also tries the
        # alias tier. A couple of statements, against the ~twenty the index and ledger cost.
        assert abs(len(hit_sql) - len(miss_sql)) <= 2, (len(hit_sql), len(miss_sql))

        (miss_row,) = _events(db_session, action="ai_external_lookup_miss")
        assert miss_row.record_type == "part" and miss_row.record_id is None
        assert miss_row.allowed is False and miss_row.denial_reason == "not_found"
        assert miss_row.data_classification == "unknown"
        assert ("part", world.itar_part.id) in {
            (row.record_type, row.record_id) for row in _events(db_session, action="ai_external_redacted")
        }

    def test_a_masked_miss_fails_closed_like_a_hit(self, client: TestClient, world: World, ai_token, monkeypatch):
        def broken(db, rows):
            raise RuntimeError("ledger down")

        monkeypatch.setattr(export_control_service, "insert_controlled_access_events", broken)
        _id, token = ai_token
        hit = client.get(f"{API}/parts/by-number/{world.itar_part.part_number}", headers=bearer(token))
        miss = client.get(f"{API}/parts/by-number/NO-SUCH-PART-ZZZ", headers=bearer(token))
        assert hit.status_code == miss.status_code == 503
        assert hit.content == miss.content == json.dumps({"detail": EXPORT_CONTROL_FAILED_DETAIL}).encode()

    @pytest.mark.parametrize(
        "probe_template,control_template",
        [
            ("/parts/{probe}", "/parts/{control}"),  # a path id that does not parse: 422 echoes ``input``
            ("/parts/?part_type={probe}", "/parts/?part_type={control}"),  # an enum that does not parse
            ("/work-orders/by-number/{probe}", "/work-orders/by-number/{control}"),  # a 404
        ],
    )
    def test_route_errors_never_echo_the_request(
        self, client: TestClient, world: World, ai_token, probe_template: str, control_template: str
    ):
        """The residual text pass over an error that echoed the request rewrote a probed
        controlled part number to ``[export-controlled]`` -- confirming the probe. A route's own
        error is now one fixed text per status code, so a probe and a control answer identically."""
        _id, token = ai_token
        probe = client.get(API + probe_template.format(probe=world.itar_part.part_number), headers=bearer(token))
        control = client.get(API + control_template.format(control="ZZZZ-NOT-A-PART"), headers=bearer(token))
        assert probe.status_code == control.status_code and probe.status_code in (404, 422)
        assert probe.content == control.content, (probe.text, control.text)
        assert REDACTED_VALUE.encode() not in probe.content and not contains_canary(probe.content)
        assert probe.json()["detail"] == ai_external_error_detail(probe.status_code)

    def test_a_422_names_where_and_what_but_never_the_value(self, client: TestClient, world: World, ai_token):
        _id, token = ai_token
        response = client.get(f"{API}/parts/?limit=not-a-number", headers=bearer(token))
        assert response.status_code == 422
        assert response.json() == {
            "detail": ai_external_error_detail(422),
            "errors": [{"type": "int_parsing", "loc": ["query", "limit"], "msg": "Invalid value."}],
        }


# --------------------------------------------------------------------------- short customer codes


class TestShortCustomerCodes:
    def test_a_three_letter_controlled_code_is_withheld_as_a_whole_value(
        self, client: TestClient, db_session: Session, world: World, ai_token
    ):
        """Code review P0, finding 2: the substring pass needs 4+ characters (so a short token
        cannot rewrite ordinary words), and a controlled customer coded "BAE" went out whole in
        the browse page's customer facet. A whole value EQUAL to a sensitive token is replaced
        whatever its length."""
        short = Customer(name="Bravo Aero Electronics", code="BAE", company_id=world.company_id)
        # Its own part: a part a controlled job builds is controlled with it (and its other jobs too).
        bae_part = Part(
            part_number="BAE-BRKT-1", name="Bravo bracket", part_type="manufactured", company_id=world.company_id
        )
        db_session.add_all([short, bae_part])
        db_session.flush()
        db_session.add(
            CustomerContract(
                company_id=world.company_id, customer_id=short.id, contract_name="ITAR", itar_controlled=True
            )
        )
        db_session.add(
            WorkOrder(
                work_order_number="WO-BAE-0001",
                part_id=bae_part.id,
                quantity_ordered=1,
                status=WorkOrderStatus.RELEASED,
                customer_name="BAE",
                company_id=world.company_id,
            )
        )
        world.clear_wo.customer_po = "bae"  # a cleared record carrying the code as a whole value
        db_session.commit()
        _id, token = ai_token
        browse = client.get(f"{API}/work-orders/browse", headers=bearer(token))
        assert browse.status_code == 200
        assert "BAE" not in browse.json()["customers"] and "bae" not in browse.json()["customers"]
        assert world.clear_customer.name in browse.json()["customers"], "a cleared name passes in full"
        listed = client.get(f"{API}/work-orders/", headers=bearer(token)).json()
        bae = find_record(listed, key="work_order_number", value="WO-BAE-0001")
        assert bae["export_controlled"] is True and bae["customer_name"] == REDACTED_VALUE
        clear = find_record(listed, key="id", value=world.clear_wo.id)
        assert "export_controlled" not in clear and clear["customer_po"] == REDACTED_VALUE
        assert clear["customer_name"] == world.clear_customer.name


# --------------------------------------------------------------------------- nested records


def _sibling_with_unlinkable_component(db: Session, w: World) -> WorkOrderOperation:
    """A second operation on the CLEARED work order whose component part has no customer (an
    engineering part): the operation is withheld as unlinkable while the work order is not."""
    engineering = Part(
        part_number="ENG-ONLY-0001",
        name="SIBLINGCANARY engineering part",
        part_type="manufactured",
        company_id=w.company_id,
    )
    db.add(engineering)
    db.flush()
    sibling = WorkOrderOperation(
        work_order_id=w.clear_wo.id,
        work_center_id=w.work_center.id,
        sequence=20,
        operation_number="20",
        name="SIBLINGCANARY sibling op",
        component_part_id=engineering.id,
        component_quantity=1,
        status=OperationStatus.PENDING,
        actual_start=datetime.utcnow() - timedelta(hours=1),
        company_id=w.company_id,
    )
    db.add(sibling)
    db.commit()
    db.refresh(sibling)
    return sibling


class TestNestedRecords:
    """Compliance review P0, finding 3: nested record lists that carry only ``id`` for themselves."""

    @pytest.mark.parametrize("template", ["/shop-floor/operations/{op}", "/work-orders/{wo}/timeline"])
    def test_a_withheld_sibling_operation_is_withheld_inside_a_cleared_response(
        self, client: TestClient, db_session: Session, world: World, ai_token, auth_headers: dict, template: str
    ):
        sibling = _sibling_with_unlinkable_component(db_session, world)
        url = API + template.format(op=world.clear_op.id, wo=world.clear_wo.id)
        person = client.get(url, headers=auth_headers)
        assert person.status_code == 200 and b"SIBLINGCANARY" in person.content, "vacuous: the sibling is not there"
        _id, token = ai_token
        response = client.get(url, headers=bearer(token))
        assert response.status_code == 200
        assert b"SIBLINGCANARY" not in response.content, response.text[:800]
        body = response.json()
        if "all_operations" in body:
            entry = find_record(body["all_operations"], key="id", value=sibling.id)
            assert entry["export_controlled"] is True and entry["name"] == REDACTED_VALUE
            assert entry["sequence"] == 20 and entry["operation_number"] == "20"
            own = find_record(body["all_operations"], key="id", value=world.clear_op.id)
            assert "export_controlled" not in own and own["name"] == "Brake"

    def test_a_blocker_on_a_withheld_operation_is_withheld_in_a_cleared_timeline(
        self, client: TestClient, db_session: Session, world: World, ai_token, auth_headers: dict
    ):
        """Code review P0 round 2, finding 3: a blocker filed against an operation links to
        ``#job-blockers``, not to the operation, so the timeline's link-based rule could not tie
        it to its (withheld) operation and the blocker's free-text title went out in full. Every
        operation-scoped timeline item now carries ``operation_id``."""
        sibling = _sibling_with_unlinkable_component(db_session, world)
        job_blocker = WorkOrderBlocker(
            work_order_id=world.clear_wo.id,
            title=f"{CLEAR_TEXT} job-level blocker",
            status="open",
            reported_at=datetime.utcnow() - timedelta(minutes=50),
            company_id=world.company_id,
        )
        op_blocker = WorkOrderBlocker(
            work_order_id=world.clear_wo.id,
            operation_id=sibling.id,
            title="SIBLINGCANARY weldment fixture missing",
            status="resolved",
            reported_at=datetime.utcnow() - timedelta(minutes=40),
            resolved_at=datetime.utcnow() - timedelta(minutes=30),
            company_id=world.company_id,
        )
        db_session.add_all([job_blocker, op_blocker])
        db_session.commit()
        url = f"{API}/work-orders/{world.clear_wo.id}/timeline?category=blocker"
        person = client.get(url, headers=auth_headers)
        assert person.status_code == 200
        person_items = person.json()["items"]
        assert sum(b"SIBLINGCANARY" in json.dumps(item).encode() for item in person_items) == 2, "vacuous"
        assert {item["operation_id"] for item in person_items} == {sibling.id, None}

        _id, token = ai_token
        response = client.get(url, headers=bearer(token))
        assert response.status_code == 200
        assert b"SIBLINGCANARY" not in response.content, response.text[:800]
        items = response.json()["items"]
        withheld = [item for item in items if item.get("operation_id") == sibling.id]
        assert len(withheld) == 2
        for item in withheld:
            assert item["export_controlled"] is True and item["detail"] == REDACTED_VALUE
            assert item["occurred_at"] and item["actor_id"] is None, "dates and ids stay true"
        (job_item,) = [item for item in items if item.get("operation_id") is None]
        assert "export_controlled" not in job_item and job_item["detail"].startswith(CLEAR_TEXT)


# --------------------------------------------------------------------------- export audit rows


class TestNoExportRecordWithoutAnExport:
    @pytest.mark.parametrize(
        "path", ["/visitor-logs/export.csv", "/consumables/checkouts/export.csv", "/import/batches/1/failed-rows.csv"]
    )
    def test_a_read_only_token_is_refused_before_the_export_row_is_written(
        self, client: TestClient, db_session: Session, admin_headers: dict, admin_user: User, path: str
    ):
        """Compliance review P0, finding 7: the file-bytes rule replaces the body AFTER the route
        ran, and these exporters commit an EXPORT audit row first -- recording an export the
        caller never received. They now refuse a narrowed token before writing anything."""
        _id, token = issue_token(client, admin_headers, admin_user, scope="read_only", ai_external=False)
        before = db_session.query(AuditLog).filter(AuditLog.action == "EXPORT").count()
        response = client.get(API + path, headers=bearer(token))
        assert response.status_code == 403, response.text[:300]
        assert response.json() == {"detail": READ_ONLY_FILE_REFUSED_DETAIL}
        db_session.expire_all()
        assert db_session.query(AuditLog).filter(AuditLog.action == "EXPORT").count() == before
        # A person still exports.
        assert client.get(API + path, headers=admin_headers).status_code != 403


# --------------------------------------------------------------------------- tenancy


class TestTenantIsolation:
    def test_another_companys_contract_classifies_nothing_here(
        self, client: TestClient, db_session: Session, world: World, ai_token
    ):
        """Company 2 controls a customer with the SAME name as company 1's cleared one."""
        other = seed_world(db_session, company_id=2, prefix="B-")
        db_session.add(
            CustomerContract(
                company_id=2,
                customer_id=other.clear_customer.id,
                contract_name="Other tenant's ITAR program",
                itar_controlled=True,
            )
        )
        db_session.commit()
        _id, token = ai_token
        body = client.get(f"{API}/work-orders/{world.clear_wo.id}", headers=bearer(token)).json()
        assert "export_controlled" not in body and body["customer_name"] == world.clear_customer.name

    def test_each_company_is_classified_by_its_own_contracts(
        self, client: TestClient, db_session: Session, world: World, admin_headers: dict
    ):
        """Company 2 has the same customer, uncontrolled: its own AI token sees it in full,
        and never sees company 1's work order at all."""
        other = seed_world(db_session, company_id=2, controlled=False, prefix="B-")
        viewer2 = make_viewer(db_session, company_id=2, email="assistant-2@werco.com")
        admin2 = User(
            email="admin-2@werco.com",
            employee_id="EMP-ADMIN-2",
            first_name="Admin",
            last_name="Two",
            hashed_password="$2b$12$abcdefghijklmnopqrstuv",
            role=UserRole.ADMIN,
            is_active=True,
            company_id=2,
        )
        db_session.add(admin2)
        db_session.commit()
        _id, token = issue_token(client, user_headers(admin2), viewer2, scope="read_only")
        own = client.get(f"{API}/work-orders/{other.itar_wo.id}", headers=bearer(token))
        assert own.status_code == 200 and contains_canary(own.content)
        foreign = client.get(f"{API}/work-orders/{world.itar_wo.id}", headers=bearer(token))
        assert foreign.status_code == 404
        assert not contains_canary(foreign.content)


# --------------------------------------------------------------------------- the middleware, raw ASGI


def _marker(**overrides) -> ExportControlMarker:
    values = dict(company_id=1, user_id=1, api_token_id=1, jti_prefix="abcd1234", label="bot", ai_external=True)
    values.update(overrides)
    return ExportControlMarker(**values)


async def _drive(inner_messages, *, marker=None) -> List[dict]:
    """Run ``ExportControlMiddleware`` over an inner app that sends ``inner_messages``."""

    async def inner(scope, receive, send):
        if marker is not None:
            scope["state"][EXPORT_CONTROL_STATE_KEY] = marker
        for message in inner_messages:
            await send(message)

    sent: List[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": "GET", "path": "/api/v1/parts/", "headers": [], "client": ("1.2.3.4", 5)}
    await ExportControlMiddleware(inner)(scope, receive, send)
    return sent


def _start(content_type: bytes = b"application/json", status: int = 200, **extra) -> dict:
    headers = [(b"content-type", content_type), *extra.get("headers", [])]
    return {"type": "http.response.start", "status": status, "headers": headers}


class TestMiddlewareMessages:
    async def test_no_marker_passes_every_message_untouched(self):
        messages = [_start(b"application/pdf"), {"type": "http.response.body", "body": b"%PDF", "more_body": False}]
        assert await _drive(messages) == messages

    async def test_a_message_before_the_response_starts(self):
        """Nit N1 (code review P0 round 2): a message before ``http.response.start`` (Starlette's
        ``http.response.debug``, sent by a template response) fell through to the buffering
        branch and turned an UNMARKED caller's response into a 503. Unmarked, it is forwarded
        untouched; marked, it is dropped and the response that follows is still checked."""
        debug = {"type": "http.response.debug", "info": {"template": "x.html", "context": {"notes": "ITARCANARY"}}}
        body = {"type": "http.response.body", "body": b'{"ok": true}', "more_body": False}
        assert await _drive([debug, _start(), body]) == [debug, _start(), body]

        marked = await _drive([debug, _start(), body], marker=_marker(ai_external=False))
        assert debug not in marked
        assert [message["type"] for message in marked] == ["http.response.start", "http.response.body"]
        assert marked[0]["status"] == 200 and marked[1]["body"] == b'{"ok": true}'

    async def test_an_encoded_body_fails_closed(self):
        messages = [
            _start(headers=[(b"content-encoding", b"gzip")]),
            {"type": "http.response.body", "body": gzip.compress(b'{"a": 1}'), "more_body": False},
        ]
        sent = await _drive(messages, marker=_marker())
        assert sent[0]["status"] == 503
        assert json.loads(sent[1]["body"]) == {"detail": EXPORT_CONTROL_FAILED_DETAIL}

    async def test_a_file_handed_to_the_server_is_never_forwarded(self):
        pathsend = {"type": "http.response.pathsend", "path": "/srv/drawings/secret.pdf"}
        sent = await _drive([_start(), pathsend], marker=_marker())
        assert [message["type"] for message in sent] == ["http.response.start", "http.response.body"]
        assert sent[0]["status"] == 503

        read_only = await _drive([_start(b"application/pdf"), pathsend], marker=_marker(ai_external=False))
        assert pathsend not in read_only
        assert read_only[0]["status"] == 403
        assert json.loads(read_only[1]["body"]) == {"detail": READ_ONLY_FILE_REFUSED_DETAIL}

    async def test_an_empty_json_body_needs_no_index(self):
        sent = await _drive([_start(status=200), {"type": "http.response.body", "body": b"", "more_body": False}])
        assert sent[0]["status"] == 200
        sent = await _drive(
            [_start(), {"type": "http.response.body", "body": b"", "more_body": False}], marker=_marker()
        )
        assert sent[0]["status"] == 200 and sent[1]["body"] == b""

    async def test_a_refused_request_answered_2xx_is_never_forwarded(self, monkeypatch):
        """Code review P0, finding 5: if anything caught a gate's 403 and the route answered
        2xx, that body was never checked -- it must not go out under the refusal marker."""
        recorded = []
        monkeypatch.setattr(export_control_service, "record_refusal", lambda *a, **k: recorded.append(a))
        secret = {"type": "http.response.body", "body": b'{"notes": "ITARCANARY"}', "more_body": False}
        sent = await _drive([_start(status=200), secret], marker=_marker(refusal_reason="route_not_allowlisted"))
        assert sent[0]["status"] == 503
        assert json.loads(sent[1]["body"]) == {"detail": EXPORT_CONTROL_FAILED_DETAIL}
        assert all(b"ITARCANARY" not in message.get("body", b"") for message in sent)
        assert recorded, "the refusal itself is still ledgered"
        # The ordinary case -- the gate's own 403 -- still passes.
        refusal = {"type": "http.response.body", "body": b'{"detail": "no"}', "more_body": False}
        sent = await _drive([_start(status=403), refusal], marker=_marker(refusal_reason="route_not_allowlisted"))
        assert sent[0]["status"] == 403 and sent[1]["body"] == b'{"detail": "no"}'

    async def test_bodyless_statuses_pass(self):
        sent = await _drive([{"type": "http.response.start", "status": 204, "headers": []}], marker=_marker())
        assert sent[0]["status"] == 204


@pytest.mark.unit
class TestChanceMatchGuard:
    """The guard behind every absence check over raw response bytes in this file.

    On 2026-11-24 ``test_a_legacy_ai_estimates_dimensions_are_withheld_as_numbers`` failed on
    main with ``[b'7.94'] == []``: nothing leaked, the quote's ``created_at`` was
    ``...00:51:37.94....Z`` and its text contains ``7.94``. A red main skips the deploy."""

    @pytest.mark.parametrize("needle", [b"7.94", b"3.141592"], ids=["incident-7.94", "profile-3.141592"])
    def test_the_retired_needles_are_flagged_with_a_real_timestamp_containing_them(self, needle):
        instant = iso_timestamp_containing(needle)
        assert instant is not None and could_occur_in_iso_timestamp(needle)
        assert needle in instant.encode()
        datetime.fromisoformat(instant.removesuffix("Z"))  # a real instant, not just the right shape

    def test_the_incident_instant_itself_contains_the_old_needle(self):
        assert b"7.94" in b'"created_at":"2026-11-24T00:51:37.940000Z"'
        assert iso_timestamp_containing(b"37.94") is not None

    @pytest.mark.parametrize(
        "needle",
        [b"7.9375013", b"3.1415927", b"7329.15287", b"4321.98765", b"0.3125987", b"1.4142135", b"0.0625111"],
    )
    def test_the_dimension_needles_cannot_occur_in_any_timestamp(self, needle):
        assert iso_timestamp_containing(needle) is None and not could_occur_in_iso_timestamp(needle)

    def test_the_numeric_and_text_canaries_cannot_occur_in_any_timestamp(self):
        for needle in (*NUMERIC_CANARIES, CANARY.encode(), CANARY.lower().encode()):
            assert not could_occur_in_iso_timestamp(needle), needle

    def test_a_short_digit_run_is_flagged_because_a_microsecond_field_can_hold_it(self):
        for needle in (b"999", b"830416", b"13", b"7.9"):
            instant = iso_timestamp_containing(needle)
            assert instant is not None and needle in instant.encode(), needle
        for needle in (b"7777777", b"98765.", b"2026-13", b"24:00:00"):
            assert iso_timestamp_containing(needle) is None, needle

    def test_strip_iso_instants_removes_whole_instants_only(self):
        text = '{"at":"2026-11-24T15:15:22.830416Z","raw":"2026-11-24 15:15:22.830416","pin":"830416"}'
        stripped = strip_iso_instants(text)
        assert "2026-11-24" not in stripped and stripped.count("830416") == 1
        # A secret glued onto an instant, or a fraction no clock writes, is not hidden.
        assert "830416" in strip_iso_instants("2026-11-24T15:15:22.123456Z830416")
        assert "830416" in strip_iso_instants("2026-11-24T15:15:22.1234567830416Z")
