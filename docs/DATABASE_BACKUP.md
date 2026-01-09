# Werco ERP - Database Backup & Restore Guide

## Overview

This guide covers database backup and restore procedures for the Werco ERP system running on PostgreSQL (Railway or local).

## Quick Start

### Create a Backup
```powershell
cd C:\Users\jmw\Desktop\Werco-ERP\scripts
.\db-backup.ps1
```

### Restore from Backup
```powershell
cd C:\Users\jmw\Desktop\Werco-ERP\scripts
.\db-restore.ps1 -BackupFile "..\backups\database\werco_erp_backup_20260109_120000.sql.gz"
```

### List Available Backups
```powershell
.\db-backup-utils.ps1 list
```

---

## Prerequisites

### Required Tools
- **PostgreSQL Client Tools** (pg_dump, psql)
  - Download: https://www.postgresql.org/download/windows/
  - Or install via: `winget install PostgreSQL.PostgreSQL`
  - Ensure `pg_dump` and `psql` are in your PATH

### Database Connection
The scripts will automatically find your DATABASE_URL from (in order):
1. Command line parameter `-DatabaseUrl`
2. Environment variable `$env:DATABASE_URL`
3. Railway CLI (`railway variables get DATABASE_URL`)
4. `.env` file in project root
5. `backend/.env` file

---

## Backup Scripts

### db-backup.ps1

Creates a compressed PostgreSQL backup.

**Usage:**
```powershell
# Basic backup (auto-detects database URL)
.\db-backup.ps1

# Specify database URL explicitly
.\db-backup.ps1 -DatabaseUrl "postgresql://user:pass@host:5432/dbname"

# Custom output directory
.\db-backup.ps1 -OutputDir "D:\backups"

# Change retention period (default: 30 days)
.\db-backup.ps1 -RetentionDays 7
```

**Output:**
- Backup file: `backups/database/werco_erp_backup_YYYYMMDD_HHMMSS.sql.gz`
- Automatically removes backups older than retention period

**What's Backed Up:**
- All tables and data
- Indexes
- Sequences
- Constraints
- Views

**What's NOT Backed Up:**
- Owner/ACL information (for portability)
- User accounts (PostgreSQL roles)

---

### db-restore.ps1

Restores a database from a backup file.

**Usage:**
```powershell
# Restore from backup (prompts for confirmation)
.\db-restore.ps1 -BackupFile "..\backups\database\werco_erp_backup_20260109_120000.sql.gz"

# Skip confirmation prompt
.\db-restore.ps1 -BackupFile "backup.sql.gz" -Force

# Specify database URL
.\db-restore.ps1 -BackupFile "backup.sql.gz" -DatabaseUrl "postgresql://..."
```

**WARNING:** Restore will:
- DROP existing tables
- REPLACE all data
- Require typing "RESTORE" to confirm (unless -Force is used)

**After Restore:**
- Restart the application to reconnect to the database
- Verify data integrity through the UI

---

### db-backup-utils.ps1

Utility functions for managing backups.

**List Backups:**
```powershell
.\db-backup-utils.ps1 list
```

**Verify Backup Integrity:**
```powershell
# Verify specific backup
.\db-backup-utils.ps1 verify -BackupFile "..\backups\database\backup.sql.gz"

# Verify most recent backup
.\db-backup-utils.ps1 verify
```

**Schedule Automatic Backups (Windows Task Scheduler):**
```powershell
# Create daily backup (every 24 hours)
.\db-backup-utils.ps1 schedule -IntervalHours 24

# Create backup every 6 hours
.\db-backup-utils.ps1 schedule -IntervalHours 6

# Remove scheduled task
.\db-backup-utils.ps1 unschedule
```

---

## Backup Strategy Recommendations

### Production Environment

1. **Automated Daily Backups**
   ```powershell
   .\db-backup-utils.ps1 schedule -IntervalHours 24
   ```

2. **Retention Policy**
   - Keep 30 days of backups locally
   - Archive monthly backups for 1 year (manual or S3)

3. **Off-site Storage**
   - Copy critical backups to S3 or another cloud storage
   - The Python script `backup_database.py` supports S3 upload

### Before Major Changes

Always create a backup before:
- Database migrations
- Major application updates
- Data imports
- Bulk operations

```powershell
.\db-backup.ps1
```

### Testing Restores

Periodically test your restore procedure:
1. Create a backup
2. Set up a test database
3. Restore to the test database
4. Verify data integrity

---

## Railway-Specific Notes

### Getting Database URL from Railway
```powershell
# Link to your Railway project first
railway link

# Get the DATABASE_URL
railway variables get DATABASE_URL
```

### Backing Up Railway Production Database
```powershell
# Set the Railway DATABASE_URL temporarily
$env:DATABASE_URL = "postgresql://..."
.\db-backup.ps1
```

### Railway Built-in Backups
Railway PostgreSQL also has built-in backup features:
- Point-in-time recovery (PITR)
- Automatic daily backups
- Check Railway dashboard for details

---

## Troubleshooting

### "pg_dump not found"
Install PostgreSQL client tools and add to PATH:
```powershell
winget install PostgreSQL.PostgreSQL
# Restart PowerShell after installation
```

### "Could not find DATABASE_URL"
Set the environment variable:
```powershell
$env:DATABASE_URL = "postgresql://user:password@host:5432/database"
.\db-backup.ps1
```

### "Connection refused"
- Verify the database host and port
- Check if the database is running
- Verify firewall/network access

### Backup file is empty or very small
- Check database connection
- Verify you have SELECT permissions
- Run pg_dump manually with verbose output:
  ```bash
  pg_dump -h host -U user -d database -v
  ```

### Restore fails with "permission denied"
- Verify you have write permissions on the database
- Try running PowerShell as Administrator

---

## File Locations

| Item | Location |
|------|----------|
| Backup scripts | `scripts/db-backup.ps1`, `db-restore.ps1`, `db-backup-utils.ps1` |
| Backup files | `backups/database/werco_erp_backup_*.sql.gz` |
| Python backup script | `scripts/backup_database.py` (with S3 support) |
| This documentation | `docs/DATABASE_BACKUP.md` |

---

## Emergency Recovery

If you need to restore from backup urgently:

1. **Stop the application** (to prevent further data changes)

2. **Identify the backup to restore**
   ```powershell
   .\db-backup-utils.ps1 list
   ```

3. **Verify backup integrity**
   ```powershell
   .\db-backup-utils.ps1 verify -BackupFile "path\to\backup.sql.gz"
   ```

4. **Restore the database**
   ```powershell
   .\db-restore.ps1 -BackupFile "path\to\backup.sql.gz" -Force
   ```

5. **Restart the application**

6. **Verify data through the UI**

---

## Contact

For database emergencies or questions about backups, contact the IT administrator.
