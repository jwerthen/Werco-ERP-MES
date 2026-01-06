#!/usr/bin/env python3
"""
Quick system verification for Werco ERP.
"""
import os
import sys

GREEN = '\033[0;32m'
RED = '\033[0;31m'
YELLOW = '\033[1;33m'
NC = '\033[0m'

print("\n" + "="*50)
print("Werco ERP Quick System Check")
print("="*50 + "\n")

checks = []

# Check 1: SECRET_KEY
secret_key = os.getenv('SECRET_KEY', '')
checks.append({
    'name': 'SECRET_KEY secure',
    'pass': len(secret_key) > 32 and 'change-this' not in secret_key.lower(),
    'critical': True
})

# Check 2: DEBUG disabled
checks.append({
    'name': 'DEBUG mode disabled',
    'pass': os.getenv('DEBUG') != 'true',
    'critical': True
})

# Check 3: Sentry configured
checks.append({
    'name': 'Sentry configured',
    'pass': 'sentry.io' in os.getenv('SENTRY_DSN', ''),
    'critical': True
})

# Check 4: Database URL
checks.append({
    'name': 'Database configured',
    'pass': 'postgresql://' in os.getenv('DATABASE_URL', ''),
    'critical': True
})

# Check 5: Environment set
checks.append({
    'name': 'Environment configured',
    'pass': os.getenv('ENVIRONMENT') in ['development', 'staging', 'production'],
    'critical': False
})

# Run checks
critical_passed = 0
total_critical = 0

for check in checks:
    if check['pass']:
        print(f"{GREEN}✓{NC} {check['name']}")
        if check['critical']:
            critical_passed += 1
    else:
        if check['critical']:
            print(f"{RED}✗{NC} {check['name']}")
        else:
            print(f"{YELLOW}⚠{NC} {check['name']}")
    
    if check['critical']:
        total_critical += 1

# Summary
print("\n" + "="*50)
critical_count = sum(1 for c in checks if c['critical'])

if critical_passed == critical_count:
    print(f"{GREEN}✓ All critical checks passed!{NC}")
    if os.getenv('ENVIRONMENT') == 'production':
        print(f"{GREEN}✓ Ready for production!{NC}")
    else:
        print(f"{YELLOW}⚠ Running in {os.getenv('ENVIRONMENT')} mode{NC}")
    sys.exit(0)
else:
    print(f"{RED}✗ {critical_count - critical_passed} critical issue(s) found{NC}")
    print(f"{RED}Fix before production deployment{NC}")
    sys.exit(1)
