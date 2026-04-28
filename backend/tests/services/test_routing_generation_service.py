from app.models.company import Company
from app.models.quote_config import QuoteSettings
from app.models.work_center import WorkCenter
from app.services.routing_generation_service import (
    generate_draft_routing,
    map_operations_to_work_centers,
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
