/**
 * Load Test
 * 
 * Simulates expected production load with a mix of operations.
 * Tests sustained load handling capability.
 * 
 * Usage: k6 run load.js
 * Custom: k6 run --vus 100 --duration 10m load.js
 */

import http from 'k6/http';
import { check, sleep, group } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';
import { BASE_URL, ENDPOINTS, getHeaders, THRESHOLDS, STAGES, randomInt } from './config.js';

// Custom metrics
const partsRequests = new Counter('parts_requests');
const workOrderRequests = new Counter('work_order_requests');
const dashboardRequests = new Counter('dashboard_requests');
const errorRate = new Rate('errors');
const partsDuration = new Trend('parts_duration');
const workOrdersDuration = new Trend('work_orders_duration');

export const options = {
  stages: STAGES.load,
  thresholds: {
    ...THRESHOLDS,
    'parts_duration': ['p(95)<300'],
    'work_orders_duration': ['p(95)<400'],
    'errors': ['rate<0.01'],
  },
};

export default function () {
  const headers = getHeaders();

  // Simulate realistic user behavior with weighted operations
  const operation = randomInt(1, 100);

  if (operation <= 40) {
    // 40% - View dashboard (most common)
    group('Dashboard', () => {
      const res = http.get(`${BASE_URL}${ENDPOINTS.dashboard}`, { headers });
      dashboardRequests.add(1);
      check(res, {
        'dashboard: status ok': (r) => r.status === 200 || r.status === 401,
      });
      errorRate.add(res.status >= 400 && res.status !== 401);
    });
    
  } else if (operation <= 65) {
    // 25% - List parts
    group('Parts List', () => {
      const skip = randomInt(0, 100);
      const res = http.get(`${BASE_URL}${ENDPOINTS.parts}?skip=${skip}&limit=50`, { headers });
      partsRequests.add(1);
      partsDuration.add(res.timings.duration);
      check(res, {
        'parts list: status ok': (r) => r.status === 200 || r.status === 401,
        'parts list: response < 500ms': (r) => r.timings.duration < 500,
      });
      errorRate.add(res.status >= 400 && res.status !== 401);
    });
    
  } else if (operation <= 85) {
    // 20% - List work orders
    group('Work Orders List', () => {
      const res = http.get(`${BASE_URL}${ENDPOINTS.workOrders}?limit=50`, { headers });
      workOrderRequests.add(1);
      workOrdersDuration.add(res.timings.duration);
      check(res, {
        'work orders: status ok': (r) => r.status === 200 || r.status === 401,
        'work orders: response < 500ms': (r) => r.timings.duration < 500,
      });
      errorRate.add(res.status >= 400 && res.status !== 401);
    });
    
  } else if (operation <= 92) {
    // 7% - View customers
    group('Customers', () => {
      const res = http.get(`${BASE_URL}${ENDPOINTS.customers}`, { headers });
      check(res, {
        'customers: status ok': (r) => r.status === 200 || r.status === 401,
      });
      errorRate.add(res.status >= 400 && res.status !== 401);
    });
    
  } else if (operation <= 97) {
    // 5% - View analytics
    group('Analytics', () => {
      const res = http.get(`${BASE_URL}${ENDPOINTS.analytics}?period=30d`, { headers });
      check(res, {
        'analytics: status ok': (r) => r.status === 200 || r.status === 401,
      });
      errorRate.add(res.status >= 400 && res.status !== 401);
    });
    
  } else {
    // 3% - Health check
    group('Health', () => {
      const res = http.get(`${BASE_URL}${ENDPOINTS.health}`);
      check(res, {
        'health: status 200': (r) => r.status === 200,
      });
      errorRate.add(res.status >= 400);
    });
  }

  // Think time between requests (1-3 seconds)
  sleep(randomInt(1, 3));
}

export function handleSummary(data) {
  const summary = {
    'Total Requests': data.metrics.http_reqs.values.count,
    'Failed Requests': data.metrics.http_req_failed.values.passes,
    'Error Rate': `${(data.metrics.errors?.values.rate * 100 || 0).toFixed(2)}%`,
    'Avg Response Time': `${data.metrics.http_req_duration.values.avg.toFixed(2)}ms`,
    'p95 Response Time': `${data.metrics.http_req_duration.values['p(95)'].toFixed(2)}ms`,
    'p99 Response Time': `${data.metrics.http_req_duration.values['p(99)'].toFixed(2)}ms`,
    'Requests/sec': data.metrics.http_reqs.values.rate.toFixed(2),
    'Parts Requests': data.metrics.parts_requests?.values.count || 0,
    'Work Order Requests': data.metrics.work_order_requests?.values.count || 0,
    'Dashboard Requests': data.metrics.dashboard_requests?.values.count || 0,
  };

  console.log('\n=== Load Test Results ===');
  for (const [key, value] of Object.entries(summary)) {
    console.log(`${key}: ${value}`);
  }

  return {};
}
