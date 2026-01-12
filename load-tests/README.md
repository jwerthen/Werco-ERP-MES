# Load Testing with k6

This directory contains load testing scripts using [k6](https://k6.io/) for capacity planning and performance validation.

## Prerequisites

Install k6:
- **Windows**: `winget install k6` or `choco install k6`
- **macOS**: `brew install k6`
- **Linux**: See https://k6.io/docs/get-started/installation/

## Environment Variables

Set these before running tests:

```bash
# Required
export BASE_URL=http://localhost:8000  # API base URL
export TEST_USER_EMAIL=admin@werco.com
export TEST_USER_TOKEN=your-jwt-token  # Or run auth test first

# Optional
export VUS=10           # Virtual users (default varies by test)
export DURATION=30s     # Test duration (default varies by test)
```

## Test Scripts

| Script | Description | Default VUs | Default Duration |
|--------|-------------|-------------|------------------|
| `smoke.js` | Quick sanity check | 1 | 30s |
| `load.js` | Normal load simulation | 50 | 5m |
| `stress.js` | Find breaking point | 100→500 | 10m |
| `spike.js` | Sudden traffic spike | 10→200→10 | 3m |
| `soak.js` | Extended duration test | 30 | 30m |
| `api-endpoints.js` | Test specific endpoints | 10 | 2m |

## Running Tests

### Quick Smoke Test
```bash
k6 run smoke.js
```

### Load Test with Custom Settings
```bash
k6 run --vus 50 --duration 5m load.js
```

### With Environment Variables
```bash
k6 run -e BASE_URL=https://api.werco.com -e TEST_USER_TOKEN=xxx load.js
```

### Generate HTML Report
```bash
k6 run --out json=results.json load.js
# Then use k6 cloud or grafana to visualize
```

## Thresholds

Tests include pass/fail thresholds:
- **http_req_duration**: p(95) < 500ms
- **http_req_failed**: < 1%
- **http_reqs**: > 100/s (varies by test)

## Test Scenarios

### 1. Smoke Test (`smoke.js`)
Minimal load to verify system works. Run before other tests.

### 2. Load Test (`load.js`)
Simulates expected production load:
- 50 concurrent users
- Mix of read/write operations
- 5 minute duration

### 3. Stress Test (`stress.js`)
Gradually increases load to find limits:
- Ramps from 100 to 500 users
- Identifies breaking point
- Shows degradation pattern

### 4. Spike Test (`spike.js`)
Simulates sudden traffic surge:
- Normal load → spike → recovery
- Tests auto-scaling behavior

### 5. Soak Test (`soak.js`)
Extended duration for memory leaks:
- Moderate load for 30+ minutes
- Monitors resource consumption

## Interpreting Results

```
     http_req_duration..........: avg=45.2ms  min=12ms  med=38ms  max=892ms  p(90)=120ms  p(95)=180ms
     http_req_failed............: 0.12%   ✓ 15     ✗ 12485
     http_reqs..................: 12500   416.67/s
     vus........................: 50      min=50   max=50
```

Key metrics:
- **p(95)**: 95th percentile response time (target: <500ms)
- **http_req_failed**: Error rate (target: <1%)
- **http_reqs**: Requests per second throughput

## Capacity Planning

Based on test results, estimate capacity:

1. Run stress test to find max sustainable RPS
2. Calculate: `Max Users = (Target RPS / Avg RPS per User)`
3. Add 30% headroom for spikes
4. Plan infrastructure accordingly

## CI/CD Integration

Add to GitHub Actions:
```yaml
- name: Run Load Tests
  run: |
    k6 run --out json=results.json load-tests/smoke.js
    # Fail if thresholds not met (exit code != 0)
```
