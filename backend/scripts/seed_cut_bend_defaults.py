"""Thin CLI wrapper — seeds live in estimate_workbench_service."""

from __future__ import annotations

import argparse

from app.db.database import SessionLocal
from app.models.company import Company
from app.services.estimate_workbench_service import seed_cut_bend_defaults


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed Cut/Bend default tables")
    parser.add_argument("--company-id", type=int, default=None)
    parser.add_argument("--force", action="store_true", help="Replace existing tables")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.company_id is not None:
            companies = db.query(Company).filter(Company.id == args.company_id).all()
        else:
            companies = db.query(Company).filter(Company.is_active.is_(True)).all()
        if not companies:
            print("No companies found")
            return
        for company in companies:
            n = seed_cut_bend_defaults(db, company.id, force=args.force)
            db.commit()
            print(f"company {company.id}: seeded {n} cut/bend tables")
    finally:
        db.close()


if __name__ == "__main__":
    main()
