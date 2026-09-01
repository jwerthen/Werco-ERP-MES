"""The MCP server end to end over the SDK's in-memory transport, against the real routers.

Every test here drives a real ``ClientSession`` into ``build_server`` with an
``InProcessExecutor`` that dispatches into ``app.main:app`` -- through the middleware
stack, ``get_current_user``, ``require_role``, tenancy and audit -- with the test
database behind it. Nothing is mocked below the HTTP boundary except where a test
says so (the laser-nest import orchestrator, and two stub executors for the
result-shaping cases).

The owner's rules are the assertions, and each is pinned on the DATABASE ROW as well
as on the tool text, so a tool that merely *said* "draft" would still fail:

- no token / invalid token -> ``is_error`` 401, and no request is dispatched;
- ``create_work_order`` lands DRAFT even when the caller passes ``status: released``;
- ``duplicate_work_order`` lands DRAFT; ``release_work_order`` is the separate step;
- ``add_operation`` resolves a work center by NAME and refuses file-shaped names
  before any request is made;
- an operator is refused with the server's exact ``"Insufficient permissions"``;
- ``import_laser_nest_package`` demotes the RELEASED import to DRAFT, leaves it
  RELEASED only on ``release=true``, refuses a target carrying manual nests, and
  says so loudly when the demote itself fails;
- a generated read returns the caller's tenant only;
- argument validation, truncation, binary results and transport failures take the
  documented shapes.
"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import EmbeddedResource, TextContent

from app.main import app
from app.mcp.auth import TokenSource
from app.mcp.catalog import build_catalog
from app.mcp.convenience import (
    CONVENIENCE_TOOL_NAMES,
    CONVENIENCE_TOOLS,
    LASER_CUTTING_CREATE_REFUSAL,
    SHADOWED_OPERATIONS,
    operation_name_rejection,
)
from app.mcp.executor import InProcessExecutor
from app.mcp.results import ExecResult
from app.mcp.server import ResultCaps, build_server
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.laser_nest import LaserNest, LaserNestPackage
from app.models.part import Part
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.schemas.work_order import WorkOrderResponse

pytestmark = pytest.mark.api

FAKE_PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
BIG_CAPS = ResultCaps(max_result_chars=200_000, max_blob_bytes=5_000_000, max_upload_bytes=25_000_000)


@pytest.fixture(scope="module")
def catalog():
    return build_catalog(app.openapi(), shadowed=SHADOWED_OPERATIONS, reserved_names=CONVENIENCE_TOOL_NAMES)


def _bearer(headers: Dict[str, str]) -> str:
    return headers["Authorization"].split(" ", 1)[1]


@dataclass
class RecordingExecutor:
    """Wraps the real in-process executor and records every dispatch it is asked for."""

    inner: InProcessExecutor
    calls: List[Tuple[str, str]] = field(default_factory=list)

    async def request(self, **kwargs: Any) -> ExecResult:
        self.calls.append((kwargs["method"], kwargs["path"]))
        return await self.inner.request(**kwargs)

    async def aclose(self) -> None:
        await self.inner.aclose()


class CannedExecutor:
    """Answers every request with one canned outcome (or raises), for result shaping."""

    def __init__(self, result: Optional[ExecResult] = None, *, raises: Optional[BaseException] = None) -> None:
        self.result = result
        self.raises = raises
        self.calls = 0

    async def request(self, **kwargs: Any) -> ExecResult:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        assert self.result is not None
        return self.result

    async def aclose(self) -> None:
        return None


def make_server(
    catalog,
    *,
    headers: Optional[Dict[str, str]] = None,
    executor: Any = None,
    caps: ResultCaps = BIG_CAPS,
) -> Tuple[Server, Any]:
    """A server over ``executor`` (default: recording in-process) as the user in ``headers`` (None = no credentials)."""
    executor = executor or RecordingExecutor(InProcessExecutor(app, version=app.version))
    token_source = TokenSource(executor, access_token=_bearer(headers)) if headers else None
    server = build_server(executor, catalog=catalog, token_source=token_source, version=app.version, caps=caps)
    return server, executor


@asynccontextmanager
async def connected(server: Server) -> AsyncIterator[ClientSession]:
    """An initialized ``ClientSession`` talking to ``server`` over in-memory streams."""
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                server.run, server_streams[0], server_streams[1], server.create_initialization_options(), True
            )
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                yield session
            task_group.cancel_scope.cancel()


async def call(server: Server, name: str, arguments: Optional[Dict[str, Any]] = None):
    async with connected(server) as session:
        return await session.call_tool(name, arguments or {})


def catalog_by_name(catalog, name: str):
    return next(tool for tool in catalog if tool.name == name)


def _row(db_session, work_order_id: int) -> WorkOrder:
    db_session.expire_all()
    return db_session.query(WorkOrder).filter(WorkOrder.id == work_order_id).one()


def _operation_rows(db_session, work_order_id: int) -> List[WorkOrderOperation]:
    db_session.expire_all()
    return (
        db_session.query(WorkOrderOperation)
        .filter(WorkOrderOperation.work_order_id == work_order_id)
        .order_by(WorkOrderOperation.sequence)
        .all()
    )


# --------------------------------------------------------------------------- listing


class TestListing:
    async def test_lists_convenience_tools_first_then_generated_sorted(self, catalog):
        server, _ = make_server(catalog)
        async with connected(server) as session:
            listing = await session.list_tools()
        names = [tool.name for tool in listing.tools]
        convenience = [tool.name for tool in CONVENIENCE_TOOLS]
        assert names[: len(convenience)] == convenience
        generated = names[len(convenience) :]
        assert generated == sorted(tool.name for tool in catalog)
        assert len(names) == len(convenience) + len(catalog)
        by_name = {tool.name: tool for tool in listing.tools}
        assert by_name["create_work_order"].annotations.read_only_hint is False
        assert by_name["work_orders_list_work_orders"].annotations.read_only_hint is True
        assert "part_id" in by_name["create_work_order"].input_schema["properties"]

    async def test_unknown_tool_is_a_404_result(self, catalog):
        server, executor = make_server(catalog)
        result = await call(server, "no_such_tool", {})
        assert result.is_error and result.structured_content["status"] == 404
        assert executor.calls == []


# --------------------------------------------------------------------------- auth


class TestAuth:
    async def test_no_credentials_is_a_401_result_before_any_request(self, client, db_session, test_part, catalog):
        server, executor = make_server(catalog, headers=None)
        for name, arguments in (
            ("create_work_order", {"part_id": test_part.id, "quantity_ordered": 1}),
            ("work_orders_list_work_orders", {}),
        ):
            result = await call(server, name, arguments)
            assert result.is_error, name
            assert result.structured_content["status"] == 401
            assert "credentials" in result.structured_content["detail"].lower()
        assert executor.calls == [], "no request may be dispatched without a token"
        assert db_session.query(WorkOrder).count() == 0

    async def test_invalid_token_passes_the_routes_401_through_verbatim(self, client, catalog):
        headers = {"Authorization": "Bearer not-a-real-token"}
        server, executor = make_server(catalog, headers=headers)
        result = await call(server, "work_orders_list_work_orders", {})
        assert result.is_error and result.structured_content["status"] == 401
        # Exactly what the SPA would have been told, and exactly one attempt: a static
        # token with nothing to refresh or log in with is surfaced, not retried.
        direct = client.get("/api/v1/work-orders/", headers=headers)
        assert direct.status_code == 401
        assert result.structured_content["detail"] == direct.json()["detail"]
        assert executor.calls == [("GET", "/api/v1/work-orders/")]

    async def test_operator_is_refused_with_the_servers_exact_detail(
        self, client, db_session, operator_headers, test_part, catalog
    ):
        server, executor = make_server(catalog, headers=operator_headers)
        result = await call(server, "create_work_order", {"part_id": test_part.id, "quantity_ordered": 3})
        assert result.is_error
        assert result.structured_content == {
            "status": 403,
            "detail": "Insufficient permissions",
            "method": "POST",
            "path": "/api/v1/work-orders/",
        }
        assert executor.calls == [("POST", "/api/v1/work-orders/")]
        assert db_session.query(WorkOrder).count() == 0


# --------------------------------------------------------------------------- work-order lifecycle


class TestWorkOrderLifecycle:
    async def test_create_lands_draft_even_when_status_released_is_passed(
        self, client, db_session, manager_headers, test_user, test_part, catalog
    ):
        server, executor = make_server(catalog, headers=manager_headers)
        result = await call(
            server,
            "create_work_order",
            {"part_id": test_part.id, "quantity_ordered": 5, "status": "released", "priority": 2},
        )
        assert not result.is_error, result.content
        created = result.structured_content
        assert created["status"] == "draft" and created["part_id"] == test_part.id

        row = _row(db_session, created["id"])
        assert row.status == WorkOrderStatus.DRAFT
        assert row.released_at is None and row.released_by is None
        assert row.priority == 2
        # Written through the real route, so the audit row is the caller's (invariant 2).
        audit = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "work_order", AuditLog.resource_id == row.id)
            .all()
        )
        assert audit and all(entry.user_id == test_user.id for entry in audit)
        assert executor.calls == [("POST", "/api/v1/work-orders/")]

    async def test_create_refuses_laser_cutting_before_calling_the_route(
        self, client, db_session, manager_headers, test_part, catalog
    ):
        server, executor = make_server(catalog, headers=manager_headers)
        result = await call(
            server,
            "create_work_order",
            {"part_id": test_part.id, "quantity_ordered": 1, "work_order_type": "laser_cutting"},
        )
        assert result.is_error and result.structured_content["status"] == 422
        assert result.structured_content["detail"] == LASER_CUTTING_CREATE_REFUSAL
        assert executor.calls == []
        # Same wording as the API's own refusal, so the agent learns one message.
        direct = client.post(
            "/api/v1/work-orders/",
            json={"part_id": test_part.id, "quantity_ordered": 1, "work_order_type": "laser_cutting"},
            headers=manager_headers,
        )
        assert direct.status_code == 422
        assert any(LASER_CUTTING_CREATE_REFUSAL in str(err.get("msg")) for err in direct.json()["detail"])
        assert db_session.query(WorkOrder).count() == 0

    async def test_add_operations_by_name_get_duplicate_then_release_explicitly(
        self, client, db_session, manager_headers, test_part, test_work_center, catalog
    ):
        server, _ = make_server(catalog, headers=manager_headers)
        created = await call(server, "create_work_order", {"part_id": test_part.id, "quantity_ordered": 4})
        assert not created.is_error, created.content
        wo_id = created.structured_content["id"]
        wo_number = created.structured_content["work_order_number"]

        # Work center by exact NAME, then by CODE in a different case; sequence auto-appended.
        first = await call(
            server, "add_operation", {"work_order_id": wo_id, "name": "Laser", "work_center": test_work_center.name}
        )
        assert not first.is_error, first.content
        second = await call(
            server,
            "add_operation",
            {"work_order_id": wo_id, "name": "Brake", "work_center": test_work_center.code.lower()},
        )
        assert not second.is_error, second.content
        ops = _operation_rows(db_session, wo_id)
        assert [(op.name, op.sequence, op.work_center_id) for op in ops] == [
            ("Laser", 10, test_work_center.id),
            ("Brake", 20, test_work_center.id),
        ]

        by_id = await call(server, "get_work_order", {"work_order_id": wo_id})
        by_number = await call(server, "get_work_order", {"work_order_number": wo_number})
        for shown in (by_id, by_number):
            assert not shown.is_error, shown.content
            assert shown.structured_content["id"] == wo_id
            assert [op["name"] for op in shown.structured_content["operations"]] == ["Laser", "Brake"]

        duplicated = await call(server, "duplicate_work_order", {"work_order_id": wo_id, "quantity_ordered": 7})
        assert not duplicated.is_error, duplicated.content
        copy_payload = duplicated.structured_content["work_order"]
        assert copy_payload["id"] != wo_id and copy_payload["status"] == "draft"
        copy_row = _row(db_session, copy_payload["id"])
        assert copy_row.status == WorkOrderStatus.DRAFT
        assert float(copy_row.quantity_ordered) == 7.0
        assert [op.name for op in _operation_rows(db_session, copy_row.id)] == ["Laser", "Brake"]

        # Release is the separate, explicit step -- and it touches only the one it names.
        assert _row(db_session, wo_id).status == WorkOrderStatus.DRAFT
        released = await call(server, "release_work_order", {"work_order_id": wo_id})
        assert not released.is_error, released.content
        assert released.structured_content["status"] == "released"
        original = _row(db_session, wo_id)
        assert original.status == WorkOrderStatus.RELEASED and original.released_at is not None
        assert _row(db_session, copy_row.id).status == WorkOrderStatus.DRAFT

    async def test_add_operation_refuses_file_shaped_names_before_any_request(
        self, client, db_session, manager_headers, test_work_order, test_work_center, catalog
    ):
        server, executor = make_server(catalog, headers=manager_headers)
        before = len(_operation_rows(db_session, test_work_order.id))
        for bad, why in (
            ("nests/bracket_v2.dxf", "file path"),
            ("C:\\jobs\\plate.nc", "file path"),
            ("BRACKET_01.DXF", "file export"),
            ("traveler.pdf", "file export"),
            ("Part Detail", "DXF export label"),
            ("part detail", "DXF export label"),
        ):
            result = await call(
                server,
                "add_operation",
                {"work_order_id": test_work_order.id, "name": bad, "work_center": test_work_center.id},
            )
            assert result.is_error, bad
            assert result.structured_content["status"] == 422
            assert repr(bad) in result.structured_content["detail"], "the refused name is quoted back, not rewritten"
            assert why.split()[0] in result.structured_content["detail"]
        assert executor.calls == [], "the name guard runs before the work center lookup or any request"
        assert len(_operation_rows(db_session, test_work_order.id)) == before
        assert operation_name_rejection("Deburr") is None

    async def test_add_operation_refuses_ambiguous_or_unknown_work_center_names(
        self, client, db_session, manager_headers, test_work_order, test_work_center, catalog
    ):
        twin = WorkCenter(
            name=f"{test_work_center.name} Backup",
            code=f"{test_work_center.code}-B",
            work_center_type="welding",
            is_active=True,
            company_id=1,
        )
        db_session.add(twin)
        db_session.commit()
        server, _ = make_server(catalog, headers=manager_headers)
        before = len(_operation_rows(db_session, test_work_order.id))

        ambiguous = await call(
            server,
            "add_operation",
            {
                "work_order_id": test_work_order.id,
                "name": "Weld",
                "work_center": test_work_center.name.split()[0].lower(),
            },
        )
        assert ambiguous.is_error and ambiguous.structured_content["status"] == 409
        assert "2 work centers match" in ambiguous.structured_content["detail"]

        unknown = await call(
            server, "add_operation", {"work_order_id": test_work_order.id, "name": "Weld", "work_center": "waterjet"}
        )
        assert unknown.is_error and unknown.structured_content["status"] == 404
        assert "list_work_centers" in unknown.structured_content["detail"]

        # An exact name still wins over a substring twin.
        exact = await call(
            server,
            "add_operation",
            {"work_order_id": test_work_order.id, "name": "Weld", "work_center": test_work_center.name},
        )
        assert not exact.is_error, exact.content
        rows = _operation_rows(db_session, test_work_order.id)
        assert len(rows) == before + 1 and rows[-1].work_center_id == test_work_center.id

    async def test_list_work_centers_filters_by_name_or_code_substring(
        self, client, manager_headers, test_work_center, catalog
    ):
        server, _ = make_server(catalog, headers=manager_headers)
        result = await call(server, "list_work_centers", {"name": test_work_center.code[:3].lower()})
        assert not result.is_error, result.content
        rows = result.structured_content["result"]
        assert [row["id"] for row in rows] == [test_work_center.id]
        assert set(rows[0]) == {"id", "code", "name", "type", "is_active"}
        none = await call(server, "list_work_centers", {"name": "no-such-machine"})
        assert none.structured_content["result"] == []


# --------------------------------------------------------------------------- laser nest import


@pytest.fixture
def fake_nest_import(monkeypatch):
    """Replace the shared import orchestrator with one that mints a RELEASED laser WO.

    Mirrors what the real ``_run_laser_nest_import`` returns (``child_work_order`` is
    a ``WorkOrderResponse`` JSON dump) and how it is born (RELEASED, part-less,
    pooled). ``state.version_override`` lets one test hand the tool a stale version so
    the demote PUT is refused 409.
    """
    from app.api.endpoints import work_orders as work_orders_module

    state = SimpleNamespace(calls=[], version_override=None)

    async def fake(
        *,
        db,
        current_user,
        company_id,
        audit,
        target_work_order,
        file,
        source_path,
        work_center_id,
        rows,
        due_date=None,
        sheet_match_provenance=None,
    ):
        state.calls.append(
            {
                "target_id": getattr(target_work_order, "id", None),
                "filename": getattr(file, "filename", None),
                "source_path": source_path,
                "work_center_id": work_center_id,
                "rows": rows,
                "due_date": due_date,
            }
        )
        if target_work_order is not None and target_work_order.work_order_type == "laser_cutting":
            child = target_work_order
            child.status = WorkOrderStatus.RELEASED
        else:
            child = WorkOrder(
                company_id=company_id,
                work_order_number=f"WO-NEST-{len(state.calls)}",
                part_id=None,
                parent_work_order_id=None,
                work_order_type="laser_cutting",
                sequential_operations=False,
                quantity_ordered=3,
                status=WorkOrderStatus.RELEASED,
                priority=5,
                due_date=due_date,
                notes="fake standalone import",
                created_by=current_user.id,
            )
            db.add(child)
        db.commit()
        db.refresh(child)
        payload = WorkOrderResponse.model_validate(child).model_dump(mode="json")
        if state.version_override is not None:
            payload["version"] = state.version_override
        return {"package": {"package_name": "fake.zip", "nests": []}, "child_work_order": payload}

    monkeypatch.setattr(work_orders_module, "_run_laser_nest_import", fake)
    return state


def _seed_laser_work_order_with_nest(db_session, work_center: WorkCenter, *, cnc_file_name: Optional[str]) -> WorkOrder:
    """A RELEASED laser WO carrying one nest -- manual (``cnc_file_name`` None) or imported."""
    work_order = WorkOrder(
        company_id=1,
        work_order_number=f"WO-LASER-{'MANUAL' if cnc_file_name is None else 'IMPORTED'}",
        part_id=None,
        work_order_type="laser_cutting",
        sequential_operations=False,
        quantity_ordered=2,
        status=WorkOrderStatus.RELEASED,
        priority=5,
    )
    db_session.add(work_order)
    db_session.flush()
    operation = WorkOrderOperation(
        work_order_id=work_order.id, work_center_id=work_center.id, sequence=10, name="NEST-A", company_id=1
    )
    db_session.add(operation)
    db_session.flush()
    package = LaserNestPackage(
        company_id=1, child_work_order_id=work_order.id, package_name="pkg", import_status="imported"
    )
    db_session.add(package)
    db_session.flush()
    db_session.add(
        LaserNest(
            company_id=1,
            package_id=package.id,
            work_order_operation_id=operation.id,
            nest_name="NEST-A",
            cnc_file_name=cnc_file_name,
            cnc_number="P100",
            planned_runs=2,
        )
    )
    db_session.commit()
    return work_order


class TestLaserNestImport:
    async def test_standalone_import_is_demoted_to_draft(
        self, client, db_session, manager_headers, catalog, fake_nest_import
    ):
        server, executor = make_server(catalog, headers=manager_headers)
        result = await call(
            server,
            "import_laser_nest_package",
            {
                "file": {"filename": "pkg.zip", "content_base64": base64.b64encode(b"PK\x03\x04fake").decode()},
                "rows": [{"source_file": "n1.pdf", "planned_runs": 3}],
                "work_center_id": 42,
                "due_date": "2026-12-01",
            },
        )
        assert not result.is_error, result.content
        payload = result.structured_content
        assert payload["demoted_to_draft"] is True
        assert payload["import"]["child_work_order"]["status"] == "released", "the raw import evidence is kept"
        assert payload["work_order"]["status"] == "draft"
        assert payload["work_order"]["id"] == payload["import"]["child_work_order"]["id"]

        row = _row(db_session, payload["work_order"]["id"])
        assert row.status == WorkOrderStatus.DRAFT
        assert row.work_order_type == "laser_cutting"
        assert row.version == payload["import"]["child_work_order"]["version"] + 1, "the demote is a real PUT"

        assert len(fake_nest_import.calls) == 1
        seen = fake_nest_import.calls[0]
        assert seen["target_id"] is None and seen["filename"] == "pkg.zip"
        assert seen["rows"] == '[{"source_file": "n1.pdf", "planned_runs": 3}]'
        assert seen["work_center_id"] == 42 and str(seen["due_date"]) == "2026-12-01"
        assert executor.calls == [
            ("POST", "/api/v1/work-orders/laser-nest-packages/standalone/import"),
            ("PUT", f"/api/v1/work-orders/{row.id}"),
        ]

    async def test_release_true_leaves_the_import_released(
        self, client, db_session, manager_headers, catalog, fake_nest_import
    ):
        server, executor = make_server(catalog, headers=manager_headers)
        result = await call(server, "import_laser_nest_package", {"source_path": "/staged/pkg.zip", "release": True})
        assert not result.is_error, result.content
        assert result.structured_content["demoted_to_draft"] is False
        assert result.structured_content["work_order"]["status"] == "released"
        assert _row(db_session, result.structured_content["work_order"]["id"]).status == WorkOrderStatus.RELEASED
        assert fake_nest_import.calls[0]["source_path"] == "/staged/pkg.zip"
        assert executor.calls == [("POST", "/api/v1/work-orders/laser-nest-packages/standalone/import")]

    async def test_target_with_manual_nests_is_refused_before_the_import_runs(
        self, client, db_session, manager_headers, test_work_center, catalog, fake_nest_import
    ):
        target = _seed_laser_work_order_with_nest(db_session, test_work_center, cnc_file_name=None)
        server, executor = make_server(catalog, headers=manager_headers)
        result = await call(
            server, "import_laser_nest_package", {"work_order_id": target.id, "source_path": "/staged/pkg.zip"}
        )
        assert result.is_error and result.structured_content["status"] == 409
        detail = result.structured_content["detail"]
        assert "NEST-A" in detail and "manually entered" in detail and "never mixed" in detail
        assert fake_nest_import.calls == []
        assert executor.calls == [("GET", f"/api/v1/work-orders/{target.id}")]
        assert _row(db_session, target.id).status == WorkOrderStatus.RELEASED

    async def test_target_with_only_imported_nests_proceeds_and_is_demoted(
        self, client, db_session, manager_headers, test_work_center, catalog, fake_nest_import
    ):
        target = _seed_laser_work_order_with_nest(db_session, test_work_center, cnc_file_name="nest_a.lst")
        server, executor = make_server(catalog, headers=manager_headers)
        result = await call(
            server, "import_laser_nest_package", {"work_order_id": target.id, "source_path": "/staged/pkg.zip"}
        )
        assert not result.is_error, result.content
        assert fake_nest_import.calls[0]["target_id"] == target.id
        assert result.structured_content["demoted_to_draft"] is True
        assert _row(db_session, target.id).status == WorkOrderStatus.DRAFT
        assert executor.calls[0] == ("GET", f"/api/v1/work-orders/{target.id}")
        assert executor.calls[1] == ("POST", f"/api/v1/work-orders/{target.id}/laser-nest-packages/import")
        assert executor.calls[2] == ("PUT", f"/api/v1/work-orders/{target.id}")

    async def test_failed_demote_is_reported_loudly_with_the_released_work_order(
        self, client, db_session, manager_headers, catalog, fake_nest_import
    ):
        fake_nest_import.version_override = 99  # stale -> the demote PUT is refused 409
        server, _ = make_server(catalog, headers=manager_headers)
        result = await call(server, "import_laser_nest_package", {"source_path": "/staged/pkg.zip"})
        assert result.is_error
        payload = result.structured_content
        assert payload["status"] == 409
        assert "IMPORT SUCCEEDED" in payload["detail"] and "still RELEASED" in payload["detail"]
        assert "modified by someone else" in payload["detail"], "the route's own 409 detail is quoted"
        assert payload["demoted_to_draft"] is False
        assert payload["work_order"]["status"] == "released" and payload["import"]["child_work_order"]["id"]
        assert _row(db_session, payload["work_order"]["id"]).status == WorkOrderStatus.RELEASED

    async def test_decoded_upload_over_the_cap_is_a_413_result_before_any_request(
        self, client, db_session, manager_headers, catalog, fake_nest_import
    ):
        caps = ResultCaps(max_result_chars=200_000, max_blob_bytes=5_000_000, max_upload_bytes=16)
        server, executor = make_server(catalog, headers=manager_headers, caps=caps)
        blob = base64.b64encode(b"x" * 64).decode()
        result = await call(
            server, "import_laser_nest_package", {"file": {"filename": "big.zip", "content_base64": blob}}
        )
        assert result.is_error and result.structured_content["status"] == 413
        assert "16-byte MCP upload cap" in result.structured_content["detail"]
        assert executor.calls == [] and fake_nest_import.calls == []
        assert db_session.query(WorkOrder).count() == 0
        # A generated multipart tool decodes through the same cap.
        upload_tools = [tool.name for tool in catalog if tool.file_fields]
        result = await call(
            server,
            upload_tools[0],
            {catalog_by_name(catalog, upload_tools[0]).file_fields[0]: {"filename": "b.csv", "content_base64": blob}},
        )
        assert result.is_error and result.structured_content["status"] == 413
        assert executor.calls == []

    async def test_import_requires_a_file_or_a_source_path(self, client, manager_headers, catalog, fake_nest_import):
        server, executor = make_server(catalog, headers=manager_headers)
        result = await call(server, "import_laser_nest_package", {"rows": []})
        assert result.is_error and result.structured_content["status"] == 422
        assert executor.calls == [] and fake_nest_import.calls == []


# --------------------------------------------------------------------------- generated tools


class TestGeneratedTools:
    async def test_generated_read_returns_the_callers_tenant_only(
        self, client, db_session, manager_headers, test_work_order, catalog
    ):
        db_session.add(Company(id=2, name="Other Manufacturing", slug="other", is_active=True))
        db_session.flush()
        other_part = Part(
            part_number="P-OTHER-1", name="Other part", part_type="manufactured", unit_of_measure="each", company_id=2
        )
        db_session.add(other_part)
        db_session.flush()
        db_session.add(
            WorkOrder(
                work_order_number="WO-OTHER-1",
                part_id=other_part.id,
                quantity_ordered=1,
                status="draft",
                priority=5,
                company_id=2,
            )
        )
        db_session.commit()
        assert db_session.query(WorkOrder).filter(WorkOrder.company_id == 2).count() == 1

        server, executor = make_server(catalog, headers=manager_headers)
        result = await call(server, "work_orders_list_work_orders", {"limit": 50})
        assert not result.is_error, result.content
        numbers = {row["work_order_number"] for row in result.structured_content["result"]}
        assert test_work_order.work_order_number in numbers
        assert "WO-OTHER-1" not in numbers
        assert executor.calls == [("GET", "/api/v1/work-orders/")]

    async def test_generated_write_goes_through_the_real_route(
        self, client, db_session, manager_headers, test_work_order, catalog
    ):
        server, executor = make_server(catalog, headers=manager_headers)
        original_version = test_work_order.version
        stale = await call(
            server,
            "work_orders_update_work_order",
            {"work_order_id": test_work_order.id, "version": 999, "priority": 1},
        )
        assert stale.is_error and stale.structured_content["status"] == 409
        assert "modified by someone else" in stale.structured_content["detail"]

        fresh = await call(
            server,
            "work_orders_update_work_order",
            {"work_order_id": test_work_order.id, "version": original_version, "priority": 1},
        )
        assert not fresh.is_error, fresh.content
        row = _row(db_session, test_work_order.id)
        assert row.priority == 1 and row.version == original_version + 1
        assert executor.calls == [("PUT", f"/api/v1/work-orders/{test_work_order.id}")] * 2

    async def test_argument_validation_failure_is_a_422_result_with_a_helpful_message(
        self, client, db_session, manager_headers, test_part, catalog
    ):
        server, executor = make_server(catalog, headers=manager_headers)
        missing = await call(server, "create_work_order", {"quantity_ordered": 5})
        assert missing.is_error and missing.structured_content["status"] == 422
        assert any("part_id" in msg and "required" in msg for msg in missing.structured_content["detail"])
        assert "inputSchema" in missing.structured_content["hint"]

        wrong_type = await call(server, "create_work_order", {"part_id": "abc", "quantity_ordered": 5})
        assert wrong_type.is_error and wrong_type.structured_content["status"] == 422
        assert any("part_id" in msg for msg in wrong_type.structured_content["detail"])

        generated = await call(server, "work_orders_list_work_orders", {"limit": "many"})
        assert generated.is_error and generated.structured_content["status"] == 422
        assert executor.calls == [], "invalid arguments never reach the API"
        assert db_session.query(WorkOrder).count() == 0


# --------------------------------------------------------------------------- result shaping


class TestResultShaping:
    async def test_oversized_json_is_truncated_with_a_note_and_a_marker(
        self, client, manager_headers, test_work_order, catalog
    ):
        caps = ResultCaps(max_result_chars=120, max_blob_bytes=5_000_000, max_upload_bytes=25_000_000)
        server, _ = make_server(catalog, headers=manager_headers, caps=caps)
        result = await call(server, "get_work_order", {"work_order_id": test_work_order.id})
        assert not result.is_error
        text = result.content[0].text
        assert "[truncated: 120 of " in text and text.endswith("narrow with limit/skip or filters]")
        assert text.startswith("{")
        marker = result.structured_content
        assert marker["truncated"] is True and marker["status"] == 200 and marker["chars"] > 120
        assert "work_order_number" not in marker, "the payload is never handed over twice"

    async def test_binary_response_becomes_an_embedded_resource(self, client, manager_headers, catalog):
        executor = CannedExecutor(
            ExecResult(
                status=200,
                headers={"content-disposition": 'attachment; filename="traveler.pdf"'},
                content=FAKE_PDF,
                content_type="application/pdf",
            )
        )
        server, _ = make_server(catalog, headers=manager_headers, executor=executor)
        result = await call(server, "work_orders_list_work_orders", {})
        assert not result.is_error and executor.calls == 1
        [resource] = result.content
        assert isinstance(resource, EmbeddedResource)
        assert base64.b64decode(resource.resource.blob) == FAKE_PDF
        assert resource.resource.mime_type == "application/pdf"
        assert str(resource.resource.uri).startswith("werco://work_orders_list_work_orders/")
        assert result.structured_content["filename"] == "traveler.pdf"
        assert result.structured_content["bytes"] == len(FAKE_PDF)

    async def test_binary_over_the_blob_cap_is_an_error_pointing_at_the_ui(self, client, manager_headers, catalog):
        executor = CannedExecutor(ExecResult(status=200, content=FAKE_PDF, content_type="application/pdf"))
        caps = ResultCaps(max_result_chars=200_000, max_blob_bytes=len(FAKE_PDF) - 1, max_upload_bytes=25_000_000)
        server, _ = make_server(catalog, headers=manager_headers, executor=executor, caps=caps)
        result = await call(server, "work_orders_list_work_orders", {})
        assert result.is_error
        assert "Werco ERP UI" in result.structured_content["detail"]
        assert result.structured_content["bytes"] == len(FAKE_PDF)
        assert all(isinstance(item, TextContent) for item in result.content)

    async def test_empty_2xx_is_ok_with_status(self, client, manager_headers, catalog):
        executor = CannedExecutor(ExecResult(status=204, content=b"", content_type=""))
        server, _ = make_server(catalog, headers=manager_headers, executor=executor)
        result = await call(server, "work_orders_list_work_orders", {})
        assert not result.is_error and result.structured_content == {"ok": True, "status": 204}

    async def test_transport_failure_is_status_zero_naming_the_exception(self, client, manager_headers, catalog):
        executor = CannedExecutor(raises=RuntimeError("socket melted"))
        server, _ = make_server(catalog, headers=manager_headers, executor=executor)
        result = await call(server, "work_orders_list_work_orders", {})
        assert result.is_error
        assert result.structured_content["status"] == 0
        assert result.structured_content["detail"] == "RuntimeError: socket melted"
        assert (
            result.structured_content["method"] == "GET" and result.structured_content["path"] == "/api/v1/work-orders/"
        )
