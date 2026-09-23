"""Document imports create exactly one reviewed PO, open for receiving when selected."""

from copy import deepcopy
from datetime import date, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.hank import HankTask
from app.models.hank_intake import HankIntakeBatch, HankIntakeFile
from app.models.inventory import InventoryItem
from app.models.part import Part
from app.models.purchasing import POReceipt, POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.role_permission import RolePermission
from app.schemas.hank_intake import IntakeExtraction
from app.schemas.purchasing import POCreate
from app.services.audit_service import AuditService, AuditWriteError
from app.services.erp_draft_commands import generate_po_number
from app.services.hank_intake_purchase_order_service import HankIntakePurchaseOrderService, _date, _price
from app.services.hank_task_service import _digest, _row_values

from .test_hank_tasks import execute, propose
from .test_hank_tasks import vendor as _vendor

vendor = _vendor


@pytest.fixture
def source(db_session, test_user, test_part, vendor):
    batch = HankIntakeBatch(
        company_id=1, owner_id=test_user.id, credential_key="user", request_key=str(uuid4()), request_hash="a" * 64
    )
    db_session.add(batch)
    db_session.flush()
    fields = [
        ("po_number", "OLD-PO-123"),
        ("vendor_name", vendor.name),
        ("date", "2026-09-20"),
        ("due_date", "2026-10-01"),
    ]
    row = HankIntakeFile(
        company_id=1,
        batch_id=batch.id,
        ordinal=0,
        filename="original-po.docx",
        content_sha256="b" * 64,
        storage_ref="private-test.docx",
        file_size=100,
        status="awaiting_review",
        version=2,
        analysis_json=IntakeExtraction(
            classification="purchase_order",
            confidence="high",
            summary="Original purchase order",
            fields=[
                {"name": name, "value": value, "confidence": "high", "evidence": [{"page": 1, "excerpt": value}]}
                for name, value in fields
            ],
            lines=[
                {
                    "part_number": test_part.part_number,
                    "description": "Ordered material",
                    "quantity": "3",
                    "unit_of_measure": "ea",
                    "unit_price": "$12.50",
                    "confidence": "high",
                    "evidence": [{"page": 1, "excerpt": f"{test_part.part_number} 3 ea $12.50"}],
                }
            ],
        ).model_dump(),
    )
    db_session.add(row)
    db_session.commit()
    return row


def import_input(source, vendor, part, **overrides):
    return {
        "vendor_id": vendor.id,
        "source_intake_file_id": source.id,
        "source_intake_version": source.version,
        "po_number": "OLD-PO-123",
        "order_date": "2026-09-20",
        "required_date": "2026-10-01",
        "ready_for_receiving": True,
        "lines": [
            {
                "part_id": part.id,
                "quantity_ordered": 3,
                "unit_price": 12.50,
                "source_line_index": 0,
                "unit_of_measure": "each",
            }
        ],
        **overrides,
    }


def service(db, user):
    return HankIntakePurchaseOrderService(db, user, 1)


def edit_analysis(db, source, mutate):
    analysis = deepcopy(source.analysis_json)
    mutate(analysis)
    source.analysis_json = analysis
    db.commit()


def test_draft_is_read_only_preserves_evidence_and_suggests_exact_matches(
    db_session, test_user, source, test_part, vendor
):
    result = service(db_session, test_user).draft(source.id)
    assert result.po_number == "OLD-PO-123"
    assert result.vendor_id == vendor.id
    assert result.order_date == date(2026, 9, 20)
    assert result.required_date == date(2026, 10, 1)
    assert result.can_ready_for_receiving
    assert result.lines[0].part_id == test_part.id
    assert result.lines[0].quantity_ordered == 3
    assert result.lines[0].unit_price_amount == 12.5
    assert result.lines[0].evidence[0].page == 1
    assert db_session.query(PurchaseOrder).count() == 0
    assert db_session.query(HankTask).count() == 0


def test_reviewed_import_preserves_number_and_reaches_receiving_without_receiving_stock(
    client, auth_headers, db_session, source, vendor, test_part
):
    payload = import_input(source, vendor, test_part)
    task_response = propose(client, auth_headers, "draft_purchase_order", payload)
    assert task_response.status_code == 200, task_response.text
    task = task_response.json()
    assert "sent" in " ".join(task["preview"]["changes"])
    assert db_session.query(PurchaseOrder).count() == 0
    result = execute(client, auth_headers, task)
    assert result.status_code == 200, result.text
    po = db_session.query(PurchaseOrder).one()
    assert po.po_number == "OLD-PO-123"
    assert po.status == POStatus.SENT
    assert po.order_date == date(2026, 9, 20)
    assert po.required_date == date(2026, 10, 1)
    assert po.source_document_path == source.storage_ref
    assert po.approved_at is None and po.approved_by is None
    assert po.total == 37.5
    line = db_session.query(PurchaseOrderLine).one()
    assert line.part_id == test_part.id
    assert line.quantity_ordered == 3 and line.quantity_received == 0
    assert line.unit_price == 12.5
    open_response = client.get("/api/v1/receiving/open-pos", headers=auth_headers)
    assert open_response.status_code == 200, open_response.text
    assert open_response.json()[0]["po_number"] == "OLD-PO-123"
    assert open_response.json()[0]["lines"][0]["quantity_remaining"] == 3
    assert db_session.query(POReceipt).count() == 0
    assert db_session.query(InventoryItem).count() == 0
    assert db_session.query(AuditLog).filter_by(resource_type="purchase_order").count() == 2
    assert execute(client, auth_headers, task).json() == result.json()
    assert db_session.query(PurchaseOrder).count() == 1


def test_import_can_remain_draft(client, auth_headers, db_session, source, vendor, test_part):
    task = propose(
        client, auth_headers, "draft_purchase_order", import_input(source, vendor, test_part, ready_for_receiving=False)
    ).json()
    result = execute(client, auth_headers, task)
    assert result.status_code == 200, result.text
    assert db_session.query(PurchaseOrder).one().status == POStatus.DRAFT
    assert client.get("/api/v1/receiving/open-pos", headers=auth_headers).json() == []


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"source_intake_version": None}, 422),
        ({"po_number": None}, 422),
        ({"po_number": "  "}, 422),
        ({"source_intake_version": 999}, 409),
        ({"po_number": "x" * 51}, 422),
    ],
)
def test_source_contract_rejects_incomplete_or_stale_input(
    client, auth_headers, source, vendor, test_part, change, expected
):
    response = propose(client, auth_headers, "draft_purchase_order", import_input(source, vendor, test_part, **change))
    assert response.status_code == expected, response.text


@pytest.mark.parametrize(
    "change", [{"ready_for_receiving": True}, {"po_number": "CUSTOM"}, {"order_date": "2026-01-01"}]
)
def test_manual_task_cannot_gain_import_authority(client, auth_headers, vendor, test_part, change):
    response = propose(
        client,
        auth_headers,
        "draft_purchase_order",
        {
            "vendor_id": vendor.id,
            "lines": [{"part_id": test_part.id, "quantity_ordered": 1, "unit_price": 2}],
            **change,
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data["lines"][0].update(source_line_index=1),
        lambda data: data["lines"][0].pop("source_line_index"),
        lambda data: data["lines"][0].update(unit_of_measure="feet"),
        lambda data: data["lines"].append(dict(data["lines"][0])),
    ],
)
def test_complete_source_line_and_stocking_unit_checks(client, auth_headers, source, vendor, test_part, mutate):
    data = import_input(source, vendor, test_part)
    mutate(data)
    response = propose(client, auth_headers, "draft_purchase_order", data)
    assert response.status_code in (409, 422), response.text


@pytest.mark.parametrize("deleted", [False, True])
def test_existing_number_blocks_import_even_case_changed_or_deleted(
    client, auth_headers, db_session, source, vendor, test_part, deleted
):
    existing = PurchaseOrder(company_id=1, vendor_id=vendor.id, po_number="old-po-123", is_deleted=deleted)
    db_session.add(existing)
    db_session.commit()
    response = propose(client, auth_headers, "draft_purchase_order", import_input(source, vendor, test_part))
    assert response.status_code == 409


def test_duplicate_number_created_after_review_blocks_execution(
    client, auth_headers, db_session, source, vendor, test_part
):
    task = propose(client, auth_headers, "draft_purchase_order", import_input(source, vendor, test_part)).json()
    db_session.add(PurchaseOrder(company_id=1, vendor_id=vendor.id, po_number="OLD-PO-123"))
    db_session.commit()
    result = execute(client, auth_headers, task)
    assert result.status_code == 409
    assert db_session.query(PurchaseOrder).count() == 1


@pytest.mark.parametrize("change", ["source_version", "source_analysis", "vendor", "part"])
def test_reviewed_source_drift_requires_new_review(client, auth_headers, db_session, source, vendor, test_part, change):
    task = propose(client, auth_headers, "draft_purchase_order", import_input(source, vendor, test_part)).json()
    if change == "source_version":
        source.version += 1
    elif change == "source_analysis":
        source.analysis_json = {**source.analysis_json, "summary": "Changed extraction"}
    elif change == "vendor":
        vendor.name = "Changed supplier"
    else:
        test_part.unit_of_measure = "feet"
    db_session.commit()
    assert execute(client, auth_headers, task).status_code == 409
    assert db_session.query(PurchaseOrder).count() == 0


def test_duplicate_source_cannot_be_imported_under_another_number_and_exposes_only_po_link(
    client, auth_headers, db_session, test_user, source, vendor, test_part
):
    task = propose(client, auth_headers, "draft_purchase_order", import_input(source, vendor, test_part)).json()
    assert execute(client, auth_headers, task).status_code == 200
    clone = HankIntakeFile(
        company_id=1,
        batch_id=source.batch_id,
        ordinal=1,
        filename="duplicate.xlsx",
        content_sha256=source.content_sha256,
        storage_ref="copy.xlsx",
        file_size=100,
        status="awaiting_review",
        version=2,
        analysis_json=source.analysis_json,
    )
    db_session.add(clone)
    db_session.commit()
    result = service(db_session, test_user).draft(clone.id)
    assert result.blocked_reason and result.has_duplicates
    assert result.existing_purchase_orders[0].po_number == "OLD-PO-123"
    assert "task" not in result.model_dump_json()
    response = propose(
        client, auth_headers, "draft_purchase_order", import_input(clone, vendor, test_part, po_number="NEW-NUMBER")
    )
    assert response.status_code == 409
    assert db_session.query(PurchaseOrder).count() == 1


def test_private_source_and_active_permissions_are_enforced(
    client, auth_headers, admin_headers, db_session, test_user, source, vendor, test_part
):
    foreign = propose(client, admin_headers, "draft_purchase_order", import_input(source, vendor, test_part))
    assert foreign.status_code == 404
    db_session.add(
        RolePermission(company_id=1, role=test_user.role, permissions=["purchasing:view", "purchasing:create"])
    )
    db_session.commit()
    result = service(db_session, test_user).draft(source.id)
    assert not result.can_ready_for_receiving
    assert (
        propose(client, auth_headers, "draft_purchase_order", import_input(source, vendor, test_part)).status_code
        == 403
    )
    assert (
        propose(
            client,
            auth_headers,
            "draft_purchase_order",
            import_input(source, vendor, test_part, ready_for_receiving=False),
        ).status_code
        == 200
    )


def test_foreign_vendor_or_part_cannot_be_selected(client, auth_headers, db_session, source, vendor, test_part):
    db_session.add(Company(id=2, slug="foreign-office", name="Foreign company"))
    other = Vendor(company_id=2, code="FOREIGN", name="Foreign supplier", is_active=True)
    db_session.add(other)
    db_session.commit()
    data = import_input(source, vendor, test_part, vendor_id=other.id)
    assert propose(client, auth_headers, "draft_purchase_order", data).status_code == 404
    test_part.company_id = 2
    db_session.commit()
    assert (
        propose(client, auth_headers, "draft_purchase_order", import_input(source, vendor, test_part)).status_code
        == 404
    )


@pytest.mark.parametrize("failure", ["create_audit", "status_audit", "task_audit"])
def test_audit_failure_rolls_back_import_and_all_status_changes(
    client, auth_headers, db_session, source, vendor, test_part, monkeypatch, failure
):
    task = propose(client, auth_headers, "draft_purchase_order", import_input(source, vendor, test_part)).json()
    method = {"create_audit": "log_create", "status_audit": "log_status_change", "task_audit": "log_required"}[failure]
    original = getattr(AuditService, method)

    def fail(self, *args, **kwargs):
        if method != "log_required" or args[:2] == ("UPDATE", "hank_task"):
            raise AuditWriteError("test audit failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(AuditService, method, fail)
    result = execute(client, auth_headers, task)
    assert result.status_code == 503, result.text
    db_session.rollback()
    assert db_session.query(PurchaseOrder).count() == 0
    assert db_session.query(PurchaseOrderLine).count() == 0
    assert db_session.query(AuditLog).filter_by(resource_type="purchase_order").count() == 0
    assert db_session.get(HankTask, task["id"]).status == "awaiting_review"


@pytest.mark.parametrize("change", ["low", "missing_evidence", "unit_mismatch", "fraction", "currency"])
def test_uncertain_source_values_are_not_prefilled(db_session, test_user, source, change):
    def mutate(analysis):
        line = analysis["lines"][0]
        if change == "low":
            line["confidence"] = "low"
        elif change == "missing_evidence":
            line["evidence"] = []
        elif change == "unit_mismatch":
            line["unit_of_measure"] = "ft"
        elif change == "fraction":
            line["quantity"] = "1/2"
        else:
            analysis["fields"].append(
                {"name": "currency", "value": "EUR", "confidence": "high", "evidence": [{"page": 1, "excerpt": "EUR"}]}
            )

    edit_analysis(db_session, source, mutate)
    result = service(db_session, test_user).draft(source.id)
    if change == "currency":
        assert result.lines[0].unit_price_amount is None
    else:
        assert result.lines[0].quantity_ordered is None
    if change in ("low", "missing_evidence", 'unit_mismatch'):
        assert result.lines[0].unit_price_amount is None
    if change in ("low", "missing_evidence"):
        assert result.lines[0].part_id is None


def test_ambiguous_vendor_and_part_are_employee_choices(db_session, test_user, source, vendor, test_part):
    db_session.add(Vendor(company_id=1, code="SAME-NAME", name=vendor.name, is_active=True))
    db_session.add(
        Part(
            company_id=1,
            part_number=test_part.part_number.lower(),
            name="Ambiguous part",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
        )
    )
    db_session.commit()
    result = service(db_session, test_user).draft(source.id)
    assert result.vendor_id is None and len(result.vendors) == 2
    assert result.lines[0].part_id is None and len(result.lines[0].candidates) == 2


@pytest.mark.parametrize("classification", ["vendor_quote", "packing_slip", "other"])
def test_non_po_documents_cannot_create_orders(db_session, test_user, source, classification):
    edit_analysis(db_session, source, lambda data: data.update(classification=classification))
    with pytest.raises(HTTPException) as failure:
        service(db_session, test_user).draft(source.id)
    assert failure.value.status_code == 409


def test_preexisting_legacy_draft_fingerprint_still_executes(
    client, auth_headers, db_session, test_user, vendor, test_part
):
    payload = {"vendor_id": vendor.id, "lines": [{"part_id": test_part.id, "quantity_ordered": 2, "unit_price": 3}]}
    key = str(uuid4())
    task = propose(client, auth_headers, "draft_purchase_order", payload, key).json()
    row = db_session.get(HankTask, task["id"])
    original_input = POCreate.model_validate(payload).model_dump(mode='json')
    original_command = {
        'expected_company_id': 1,
        'request_key': key,
        'kind': 'draft_purchase_order',
        'input': original_input,
    }
    original_hash = _digest(
        {'schema': 1, 'company_id': 1, 'owner_id': test_user.id, 'credential_key': 'user', 'command': original_command}
    )
    assert row.request_hash == original_hash
    assert row.input_json == original_input
    assert propose(client, auth_headers, 'draft_purchase_order', payload, key).json()['id'] == task['id']
    row.source_versions_json = {
        "vendor_sha256": _digest(_row_values(vendor)),
        "parts_sha256": _digest([_row_values(test_part)]),
    }
    # Exact original input shape omits all newly optional source metadata.
    row.input_json = {
        **payload,
        "required_date": None,
        "expected_date": None,
        "ship_to": None,
        "shipping_method": None,
        "notes": None,
    }
    db_session.commit()
    response = execute(client, auth_headers, task)
    assert response.status_code == 200, response.text
    assert db_session.query(PurchaseOrder).one().status == POStatus.DRAFT


@pytest.mark.parametrize("value", ["1/2", "NaN", "Infinity", "1e3", "1,23", "($2)", "12 USD", "-1"])
def test_price_parser_never_guesses(value):
    assert _price(value) is None


def test_price_and_date_parsers_preserve_unambiguous_values():
    assert _price("$1,234.50") == 1234.50
    assert _price("0") == 0
    assert _date("09/10/2026") is None
    assert _date("2026-09-23") == date(2026, 9, 23)


@pytest.mark.parametrize(
    'numbers,expected',
    [
        (['ABC', '002', '010'], '011'),
        (['000099', '100', 'Z-FINAL'], '101'),
        (['99999999999999999999999999999999999999', '001'], '002'),
    ],
)
def test_imported_identifiers_cannot_poison_auto_numbering(db_session, vendor, numbers, expected):
    prefix = f'PO-{datetime.now():%Y%m%d}-'
    for suffix in numbers:
        db_session.add(PurchaseOrder(company_id=1, vendor_id=vendor.id, po_number=prefix + suffix))
    db_session.commit()
    assert generate_po_number(db_session, 1) == prefix + expected


def test_auto_numbering_remains_company_scoped_and_normalizes_case(db_session, vendor):
    prefix = f'PO-{datetime.now():%Y%m%d}-'
    db_session.add(Company(id=2, slug='another-number-shop', name='Other shop'))
    db_session.add(PurchaseOrder(company_id=2, vendor_id=vendor.id, po_number=prefix + '900'))
    db_session.add(PurchaseOrder(company_id=1, vendor_id=vendor.id, po_number=prefix.lower() + '009'))
    db_session.commit()
    assert generate_po_number(db_session, 1) == prefix + '010'


def test_po_import_endpoint_returns_typed_draft(client, auth_headers, source):
    response = client.get(f'/api/v1/hank/intake/files/{source.id}/purchase-order-draft', headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()['po_number'] == 'OLD-PO-123'
    assert response.json()['file_version'] == source.version


def test_import_serializes_number_check_before_source_lock(
    client, auth_headers, source, vendor, test_part, monkeypatch
):
    from app.services import hank_intake_purchase_order_service as imports

    task = propose(client, auth_headers, 'draft_purchase_order', import_input(source, vendor, test_part)).json()
    calls = []
    original = imports.HankIntakePurchaseOrderService.source

    def read_source(self, *args, **kwargs):
        if kwargs.get('locked'):
            calls.append('source')
            assert 'po_number' in calls
        return original(self, *args, **kwargs)

    monkeypatch.setattr(imports, 'acquire_generator_lock', lambda db, name, company: calls.append(name))
    monkeypatch.setattr(imports.HankIntakePurchaseOrderService, 'source', read_source)
    result = execute(client, auth_headers, task)
    assert result.status_code == 200, result.text
    assert calls[0] == 'po_number'


def test_missing_source_lines_and_incomplete_extractions_are_blocked(
    client, auth_headers, db_session, test_user, source, vendor, test_part
):
    edit_analysis(db_session, source, lambda data: data['lines'].append(deepcopy(data['lines'][0])))
    response = propose(client, auth_headers, 'draft_purchase_order', import_input(source, vendor, test_part))
    assert response.status_code == 409
    edit_analysis(db_session, source, lambda data: data.update(has_more_lines=True))
    with pytest.raises(HTTPException) as failure:
        service(db_session, test_user).draft(source.id)
    assert failure.value.status_code == 409


def test_printed_total_mismatch_is_disclosed_in_suggestion_and_final_review(
    client, auth_headers, db_session, test_user, source, vendor, test_part
):
    edit_analysis(
        db_session,
        source,
        lambda data: data['fields'].append(
            {
                'name': 'total',
                'value': '$42.50',
                'confidence': 'high',
                'evidence': [{'page': 1, 'excerpt': '$42.50'}],
            }
        ),
    )
    suggestion = service(db_session, test_user).draft(source.id)
    assert any('freight' in warning and '42.50' in warning for warning in suggestion.warnings)
    response = propose(client, auth_headers, 'draft_purchase_order', import_input(source, vendor, test_part))
    assert response.status_code == 200, response.text
    assert any('freight' in warning and '37.50' in warning for warning in response.json()['preview']['warnings'])


@pytest.mark.parametrize('field,value', [('unit_price', '99'), ('quantity', '31'), ('part_number', 'INVENTED')])
def test_retained_line_value_must_be_supported_by_its_cited_evidence(db_session, test_user, source, field, value):
    edit_analysis(db_session, source, lambda data: data['lines'][0].update({field: value}))
    suggestion = service(db_session, test_user).draft(source.id)
    key = {'unit_price': 'unit_price_amount', 'quantity': 'quantity_ordered', 'part_number': 'part_id'}[field]
    assert getattr(suggestion.lines[0], key) is None


def test_import_permission_is_rechecked_at_execution(
    client, auth_headers, db_session, test_user, source, vendor, test_part
):
    response = propose(client, auth_headers, 'draft_purchase_order', import_input(source, vendor, test_part))
    assert response.status_code == 200, response.text
    db_session.add(
        RolePermission(company_id=1, role=test_user.role, permissions=['purchasing:view', 'purchasing:create'])
    )
    db_session.commit()
    assert execute(client, auth_headers, response.json()).status_code == 403
    assert db_session.query(PurchaseOrder).count() == 0


def test_two_reviewed_tasks_cannot_import_same_source_twice(
    client, auth_headers, db_session, source, vendor, test_part
):
    first = propose(client, auth_headers, 'draft_purchase_order', import_input(source, vendor, test_part)).json()
    second = propose(
        client,
        auth_headers,
        'draft_purchase_order',
        import_input(source, vendor, test_part, po_number='ANOTHER-NUMBER'),
    ).json()
    assert execute(client, auth_headers, first).status_code == 200
    assert execute(client, auth_headers, second).status_code == 409
    assert db_session.query(PurchaseOrder).count() == 1


def test_conflicting_po_identifiers_are_not_replaced_by_document_number(db_session, test_user, source):
    def mutate(analysis):
        analysis['fields'].extend(
            [
                {
                    'name': 'po_number',
                    'value': 'ANOTHER-PO',
                    'confidence': 'high',
                    'evidence': [{'page': 1, 'excerpt': 'ANOTHER-PO'}],
                },
                {
                    'name': 'document_number',
                    'value': 'FALLBACK-PO',
                    'confidence': 'high',
                    'evidence': [{'page': 1, 'excerpt': 'FALLBACK-PO'}],
                },
            ]
        )

    edit_analysis(db_session, source, mutate)
    assert service(db_session, test_user).draft(source.id).po_number is None


@pytest.mark.parametrize('name', ['po_number', 'vendor_name'])
def test_header_prefill_rejects_identifier_substrings(db_session, test_user, source, name):
    def mutate(analysis):
        field = next(field for field in analysis['fields'] if field['name'] == name)
        field['evidence'] = [{'page': 1, 'excerpt': field['value'] + 'EXTRA'}]

    edit_analysis(db_session, source, mutate)
    result = service(db_session, test_user).draft(source.id)
    assert getattr(result, 'po_number' if name == 'po_number' else 'vendor_id') is None


@pytest.mark.parametrize('value', ['2026-09-23T00:00:00', '2026-09-23T00:00:00.000000'])
def test_native_excel_midnight_date_is_unambiguous(value):
    assert _date(value) == date(2026, 9, 23)


@pytest.mark.parametrize(
    'value',
    [
        '2026-09-23T01:00:00',
        '2026-09-23T00:00:00Z',
        '2026-09-23T00:00:00+00:00',
        '2026-09-23T00:00:00.000001',
        '2026-02-30T00:00:00',
        '2026-09-23T00:00',
    ],
)
def test_ambiguous_or_invalid_native_datetime_is_not_coerced(value):
    assert _date(value) is None


def test_manual_po_execution_locks_number_before_vendor_and_part_rows(
    client, auth_headers, vendor, test_part, monkeypatch
):
    from app.services import hank_task_service as tasks

    task = propose(
        client,
        auth_headers,
        'draft_purchase_order',
        {
            'vendor_id': vendor.id,
            'lines': [{'part_id': test_part.id, 'quantity_ordered': 1, 'unit_price': 2}],
        },
    ).json()
    calls = []
    original = tasks.HankTaskService._rows

    def rows(self, model, predicate, *, locked=False):
        if locked and model in (Vendor, Part):
            assert 'po_number' in calls
            calls.append(model.__name__)
        return original(self, model, predicate, locked=locked)

    monkeypatch.setattr(tasks, 'acquire_generator_lock', lambda db, name, company: calls.append(name))
    monkeypatch.setattr(tasks.HankTaskService, '_rows', rows)
    result = execute(client, auth_headers, task)
    assert result.status_code == 200, result.text
    assert calls.index('po_number') < calls.index('Vendor') < calls.index('Part')
