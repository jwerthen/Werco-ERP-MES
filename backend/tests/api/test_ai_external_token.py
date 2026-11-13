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
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services import export_control_service, llm_client
from tests.api.export_control_helpers import (
    CANARY,
    CLEAR_CUSTOMER_CODE,
    CLEAR_TEXT,
    World,
    bearer,
    contains_canary,
    contains_numeric_canary,
    find_record,
    issue_token,
    make_viewer,
    seed_world,
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
    ("GET", "/quotes/"): (_url("/quotes/"), True),
    ("GET", "/quotes/{quote_id}"): (_url("/quotes/{quote_id}"), True),
    ("GET", "/purchasing/vendors"): (_url("/purchasing/vendors"), False),
    ("GET", "/purchasing/vendors/{vendor_id}"): (_url("/purchasing/vendors/{vendor_id}"), False),
    ("GET", "/purchasing/purchase-orders"): (_url("/purchasing/purchase-orders"), False),
    ("GET", "/purchasing/purchase-orders/{po_id}"): (_url("/purchasing/purchase-orders/{po_id}"), True),
    ("GET", "/receiving/history"): (_url("/receiving/history"), True),
    ("GET", "/receiving/receipt/{receipt_id}"): (_url("/receiving/receipt/{receipt_id}"), True),
    ("GET", "/maintenance/work-orders"): (_url("/maintenance/work-orders"), True),
    ("GET", "/maintenance/work-orders/overdue"): (_url("/maintenance/work-orders/overdue"), True),
    ("GET", "/maintenance/work-orders/{wo_id}"): (_url("/maintenance/work-orders/{wo_id}"), True),
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
