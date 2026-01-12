/**
 * Soak Test (Endurance Test)
 * 
 * Extended duration test to identify memory leaks, connection pool exhaustion,
 * and other issues that only appear over time.
 * 
 * Usage: k6 run soak.js
 * Extended: k6 run --duration 2h soak.js
 */

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Counter, Gauge } from 'k6/metrics';
import { BASE_URL, ENDPOINTS, getHeaders, STAGES, randomInt } from './config.js';

// Time-series metrics to track degradation
const responseTimeOverTime = new Trend('response_time_over_time');
const errorsByMinute = new Counter('errors_by_minute');
const activeConnections = new Gauge('active_connections');

export const options = {
  stages: STAGES.soak,
  thresholds: {
    http_req_duration: ['p(95)<500', 'p(99)<1000'],
    http_req_failed: ['rate<0.01'],
    'response_time_over_time': ['p(95)<600'],  // Should not degrade much
  },
};

// Track metrics over time
let startTime = null;
let requestsThisMinute = 0;
let errorsThisMinute = 0;
let lastMinute = 0;

export function setup() {
  console.log('Starting soak test...');
  console.log(`Base URL: ${BASE_URL}`);
  console.log('Duration: ~30 minutes (or custom with --duration)');
  return { startTime: Date.now() };
}

export default function (data) {
  const headers = getHeaders();
  const now = Date.now();
  const elapsedMinutes = Math.floor((now - data.startTime) / 60000);
  
  // Log progress every minute
  if (elapsedMinutes > lastMinute) {
    lastMinute = elapsedMinutes;
    console.log(`[${elapsedMinutes}m] Requests: ${requestsThisMinute}, Errors: ${errorsThisMinute}`);
    requestsThisMinute = 0;
    errorsThisMinute = 0;
  }

  // Realistic workload mix
  const operations = [
    () => http.get(`${BASE_URL}${ENDPOINTS.dashboard}`, { headers }),
    () => http.get(`${BASE_URL}${ENDPOINTS.parts}?skip=${randomInt(0, 50)}&limit=20`, { headers }),
    () => http.get(`${BASE_URL}${ENDPOINTS.workOrders}?limit=20`, { headers }),
    () => http.get(`${BASE_URL}${ENDPOINTS.customers}`, { headers }),
    () => http.get(`${BASE_URL}${ENDPOINTS.health}`),
  ];

  // Pick random operation
  const operation = operations[randomInt(0, operations.length - 1)];
  const res = operation();
  
  requestsThisMinute++;
  responseTimeOverTime.add(res.timings.duration);
  
  const isError = res.status >= 400 && res.status !== 401;
  if (isError) {
    errorsThisMinute++;
    errorsByMinute.add(1);
  }

  check(res, {
    'status ok': (r) => r.status < 400 || r.status === 401,
    'response time < 1s': (r) => r.timings.duration < 1000,
  });

  // Normal think time
  sleep(randomInt(1, 3));
}

export function teardown(data) {
  const durationMinutes = Math.floor((Date.now() - data.startTime) / 60000);
  console.log(`\nSoak test completed after ${durationMinutes} minutes`);
}

export function handleSummary(data) {
  const metrics = data.metrics;
  
  console.log('\n=== Soak Test Results ===');
  
  console.log('\nDuration Metrics:');
  console.log(`  Total Requests: ${metrics.http_reqs.values.count}`);
  console.log(`  Requests/sec (avg): ${metrics.http_reqs.values.rate.toFixed(2)}`);
  console.log(`  Total Errors: ${metrics.http_req_failed.values.passes}`);
  console.log(`  Error Rate: ${(metrics.http_req_failed.values.rate * 100).toFixed(4)}%`);

  console.log('\nResponse Time Analysis:');
  console.log(`  Min: ${metrics.http_req_duration.values.min.toFixed(2)}ms`);
  console.log(`  Avg: ${metrics.http_req_duration.values.avg.toFixed(2)}ms`);
  console.log(`  Med: ${metrics.http_req_duration.values.med.toFixed(2)}ms`);
  console.log(`  p90: ${metrics.http_req_duration.values['p(90)'].toFixed(2)}ms`);
  console.log(`  p95: ${metrics.http_req_duration.values['p(95)'].toFixed(2)}ms`);
  console.log(`  p99: ${metrics.http_req_duration.values['p(99)'].toFixed(2)}ms`);
  console.log(`  Max: ${metrics.http_req_duration.values.max.toFixed(2)}ms`);

  // Stability analysis
  console.log('\nStability Analysis:');
  const avg = metrics.http_req_duration.values.avg;
  const p99 = metrics.http_req_duration.values['p(99)'];
  const max = metrics.http_req_duration.values.max;
  
  // Check for degradation indicators
  const p99ToAvgRatio = p99 / avg;
  const maxToP99Ratio = max / p99;
  
  console.log(`  p99/avg ratio: ${p99ToAvgRatio.toFixed(2)} (ideally < 3)`);
  console.log(`  max/p99 ratio: ${maxToP99Ratio.toFixed(2)} (ideally < 5)`);
  
  if (p99ToAvgRatio > 5) {
    console.log('  ⚠️  High variance detected - possible resource contention');
  }
  if (maxToP99Ratio > 10) {
    console.log('  ⚠️  Extreme outliers detected - possible memory issues');
  }
  
  const errorRate = metrics.http_req_failed.values.rate;
  if (errorRate > 0.001) {
    console.log('  ⚠️  Error rate > 0.1% - investigate root cause');
  }
  
  if (p99ToAvgRatio < 3 && maxToP99Ratio < 5 && errorRate < 0.001) {
    console.log('  ✅ System is stable over extended duration');
  }

  // Capacity recommendations
  console.log('\nCapacity Recommendations:');
  const steadyStateRPS = metrics.http_reqs.values.rate;
  console.log(`  Sustainable RPS: ${steadyStateRPS.toFixed(2)}`);
  console.log(`  Estimated daily requests: ${(steadyStateRPS * 86400).toLocaleString()}`);
  console.log(`  Estimated hourly capacity: ${(steadyStateRPS * 3600).toLocaleString()} requests`);

  return {};
}
