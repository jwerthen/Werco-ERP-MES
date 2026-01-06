#!/usr/bin/env python3
"""
Automated database backup script for Werco ERP.
Supports local backups and S3 uploads.
"""
import os
import sys
import subprocess
import shutil
from datetime import datetime
from pathlib import Path
import logging

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from app.core.config import settings

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class DatabaseBackup:
    """Handle database backups with compression and S3 upload."""

    def __init__(self):
        self.backup_dir = Path(os.path.join(os.path.dirname(__file__), '..', 'backups', 'database'))
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.retention_days = 30

    def parse_db_url(self):
        """Parse database URL to get connection details."""
        db_url = settings.DATABASE_URL
        if db_url.startswith('postgresql://'):
            # Parse: postgresql://user:password@host:port/dbname
            url_parts = db_url.replace('postgresql://', '').split('@')
            credentials = url_parts[0].split(':')
            host_port_db = url_parts[1].split('/')
            host_port = host_port_db[0].split(':')

            user = credentials[0]
            password = credentials[1]
            host = host_port[0]
            port = host_port[1] if len(host_port) > 1 else '5432'
            dbname = host_port_db[1]

            return {
                'user': user,
                'password': password,
                'host': host,
                'port': port,
                'dbname': dbname
            }
        return None

    def create_backup(self):
        """Create database backup."""
        logger.info("Starting database backup...")

        # Get database connection details
        db_config = self.parse_db_url()
        if not db_config:
            logger.error("Failed to parse database URL")
            return False

        # Create backup filename
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_file = self.backup_dir / f"werco_erp_backup_{timestamp}.sql"
        compressed_file = self.backup_dir / f"werco_erp_backup_{timestamp}.sql.gz"

        try:
            # Execute pg_dump
            logger.info(f"Creating backup: {backup_file}")
            env = os.environ.copy()
            env['PGPASSWORD'] = db_config['password']

            cmd = [
                'pg_dump',
                '-h', db_config['host'],
                '-p', db_config['port'],
                '-U', db_config['user'],
                '-d', db_config['dbname'],
                '--no-owner',
                '--no-acl',
                '-f', str(backup_file)
            ]

            result = subprocess.run(cmd, env=env, capture_output=True, text=True)

            if result.returncode != 0:
                logger.error(f"pg_dump failed: {result.stderr}")
                return False

            # Compress backup
            logger.info("Compressing backup...")
            with open(backup_file, 'rb') as f_in:
                with open(compressed_file, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)

            # Remove uncompressed file
            backup_file.unlink()

            logger.info(f"Backup created successfully: {compressed_file}")
            file_size = compressed_file.stat().st_size / (1024 * 1024)  # MB
            logger.info(f"Backup size: {file_size:.2f} MB")

            # Upload to S3 (if configured)
            if settings.AWS_S3_BUCKET:
                self.upload_to_s3(compressed_file)

            # Clean old backups
            self.cleanup_old_backups()

            return True

        except Exception as e:
            logger.error(f"Backup failed: {e}")
            return False

    def upload_to_s3(self, file_path: Path):
        """Upload backup to S3."""
        if not settings.AWS_ACCESS_KEY_ID or not settings.AWS_SECRET_ACCESS_KEY:
            logger.info("S3 credentials not configured, skipping S3 upload")
            return

        try:
            import boto3
            from botocore.exceptions import ClientError

            logger.info(f"Uploading backup to S3: {file_path.name}")

            s3 = boto3.client(
                's3',
                aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                region_name=settings.AWS_REGION
            )

            s3_path = f"database-backups/{file_path.name}"
            s3.upload_file(str(file_path), settings.S3_BUCKET_NAME, s3_path)

            logger.info(f"Successfully uploaded to S3: s3://{settings.S3_BUCKET_NAME}/{s3_path}")

        except ImportError:
            logger.error("boto3 not installed, cannot upload to S3")
        except ClientError as e:
            logger.error(f"S3 upload failed: {e}")
        except Exception as e:
            logger.error(f"Error uploading to S3: {e}")

    def cleanup_old_backups(self):
        """Remove backups older than retention period."""
        logger.info("Cleaning up old backups...")

        cutoff_date = datetime.now().timestamp() - (self.retention_days * 24 * 60 * 60)
        deleted_count = 0

        for backup_file in self.backup_dir.glob('werco_erp_backup_*.sql.gz'):
            if backup_file.stat().st_mtime < cutoff_date:
                backup_file.unlink()
                deleted_count += 1
                logger.info(f"Deleted old backup: {backup_file.name}")

        if deleted_count > 0:
            logger.info(f"Cleaned up {deleted_count} old backup(s)")
        else:
            logger.info("No old backups to clean up")


def main():
    """Main backup execution."""
    logger.info("=" * 60)
    logger.info("Werco ERP Database Backup")
    logger.info("=" * 60)

    backup = DatabaseBackup()
    success = backup.create_backup()

    if success:
        logger.info("Backup completed successfully!")
        return 0
    else:
        logger.error("Backup failed!")
        return 1


if __name__ == "__main__":
    sys.exit(main())
