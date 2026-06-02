from app.models.company import Company
from app.models.quote_config import QuoteSettings
from app.models.part import Part
from app.models.routing import Routing
from app.models.routing_learning import RoutingLearnedAlias, RoutingOperationPattern, RoutingWorkCenterPreference
from app.models.work_center import WorkCenter
from app.services.routing_generation_service import (
    generate_draft_routing,
    map_operations_to_work_centers,
)
from app.services.routing_learning_service import (
    create_generation_session,
    get_learned_routing_context,
    learn_from_approved_generation,
)
from app.services.work_center_type_service import get_work_center_types


def test_map_operations_uses_configured_work_center_aliases():
    operations = [
        {"sequence": 10, "operation_name": "Bend part", "work_center_type": "bend"},
        {"sequence": 20, "operation_name": "Weld assembly", "work_center_type": "weld"},
        {"sequence": 30, "operation_name": "Machine slots", "work_center_type": "machine"},
    ]
    work_centers_by_type = {
        "press_brake": [{"id": 1, "name": "Brake", "code": "BRK"}],
        "welding": [{"id": 2, "name": "Weld", "code": "WLD"}],
        "cnc_machining": [{"id": 3, "name": "Mill", "code": "CNC"}],
    }

    mapped, warnings = map_operations_to_work_centers(operations, work_centers_by_type)

    assert warnings == []
    assert [op["work_center_id"] for op in mapped] == [1, 2, 3]
    assert [op["work_center_type"] for op in mapped] == ["press_brake", "welding", "cnc_machining"]


def test_map_operations_uses_learned_aliases_and_preferences():
    operations = [
        {"sequence": 10, "operation_name": "Crease flange", "work_center_type": "crease"},
    ]
    work_centers_by_type = {
        "press_brake": [
            {"id": 1, "name": "General Brake", "code": "BRK-1"},
            {"id": 2, "name": "Preferred Brake", "code": "BRK-2"},
        ],
    }

    mapped, warnings = map_operations_to_work_centers(
        operations,
        work_centers_by_type,
        learned_aliases=[{"alias": "crease", "work_center_type": "press_brake", "usage_count": 3}],
        preferred_work_center_ids={"press_brake": [2]},
    )

    assert warnings == []
    assert mapped[0]["work_center_id"] == 2
    assert mapped[0]["work_center_type"] == "press_brake"


def test_generate_draft_routing_passes_configured_types_to_extractor(monkeypatch):
    captured = {}

    def fake_extract_routing_data_with_llm(**kwargs):
        captured["work_center_types"] = kwargs["work_center_types"]
        return {
            "part_info": {},
            "operations": [
                {
                    "sequence": 10,
                    "operation_name": "Waterjet cut",
                    "work_center_type": "waterjet",
                    "description": "",
                    "confidence": "high",
                }
            ],
            "extraction_confidence": "high",
        }

    monkeypatch.setattr(
        "app.services.routing_generation_service.extract_routing_data_with_llm",
        fake_extract_routing_data_with_llm,
    )

    result = generate_draft_routing(
        drawing_text="Waterjet cut 1/4 aluminum plate.",
        geometry=None,
        work_centers_by_type={
            "waterjet": [{"id": 7, "name": "Waterjet", "code": "WJ"}],
        },
        work_center_types=["laser", "waterjet", "deburr"],
    )

    assert captured["work_center_types"] == ["laser", "waterjet", "deburr"]
    assert result["operations"][0]["work_center_id"] == 7


def test_generate_draft_routing_falls_back_to_current_work_centers(monkeypatch):
    def fake_extract_routing_data_with_llm(**kwargs):
        return {"_error": "API key not configured", "_extraction_metadata": {}}

    monkeypatch.setattr(
        "app.services.routing_generation_service.extract_routing_data_with_llm",
        fake_extract_routing_data_with_llm,
    )

    result = generate_draft_routing(
        drawing_text="Assembly bracket, 0.125 aluminum, cut flat pattern, 4 bends, PEM hardware, weld, powder coat.",
        geometry={"cut_length": 120.0, "bend_count": 4, "hole_count": 12},
        work_centers_by_type={
            "laser": [{"id": 1, "name": "Laser", "code": "LAS"}],
            "press_brake": [{"id": 2, "name": "Brake", "code": "BRK"}],
            "hardware": [{"id": 3, "name": "Hardware", "code": "HW"}],
            "welding": [{"id": 4, "name": "Weld", "code": "WLD"}],
            "powder_coating": [{"id": 5, "name": "Powder", "code": "PC"}],
            "assembly": [{"id": 6, "name": "Assembly", "code": "ASM"}],
            "inspection": [{"id": 7, "name": "Quality", "code": "QC"}],
        },
        work_center_types=["laser", "press_brake", "hardware", "welding", "powder_coating", "assembly", "inspection"],
        part_context="Part ASM-100: Guard Assembly. ERP part type: assembly.",
        is_assembly=True,
    )

    op_types = [op["work_center_type"] for op in result["operations"]]

    assert "_error" not in result
    assert "AI extraction was unavailable" in result["warnings"][0]
    assert {"laser", "press_brake", "hardware", "welding", "powder_coating", "assembly", "inspection"}.issubset(
        set(op_types)
    )
    assert "shipping" not in op_types
    assert all(op["work_center_id"] for op in result["operations"])
    assert any(op.get("work_instructions") for op in result["operations"])


def test_generate_draft_routing_adds_assembly_completion_steps_from_current_types(monkeypatch):
    def fake_extract_routing_data_with_llm(**kwargs):
        return {
            "part_info": {"assembly_required": False},
            "operations": [
                {
                    "sequence": 10,
                    "operation_name": "Laser cut",
                    "work_center_type": "laser",
                    "description": "Cut profile",
                    "confidence": "high",
                }
            ],
            "extraction_confidence": "high",
        }

    monkeypatch.setattr(
        "app.services.routing_generation_service.extract_routing_data_with_llm",
        fake_extract_routing_data_with_llm,
    )

    result = generate_draft_routing(
        drawing_text="Flat guard for assembly.",
        geometry={"cut_length": 80.0},
        work_centers_by_type={
            "laser": [{"id": 1, "name": "Laser", "code": "LAS"}],
            "assembly": [{"id": 2, "name": "Assembly", "code": "ASM"}],
            "inspection": [{"id": 3, "name": "Quality", "code": "QC"}],
        },
        work_center_types=["laser", "assembly", "inspection"],
        is_assembly=True,
    )

    op_types = [op["work_center_type"] for op in result["operations"]]

    assert op_types == ["laser", "assembly", "inspection"]
    assert [op["work_center_id"] for op in result["operations"]] == [1, 2, 3]
    assert result["part_info"]["assembly_required"] is True


def test_work_center_types_are_company_scoped(db_session):
    db_session.add_all(
        [
            Company(id=2, name="Other Manufacturing", slug="other", is_active=True),
            QuoteSettings(
                setting_key="work_center_types",
                setting_value='["laser", "waterjet"]',
                setting_type="json",
                company_id=1,
            ),
            QuoteSettings(
                setting_key="work_center_types",
                setting_value='["plasma"]',
                setting_type="json",
                company_id=2,
            ),
            WorkCenter(
                code="WJ-1",
                name="Waterjet",
                work_center_type="waterjet",
                is_active=True,
                company_id=1,
            ),
            WorkCenter(
                code="PL-1",
                name="Plasma",
                work_center_type="plasma",
                is_active=True,
                company_id=2,
            ),
        ]
    )
    db_session.commit()

    company_one_types = get_work_center_types(db_session, company_id=1)

    assert "waterjet" in company_one_types
    assert "plasma" not in company_one_types


def test_learning_records_approved_edits_for_future_generation(db_session):
    part = Part(
        part_number="LEARN-001",
        name="Learned Bracket",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=1,
    )
    brake = WorkCenter(
        code="BRK-LEARN",
        name="Preferred Brake",
        work_center_type="press_brake",
        is_active=True,
        company_id=1,
    )
    db_session.add_all([part, brake])
    db_session.flush()
    routing = Routing(part_id=part.id, revision="A", status="draft", is_active=True, company_id=1)
    db_session.add(routing)
    db_session.flush()

    session = create_generation_session(
        db_session,
        company_id=1,
        part_id=part.id,
        created_by=None,
        file_name="learn.dxf",
        file_type="dxf",
        file_size=100,
        file_path="uploads/routing_generation/learn.dxf",
        drawing_text="0.125 aluminum bracket with one crease flange.",
        geometry={"cut_length": 40.0, "bend_count": 1, "hole_count": 0},
        drawing_info={"material": "Aluminum 6061", "thickness": "0.125in", "assembly_required": False},
        proposed_operations=[
            {
                "sequence": 10,
                "operation_name": "Crease Flange",
                "work_center_type": "crease",
                "work_center_id": None,
                "work_instructions": "Original suggestion",
            }
        ],
        warnings=[],
        extraction_confidence="medium",
        source_was_ocr=False,
    )

    learn_from_approved_generation(
        db_session,
        generation_session=session,
        approved_operations=[
            {
                "sequence": 10,
                "name": "Form Flange",
                "description": "Use learned brake process",
                "work_center_id": brake.id,
                "setup_hours": 0.2,
                "run_hours_per_unit": 0.05,
                "is_inspection_point": False,
                "is_outside_operation": False,
                "work_instructions": "Check bend direction before running the lot.",
            }
        ],
        part=part,
        routing_id=routing.id,
        approved_by=None,
        company_id=1,
    )
    db_session.commit()

    alias = (
        db_session.query(RoutingLearnedAlias)
        .filter(RoutingLearnedAlias.company_id == 1, RoutingLearnedAlias.alias == "crease")
        .first()
    )
    preference = db_session.query(RoutingWorkCenterPreference).filter_by(company_id=1).first()
    pattern = db_session.query(RoutingOperationPattern).filter_by(company_id=1).first()

    assert session.status == "approved"
    assert session.correction_summary["work_center_changed_count"] == 0
    assert alias.work_center_type == "press_brake"
    assert preference.work_center_id == brake.id
    assert pattern.operations[0]["operation_name"] == "Form Flange"

    learned_context = get_learned_routing_context(
        db_session,
        company_id=1,
        part=part,
        drawing_text="0.125 aluminum bracket with one crease flange.",
        geometry={"cut_length": 40.0, "bend_count": 1, "hole_count": 0},
    )

    assert any(item["alias"] == "crease" for item in learned_context["aliases"])
    assert learned_context["preferred_work_center_ids"]["press_brake"] == [brake.id]
    assert learned_context["patterns"][0]["operations"][0]["work_center_type"] == "press_brake"
