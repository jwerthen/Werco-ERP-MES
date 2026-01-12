/**
 * Spike Test
 * 
 * Tests system behavior under sudden traffic spikes.
 * Evaluates auto-scaling and recovery capabilities.
 * 
 * Usage: k6 run spike.js
 */

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Trend, Counter } from 'k6/metrics';
import { BASE_URL, ENDPOINTS, getHeaders, STAGES, randomInt } from './config.js';

// Metrics for spike analysis
const preSpike = new Trend('pre_spike_duration');
const duringSpike = new Trend('during_spike_duration');
const postSpike = new Trend('post_spike_duration');
const spikeErrors = new Counter('spike_errors');
const recoveryTime = new Trend('recovery_time');

export const options = {
  stages: STAGES.spike,
  thresholds: {
    http_req_duration: ['p(95)<3000'],  // Allow higher during spike
    http_req_failed: ['rate<0.15'],      // Allow more errors during spike
    'pre_spike_duration': ['p(95)<500'],
    'post_spike_duration': ['p(95)<1000'], // Recovery should be quick
  },
};

// Track spike phase
function getPhase(vu, iteration) {
  // Rough estimation based on VU count
  if (vu <= 10) return 'pre';
  if (vu >= 100) return 'spike';
  return 'post';
}

export default function () {
  const headers = getHeaders();
  const phase = __VU <= 15 ? 'normal' : (__VU >= 150 ? 'spike' : 'recovery');
  
  // Critical path endpoints
  const endpoints = [
    { url: `${BASE_URL}${ENDPOINTS.dashboard}`, name: 'dashboard' },
    { url: `${BASE_URL}${ENDPOINTS.parts}?limit=20`, name: 'parts' },
    { url: `${BASE_URL}${ENDPOINTS.workOrders}?limit=20`, name: 'work_orders' },
  ];

  // Pick random endpoint
  const endpoint = endpoints[randomInt(0, endpoints.length - 1)];
  
  const res = http.get(endpoint.url, { 
    headers,
    tags: { phase, endpoint: endpoint.name }
  });

  // Track metrics by phase
  if (phase === 'normal') {
    preSpike.add(res.timings.duration);
  } else if (phase === 'spike') {
    duringSpike.add(res.timings.duration);
    if (res.status >= 500) {
      spikeErrors.add(1);
    }
  } else {
    postSpike.add(res.timings.duration);
  }

  check(res, {
    'response received': (r) => r.status > 0,
    'not server error': (r) => r.status < 500 || r.status === 503, // 503 acceptable during spike
  });

  // Minimal sleep
  sleep(randomInt(0.2, 0.8));
}

export function handleSummary(data) {
  const metrics = data.metrics;
  
  console.log('\n=== Spike Test Results ===');
  
  console.log('\nOverall Performance:');
  console.log(`  Total Requests: ${metrics.http_reqs.values.count}`);
  console.log(`  Error Rate: ${(metrics.http_req_failed.values.rate * 100).toFixed(2)}%`);
  console.log(`  Avg Response: ${metrics.http_req_duration.values.avg.toFixed(2)}ms`);
  console.log(`  Peak Response: ${metrics.http_req_duration.values.max.toFixed(2)}ms`);

  console.log('\nPhase Analysis:');
  if (metrics.pre_spike_duration) {
    console.log(`  Pre-Spike p95: ${metrics.pre_spike_duration.values['p(95)']?.toFixed(2) || 'N/A'}ms`);
  }
  if (metrics.during_spike_duration) {
    console.log(`  During Spike p95: ${metrics.during_spike_duration.values['p(95)']?.toFixed(2) || 'N/A'}ms`);
  }
  if (metrics.post_spike_duration) {
    console.log(`  Post-Spike p95: ${metrics.post_spike_duration.values['p(95)']?.toFixed(2) || 'N/A'}ms`);
  }

  // Recovery analysis
  console.log('\nRecovery Analysis:');
  const preSpikeP95 = metrics.pre_spike_duration?.values['p(95)'] || 0;
  const postSpikeP95 = metrics.post_spike_duration?.values['p(95)'] || 0;
  
  if (preSpikeP95 > 0 && postSpikeP95 > 0) {
    const recoveryRatio = postSpikeP95 / preSpikeP95;
    console.log(`  Recovery Ratio: ${recoveryRatio.toFixed(2)}x baseline`);
    
    if (recoveryRatio <= 1.2) {
      console.log('  ✅ Excellent recovery - system returned to baseline quickly');
    } else if (recoveryRatio <= 2.0) {
      console.log('  ⚠️  Moderate recovery - some lingering effects');
    } else {
      console.log('  ❌ Poor recovery - system did not recover well');
    }
  }

  // Spike handling
  const spikeErrorCount = metrics.spike_errors?.values.count || 0;
  console.log(`  Errors During Spike: ${spikeErrorCount}`);
  
  if (spikeErrorCount === 0) {
    console.log('  ✅ No errors during spike - excellent resilience');
  } else if (spikeErrorCount < 10) {
    console.log('  ⚠️  Some errors during spike - acceptable');
  } else {
    console.log('  ❌ Many errors during spike - needs improvement');
  }

  return {};
}
