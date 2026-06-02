#!/usr/bin/env python3
"""
Simplified database backup script for the configured Supabase Postgres database.
"""

import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

# Backup directory
BACKUP_DIR = "/app/backups/database"
os.makedirs(BACKUP_DIR, exist_ok=True)

# Generate timestamp
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
backup_file = f"{BACKUP_DIR}/werco_erp_backup_{timestamp}.sql"
compressed_file = f"{BACKUP_DIR}/werco_erp_backup_{timestamp}.sql.gz"

print(f"Starting database backup...")

database_url = os.getenv("DATABASE_URL")
if not database_url:
    print("ERROR: DATABASE_URL must be set to the Supabase Postgres connection string")
    sys.exit(1)

parsed = urlparse(database_url.replace("postgresql+psycopg2://", "postgresql://", 1))
db_host = parsed.hostname
db_user = unquote(parsed.username or "")
db_password = unquote(parsed.password or "")
db_port = str(parsed.port or 5432)
db_name = unquote(parsed.path.lstrip("/"))

try:
    # Run pg_dump
    cmd = [
        "pg_dump",
        "-h",
        db_host,
        "-p",
        db_port,
        "-U",
        db_user,
        "-d",
        db_name,
        "--no-owner",
        "--no-acl",
        "-f",
        backup_file,
    ]

    print(f"Executing: {' '.join(cmd)}")
    pgpass_file = Path(tempfile.mktemp(prefix=".pgpass_"))
    try:
        pgpass_file.write_text(f"{db_host}:{db_port}:{db_name}:{db_user}:{db_password}\n")
        pgpass_file.chmod(0o600)
        env = os.environ.copy()
        env["PGPASSFILE"] = str(pgpass_file)
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    finally:
        if pgpass_file.exists():
            pgpass_file.unlink()

    if result.returncode != 0:
        print(f"ERROR: pg_dump failed")
        print(f"STDERR: {result.stderr}")
        sys.exit(1)

    print(f"Backup created: {backup_file}")

    # Compress
    print("Compressing backup...")
    subprocess.run(["gzip", backup_file], check=True)

    file_size = os.path.getsize(compressed_file) / (1024 * 1024)
    print(f"✓ Backup successful: {compressed_file}")
    print(f"  Size: {file_size:.2f} MB")

    sys.exit(0)

except Exception as e:
    print(f"ERROR: Backup failed: {e}")
    sys.exit(1)
