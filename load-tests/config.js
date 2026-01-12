/**
 * Shared configuration for k6 load tests
 */

// Base URL from environment or default
export const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';

// Test user credentials
export const TEST_USER = {
  email: __ENV.TEST_USER_EMAIL || 'admin@werco.com',
  token: __ENV.TEST_USER_TOKEN || '',
};

// Common headers
export function getHeaders(token = TEST_USER.token) {
  return {
    'Content-Type': 'application/json',
    'Authorization': token ? `Bearer ${token}` : '',
  };
}

// Common thresholds
export const THRESHOLDS = {
  // Response time thresholds
  http_req_duration: ['p(95)<500', 'p(99)<1000'],
  // Error rate threshold
  http_req_failed: ['rate<0.01'],
  // Minimum throughput
  http_reqs: ['rate>10'],
};

// Stricter thresholds for critical endpoints
export const STRICT_THRESHOLDS = {
  http_req_duration: ['p(95)<200', 'p(99)<500'],
  http_req_failed: ['rate<0.001'],
  http_reqs: ['rate>50'],
};

// API endpoints
export const ENDPOINTS = {
  // Auth
  login: '/auth/login',
  me: '/auth/me',
  refresh: '/auth/refresh',
  
  // Core entities
  parts: '/parts',
  workOrders: '/work-orders',
  customers: '/customers',
  workCenters: '/work-centers',
  
  // Operations
  dashboard: '/dashboard',
  shopFloor: '/shop-floor/operations',
  
  // Reports
  reports: '/reports',
  analytics: '/analytics/kpis',
  
  // Health
  health: '/health',
  healthDetailed: '/health/detailed',
};

// Scenario stages for different test types
export const STAGES = {
  smoke: [
    { duration: '10s', target: 1 },
    { duration: '20s', target: 1 },
  ],
  
  load: [
    { duration: '1m', target: 50 },   // Ramp up
    { duration: '3m', target: 50 },   // Stay at 50
    { duration: '1m', target: 0 },    // Ramp down
  ],
  
  stress: [
    { duration: '2m', target: 100 },  // Ramp to 100
    { duration: '2m', target: 200 },  // Ramp to 200
    { duration: '2m', target: 300 },  // Ramp to 300
    { duration: '2m', target: 400 },  // Ramp to 400
    { duration: '2m', target: 0 },    // Ramp down
  ],
  
  spike: [
    { duration: '30s', target: 10 },  // Normal
    { duration: '10s', target: 200 }, // Spike up
    { duration: '30s', target: 200 }, // Stay high
    { duration: '10s', target: 10 },  // Spike down
    { duration: '1m', target: 10 },   // Recovery
    { duration: '30s', target: 0 },   // Ramp down
  ],
  
  soak: [
    { duration: '2m', target: 30 },   // Ramp up
    { duration: '26m', target: 30 },  // Stay steady
    { duration: '2m', target: 0 },    // Ramp down
  ],
};

// Random data generators
export function randomString(length = 8) {
  const chars = 'abcdefghijklmnopqrstuvwxyz0123456789';
  let result = '';
  for (let i = 0; i < length; i++) {
    result += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return result;
}

export function randomInt(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

export function randomElement(array) {
  return array[Math.floor(Math.random() * array.length)];
}
