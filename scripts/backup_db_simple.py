#!/usr/bin/env python3
"""
Simplified database backup script for Docker PostgreSQL.
"""
import subprocess
import sys
from datetime import datetime
import os

# Backup directory
BACKUP_DIR = "/app/backups/database"
os.makedirs(BACKUP_DIR, exist_ok=True)

# Generate timestamp
timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
backup_file = f"{BACKUP_DIR}/werco_erp_backup_{timestamp}.sql"
compressed_file = f"{BACKUP_DIR}/werco_erp_backup_{timestamp}.sql.gz"

print(f"Starting database backup...")

# Database configuration from environment
db_host = os.getenv('DB_HOST', 'db')
db_user = os.getenv('POSTGRES_USER', 'werco_user')
db_name = os.getenv('POSTGRES_DB', 'werco_erp')

try:
    # Run pg_dump
    cmd = [
        'pg_dump',
        '-h', db_host,
        '-U', db_user,
        '-d', db_name,
        '--no-owner',
        '--no-acl',
        '-f', backup_file
    ]

    print(f"Executing: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"ERROR: pg_dump failed")
        print(f"STDERR: {result.stderr}")
        sys.exit(1)

    print(f"Backup created: {backup_file}")

    # Compress
    print("Compressing backup...")
    subprocess.run(['gzip', backup_file], check=True)

    file_size = os.path.getsize(compressed_file) / (1024 * 1024)
    print(f"✓ Backup successful: {compressed_file}")
    print(f"  Size: {file_size:.2f} MB")

    sys.exit(0)

except Exception as e:
    print(f"ERROR: Backup failed: {e}")
    sys.exit(1)
