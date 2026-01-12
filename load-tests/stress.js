/**
 * Stress Test
 * 
 * Gradually increases load to find the system's breaking point.
 * Identifies maximum capacity and degradation patterns.
 * 
 * Usage: k6 run stress.js
 */

import http from 'k6/http';
import { check, sleep, group } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';
import { BASE_URL, ENDPOINTS, getHeaders, STAGES, randomInt } from './config.js';

// Custom metrics for stress analysis
const requestsPerStage = new Counter('requests_per_stage');
const errorsByStage = new Counter('errors_by_stage');
const responseTimes = new Trend('response_times_by_stage');

export const options = {
  stages: STAGES.stress,
  thresholds: {
    // Relaxed thresholds for stress test - we expect some failures
    http_req_duration: ['p(95)<2000'],  // 2 second max
    http_req_failed: ['rate<0.10'],      // Up to 10% errors acceptable
  },
};

// Track current stage
let currentStage = 0;
let lastVUs = 0;

export default function () {
  const headers = getHeaders();
  
  // Track stage changes
  const currentVUs = __VU;
  if (currentVUs !== lastVUs) {
    lastVUs = currentVUs;
    console.log(`VUs changed to: ${currentVUs}`);
  }

  // Mix of read operations (stress tests focus on reads)
  const batch = http.batch([
    ['GET', `${BASE_URL}${ENDPOINTS.health}`, null, { tags: { name: 'health' } }],
    ['GET', `${BASE_URL}${ENDPOINTS.parts}?limit=20`, null, { headers, tags: { name: 'parts' } }],
    ['GET', `${BASE_URL}${ENDPOINTS.workOrders}?limit=20`, null, { headers, tags: { name: 'work_orders' } }],
  ]);

  // Check each response
  batch.forEach((res, idx) => {
    const names = ['health', 'parts', 'work_orders'];
    requestsPerStage.add(1);
    responseTimes.add(res.timings.duration);
    
    const isError = res.status >= 400 && res.status !== 401;
    if (isError) {
      errorsByStage.add(1);
    }
    
    check(res, {
      [`${names[idx]}: responded`]: (r) => r.status > 0,
      [`${names[idx]}: not server error`]: (r) => r.status < 500,
    });
  });

  // Minimal think time during stress
  sleep(randomInt(0.5, 1.5));
}

export function handleSummary(data) {
  const metrics = data.metrics;
  
  console.log('\n=== Stress Test Results ===');
  console.log('\nPerformance Metrics:');
  console.log(`  Total Requests: ${metrics.http_reqs.values.count}`);
  console.log(`  Requests/sec (avg): ${metrics.http_reqs.values.rate.toFixed(2)}`);
  console.log(`  Failed Requests: ${metrics.http_req_failed.values.passes}`);
  console.log(`  Error Rate: ${(metrics.http_req_failed.values.rate * 100).toFixed(2)}%`);
  
  console.log('\nResponse Times:');
  console.log(`  Min: ${metrics.http_req_duration.values.min.toFixed(2)}ms`);
  console.log(`  Avg: ${metrics.http_req_duration.values.avg.toFixed(2)}ms`);
  console.log(`  Med: ${metrics.http_req_duration.values.med.toFixed(2)}ms`);
  console.log(`  p90: ${metrics.http_req_duration.values['p(90)'].toFixed(2)}ms`);
  console.log(`  p95: ${metrics.http_req_duration.values['p(95)'].toFixed(2)}ms`);
  console.log(`  Max: ${metrics.http_req_duration.values.max.toFixed(2)}ms`);

  console.log('\nCapacity Analysis:');
  const maxRPS = metrics.http_reqs.values.rate;
  const avgResponseTime = metrics.http_req_duration.values.avg;
  const errorRate = metrics.http_req_failed.values.rate;
  
  // Estimate sustainable capacity (where error rate < 1% and p95 < 500ms)
  const sustainableRPS = maxRPS * (1 - errorRate);
  console.log(`  Peak RPS: ${maxRPS.toFixed(2)}`);
  console.log(`  Estimated Sustainable RPS: ${sustainableRPS.toFixed(2)}`);
  console.log(`  Avg Response at Peak: ${avgResponseTime.toFixed(2)}ms`);

  // Recommendations
  console.log('\nRecommendations:');
  if (errorRate > 0.05) {
    console.log('  ⚠️  High error rate - consider scaling resources');
  }
  if (avgResponseTime > 500) {
    console.log('  ⚠️  High average response time - optimize slow queries');
  }
  if (maxRPS < 100) {
    console.log('  ⚠️  Low throughput - check for bottlenecks');
  }
  if (errorRate < 0.01 && avgResponseTime < 200) {
    console.log('  ✅ System handled stress well - capacity is adequate');
  }

  return {};
}
