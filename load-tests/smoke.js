/**
 * Smoke Test
 * 
 * Quick sanity check to verify the system is working.
 * Run this before other load tests to ensure basic functionality.
 * 
 * Usage: k6 run smoke.js
 */

import http from 'k6/http';
import { check, sleep } from 'k6';
import { BASE_URL, ENDPOINTS, getHeaders, THRESHOLDS, STAGES } from './config.js';

export const options = {
  stages: STAGES.smoke,
  thresholds: THRESHOLDS,
};

export default function () {
  // Test health endpoint
  let res = http.get(`${BASE_URL}${ENDPOINTS.health}`);
  check(res, {
    'health: status 200': (r) => r.status === 200,
    'health: response time < 200ms': (r) => r.timings.duration < 200,
  });

  sleep(1);

  // Test detailed health
  res = http.get(`${BASE_URL}${ENDPOINTS.healthDetailed}`);
  check(res, {
    'health/detailed: status 200': (r) => r.status === 200,
    'health/detailed: has database status': (r) => {
      try {
        const body = JSON.parse(r.body);
        return body.database !== undefined;
      } catch {
        return false;
      }
    },
  });

  sleep(1);

  // Test parts endpoint (requires auth in production)
  res = http.get(`${BASE_URL}${ENDPOINTS.parts}?limit=10`, {
    headers: getHeaders(),
  });
  check(res, {
    'parts: status 200 or 401': (r) => r.status === 200 || r.status === 401,
    'parts: response time < 500ms': (r) => r.timings.duration < 500,
  });

  sleep(1);

  // Test work orders endpoint
  res = http.get(`${BASE_URL}${ENDPOINTS.workOrders}?limit=10`, {
    headers: getHeaders(),
  });
  check(res, {
    'work-orders: status 200 or 401': (r) => r.status === 200 || r.status === 401,
    'work-orders: response time < 500ms': (r) => r.timings.duration < 500,
  });

  sleep(1);
}

export function handleSummary(data) {
  console.log('\n=== Smoke Test Summary ===');
  console.log(`Total requests: ${data.metrics.http_reqs.values.count}`);
  console.log(`Failed requests: ${data.metrics.http_req_failed.values.passes}`);
  console.log(`Avg response time: ${data.metrics.http_req_duration.values.avg.toFixed(2)}ms`);
  console.log(`p95 response time: ${data.metrics.http_req_duration.values['p(95)'].toFixed(2)}ms`);
  
  const passed = data.metrics.http_req_failed.values.rate < 0.01;
  console.log(`\nResult: ${passed ? 'PASSED' : 'FAILED'}`);
  
  return {};
}
