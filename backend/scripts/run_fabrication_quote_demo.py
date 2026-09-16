"""Run the replacement estimator against a new disposable local demo database.

    .venv/bin/python scripts/run_fabrication_quote_demo.py --port 8766

Only synthetic data is seeded. The working directory is changed before ERP
settings load, so the repository's .env and production database are never used.
"""

import argparse
import json
import os
import secrets
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path


def demo_plan():
    evidence = {
        "reviewed": True,
        "source": "Synthetic development fixture — not Werco shop rates",
        "status": "assumption",
        "note": "Demonstrates cost accounting only; do not send this example to a customer.",
    }

    def operation(ident, part_id, name, recipe, setup, labor_rate, machine_rate, consumables="0", outside="0"):
        return {
            "id": ident,
            "part_id": part_id,
            "name": name,
            "process": recipe["kind"],
            "setup_basis": "per_quote",
            "run_basis": "per_unit",
            "batch_size": "1",
            "setup_labor_seconds": setup,
            "setup_machine_seconds": setup,
            "labor_rate_per_hour": labor_rate,
            "machine_rate_per_hour": machine_rate,
            "consumables_cost_per_run": consumables,
            "outside_cost_per_run": outside,
            "recipe": recipe,
            "evidence": evidence,
        }

    return {
        "schema_version": "1",
        "currency": "USD",
        "target_margin": "0.25",
        "parts": [
            {
                "id": "assembly",
                "name": "Example enclosure assembly",
                "make_or_buy": "make",
                "costing_complete": True,
                "evidence": evidence,
            },
            {
                "id": "bracket",
                "name": "Example formed bracket",
                "make_or_buy": "make",
                "costing_complete": True,
                "evidence": evidence,
            },
        ],
        "roots": [{"part_id": "assembly", "quantity": "10"}],
        "bom": [{"id": "brackets", "parent_id": "assembly", "child_id": "bracket", "quantity": "4"}],
        "materials": [
            {
                "id": "sheet",
                "part_id": "bracket",
                "description": "Synthetic reviewed material allocation",
                "quantity_basis": "per_unit",
                "consumed_quantity": "0.25",
                "unit": "kg",
                "unit_cost": "5",
                "evidence": evidence,
            }
        ],
        "operations": [
            operation(
                "laser",
                "bracket",
                "Laser cutting",
                {
                    "kind": "laser",
                    "cuts": [
                        {"cut_length_mm": "240", "speed_mm_per_second": "20", "pierces": 2, "pierce_seconds": "0.3"}
                    ],
                    "noncut_machine_seconds": "2",
                    "labor_seconds": "15",
                    "speed_includes_dynamics": True,
                },
                "900",
                "40",
                "60",
            ),
            operation(
                "brake",
                "bracket",
                "Forming — two reviewed hits",
                {
                    "kind": "brake",
                    "hits": 2,
                    "seconds_per_hit": "10",
                    "handling_seconds": "10",
                    "inspection_seconds": "5",
                    "crew_size": 1,
                    "machine_seconds": "30",
                    "feasibility_reviewed": True,
                },
                "600",
                "40",
                "60",
            ),
            operation(
                "weld",
                "assembly",
                "Fit, tack and MIG weld",
                {
                    "kind": "weld",
                    "process": "MIG",
                    "weld_length_mm": "800",
                    "weld_size_mm": "3",
                    "travel_speed_mm_per_second": "5",
                    "nonweld_labor_seconds": "1200",
                    "nonweld_machine_seconds": "1200",
                    "crew_size": 1,
                    "procedure_reference": "Synthetic example only",
                },
                "1200",
                "40",
                "20",
                "2",
            ),
            operation(
                "finish",
                "assembly",
                "Finish, assemble, inspect and pack",
                {"kind": "manual", "labor_seconds": "600", "machine_seconds": "0"},
                "300",
                "40",
                "0",
                "1",
                "5",
            ),
        ],
        "hardware": [
            {
                "id": "fasteners",
                "part_id": "assembly",
                "manufacturer": "Example Hardware",
                "mpn": "DEMO-M6-010",
                "quantity_per_part": "6",
                "stock_available": 0,
                "evidence": evidence,
                "offer": {
                    "id": "demo-offer",
                    "manufacturer": "Example Hardware",
                    "mpn": "DEMO-M6-010",
                    "supplier": "Synthetic supplier",
                    "currency": "USD",
                    "price_unit_quantity": "100",
                    "price_breaks": [{"minimum_quantity": "0", "price": "30"}],
                    "pack_quantity": 100,
                    "minimum_order_quantity": 1,
                    "order_multiple": 1,
                    "quoted_on": date.today().isoformat(),
                    "valid_until": (date.today() + timedelta(days=30)).isoformat(),
                    "max_age_days": 30,
                    "applicable": True,
                    "freight": "0",
                    "evidence": evidence,
                },
            }
        ],
        "assumptions": [
            {
                "id": "demo",
                "description": "All prices and process times in this example are synthetic. This is not a production quotation.",
                "reviewed": True,
                "source": "Local development fixture",
            }
        ],
        "source_reviews": [],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    backend = Path(__file__).resolve().parents[1]
    directory = Path(tempfile.mkdtemp(prefix="werco-fabrication-demo-"))
    os.chdir(directory)
    sys.path.insert(0, str(backend))
    os.environ.update(
        DATABASE_URL="sqlite:///" + str(directory / "demo.sqlite"),
        ENVIRONMENT="test",
        SECRET_KEY=secrets.token_urlsafe(48),
        REFRESH_TOKEN_SECRET_KEY=secrets.token_urlsafe(48),
        REDIS_URL="",
        SENTRY_DSN="",
        WERCO_MCP_HTTP_ENABLED="false",
        ALLOWED_HOSTS="localhost,127.0.0.1,testserver",
        CORS_ORIGINS="http://127.0.0.1:5179,http://localhost:5179",
    )
    quote_python = backend / ".venv-quoting/bin/python"
    if quote_python.exists():
        os.environ["WERCO_QUOTE_WORKER_PYTHON"] = str(quote_python)
    from app.core.security import get_password_hash
    from app.db.database import Base, SessionLocal, engine
    from app.fabrication_quote.schemas_api import CreateQuote
    from app.main import app
    from app.models.company import Company
    from app.models.customer import Customer
    from app.models.user import User, UserRole
    from app.services.fabrication_quote_service import create

    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        company = Company(name="Werco · local demo", slug="werco-local-demo")
        db.add(company)
        db.flush()
        user = User(
            company_id=company.id,
            email="demo@example.com",
            employee_id="DEMO-001",
            first_name="Demo",
            last_name="Estimator",
            hashed_password=get_password_hash("Local-quote-demo-2026!"),
            role=UserRole.ADMIN,
            is_active=True,
            is_superuser=False,
        )
        customer = Customer(company_id=company.id, name="Example customer — synthetic", code="DEMO")
        db.add_all([user, customer])
        db.flush()
        value = CreateQuote(title="Example enclosure · synthetic rates", customer_id=customer.id, plan=demo_plan())
        result = create(db, company.id, user, value)
        db.commit()
    (directory / "example-plan.json").write_text(json.dumps(demo_plan(), indent=2))
    print(f"Local demo API: http://127.0.0.1:{args.port}/api/v1", flush=True)
    print("Sign in: demo@example.com / Local-quote-demo-2026!", flush=True)
    print(f"Synthetic estimate ID: {result['id']}; temporary data: {directory}", flush=True)
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
