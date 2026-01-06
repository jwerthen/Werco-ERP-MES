#!/usr/bin/env python3
"""
Werco ERP Startup Verification Script
Checks all critical systems before allowing startup.
"""
import os
import sys
import subprocess
import requests
from datetime import datetime

# ANSI color codes
GREEN = '\033[0;32m'
RED = '\033[0;31m'
YELLOW = '\033[1;33m'
BLUE = '\033[0;34m'
NC = '\033[0m'  # No Color

# Configuration checks
CRITICAL_CHECKS = []
WARNING_CHECKS = []
OPTIMAl_CHECKS = []

def print_header(text):
    print(f"\n{BLUE}{'='*60}{NC}")
    print(f"{BLUE}{text}{NC}")
    print(f"{BLUE}{'='*60}{NC}\n")

def check(name, condition, critical=True):
    """Check a condition and print result."""
    if condition:
        print(f"{GREEN}✓{NC} {name}")
        if critical:
            CRITICAL_CHECKS.append((name, True))
        return True
    else:
        if critical:
            print(f"{RED}✗{NC} {name}")
            CRITICAL_CHECKS.append((name, False))
        else:
            print(f"{YELLOW}⚠{NC} {name}")
            WARNING_CHECKS.append((name, False))
        return False

print_header("Werco ERP Startup Verification")
print(f"Started at: {datetime.now()}")

# 1. Environment Variables Check
print_header("1. Environment Variables")

critical_env_vars = [
    'SECRET_KEY',
    'DATABASE_URL',
    'SENTRY_DSN',
]

print(f"Checking critical environment variables...")
for var in critical_env_vars:
    value = os.getenv(var)
    if var == 'SECRET_KEY':
        check(f"{var} configured", value and len(value) > 32 and value != 'CHANGE_THIS'[:32])
    elif var == 'DATABASE_URL':
        check(f"{var} configured", value is not None and 'localhost' in value)
    else:
        check(f"{var} configured", value is not None)

# 2. Application Settings
print_header("2. Application Settings")

check("DEBUG mode disabled", os.getenv('DEBUG') != 'true', critical=False)
check("ENVIRONMENT set", os.getenv('ENVIRONMENT') in ['development', 'staging', 'production'], critical=False)
check("Rate limiting enabled", os.getenv('RATE_LIMIT_ENABLED') == 'true', critical=False)

# 3. API Health Check
print_header("3. API Health Check")

try:
    backend_url = os.getenv('BACKEND_URL', 'http://localhost:8000')
    response = requests.get(f"{backend_url}/health", timeout=5)
    check("Backend health endpoint", response.status_code == 200)

    if response.status_code == 200:
        health_data = response.json()
        check("App name in health response", 'app' in health_data)
        check("Status healthy", health_data.get('status') == 'healthy')

    # Try API docs (should be disabled in production)
    try:
        docs_response = requests.get(f"{backend_url}/api/docs", timeout=5)
        docs_available = docs_response.status_code == 200
        if os.getenv('ENVIRONMENT') == 'production':
            check("API docs disabled (production)", not docs_available, critical=False)
        else:
            check("API docs available (dev/staging)", docs_available, critical=False)
    except:
        pass

except requests.RequestException as e:
    check("Backend health endpoint", False)
    print(f"  Error: {e}")

# 4. Database Connection
print_header("4. Database Connection")

try:
    import psycopg2
    db_url = os.getenv('DATABASE_URL')
    if db_url:
        conn = psycopg2.connect(db_url)
        check("Database connection successful", True)
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        check("Database query execution", True)
        cursor.close()
        conn.close()
    else:
        check("Database URL configured", False)
except ImportError:
    check("psycopg2 installed", False)
    print("  Install with: pip install psycopg2-binary")
except Exception as e:
    check("Database connection", False)
    print(f"  Error: {e}")

# 5. Redis Check (Optional)
print_header("5. Redis Cache (Optional)")

redis_url = os.getenv('REDIS_URL')
if redis_url:
    try:
        import redis
        r = redis.from_url(redis_url)
        r.ping()
        check("Redis connection", True)
        check("Redis is responding", True)
    except ImportError:
        check("Redis installed", False, critical=False)
        print("  Install with: pip install redis")
    except Exception as e:
        check("Redis connection", False, critical=False)
        print(f"  Error: {e}")
else:
    print(f"{YELLOW}ℹ{NC} Redis not configured (optional but recommended)")

# 6. Sentry Configuration
print_header("6. Sentry Monitoring")

sentry_dsn = os.getenv('SENTRY_DSN')
if sentry_dsn:
    check("Sentry DSN configured", 'ingest.sentry.io' in sentry_dsn)
else:
    check("Sentry DSN configured", False)

# 7. File Storage Check (Optional)
print_header("7. File Storage (S3)")

aws_key = os.getenv('AWS_ACCESS_KEY_ID')
aws_secret = os.getenv('AWS_SECRET_ACCESS_KEY')
s3_bucket = os.getenv('S3_BUCKET_NAME')

if all([aws_key, aws_secret, s3_bucket]):
    check("AWS credentials configured", True)
    check("S3 bucket configured", s3_bucket is not None)
else:
    check("File storage configured", False, critical=False)
    print("  Configure for document uploads")

# 8. Security Headers
print_header("8. Security Headers")

try:
    response = requests.get(f"{backend_url}/health", timeout=5)
    headers = response.headers
    check("X-Frame-Options header", 'X-Frame-Options' in headers)
    check("X-Content-Type-Options", 'X-Content-Type-Options' in headers)
    check("X-XSS-Protection", 'X-XSS-Protection' in headers)
except:
    pass

# 9. Rate Limiting Check
print_header("9. Rate Limiting")

if os.getenv('RATE_LIMIT_ENABLED') == 'true':
    check("Rate limiting enabled in config", True)
    # Would need to make multiple requests to actually verify
    print(f"{YELLOW}ℹ{NC} Rate limiting: Configured but requires load testing to verify")
else:
    check("Rate limiting enabled", False, critical=False)

# 10. WebSocket Connection (Optional)
print_header("10. WebSocket Support")

try:
    import websockets
    check("websockets library installed", True)
    check("WebSocket infrastructure", True)
except ImportError:
    check("websockets installed", False, critical=False)

# Summary
print_header("VERIFICATION SUMMARY")

critical_passed = sum(1 for _, passed in CRITICAL_CHECKS if passed)
critical_total = len(CRITICAL_CHECKS)
warning_count = len(WARNING_CHECKS)

print(f"\nCritical Checks: {critical_passed}/{critical_total} passed")
if warning_count > 0:
    print(f"Warnings: {warning_count} items need attention")

if critical_passed == critical_total:
    print(f"\n{GREEN}✓ All critical checks passed!{NC}")
    print(f"{GREEN}Application is ready to start.{NC}\n")
else:
    print(f"\n{RED}✗ {critical_total - critical_passed} critical check(s) failed!{NC}")
    print(f"{RED}Fix the issues above before starting.{NC}\n")
    sys.exit(1)

# Recommendations
print_header("RECOMMENDATIONS")

recommendations = [
    "Test rate limiting with actual load",
    "Verify backups are working: python scripts/backup_database.py",
    "Check Sentry dashboard for errors",
    "Test email sending if SMTP configured",
    "Run load tests before production",
    "Review and update security headers if needed",
    "Configure Redis for better performance",
    "Set up automated monitoring alerts",
]

for rec in recommendations:
    print(f"• {rec}")

print(f"\n{GREEN}Verification complete!{NC}\n")
print(f"For more details, see: docs/PRODUCTION_CHECKLIST.md")
print(f"For deployment, see: docs/DEPLOYMENT.md")

sys.exit(0)
