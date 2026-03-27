"""
Reset all data in the database for a fresh go-live.
Run with: python -m scripts.reset_data

WARNING: This will DELETE all data. The database schema (tables) is preserved.
After running this, the first user to register at /register will become the admin.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.db.database import SessionLocal, engine


def reset_database():
    db = SessionLocal()

    try:
        # Get all table names
        result = db.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename != 'alembic_version'"
        ))
        tables = [row[0] for row in result]

        if not tables:
            print("No tables found.")
            return

        print(f"Found {len(tables)} tables to clear:")
        for t in sorted(tables):
            print(f"  - {t}")

        confirm = input("\nThis will DELETE ALL DATA. Type 'RESET' to confirm: ")
        if confirm != "RESET":
            print("Aborted.")
            return

        # Disable FK constraints, truncate all tables, re-enable
        db.execute(text("SET session_replication_role = 'replica'"))
        for table in tables:
            db.execute(text(f'TRUNCATE TABLE "{table}" CASCADE'))
            print(f"  Cleared: {table}")
        db.execute(text("SET session_replication_role = 'origin'"))

        db.commit()
        print(f"\nAll {len(tables)} tables cleared. Database is ready for go-live.")
        print("Visit /register to create your admin account.")

    except Exception as e:
        db.rollback()
        print(f"Error: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    reset_database()
