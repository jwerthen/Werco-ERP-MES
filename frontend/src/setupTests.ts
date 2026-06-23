// jest-dom adds custom jest matchers for asserting on DOM nodes.
import '@testing-library/jest-dom';
import { TextDecoder, TextEncoder } from 'util';

if (!global.TextEncoder) {
  global.TextEncoder = TextEncoder as any;
}

if (!global.TextDecoder) {
  global.TextDecoder = TextDecoder as any;
}

// Mock window.matchMedia
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => {},
  }),
});

// jsdom does not implement URL.createObjectURL / revokeObjectURL — components
// that build object URLs for blob previews (e.g. LaserNestPdfPreview) need them.
// Individual tests can still spyOn these to assert specific values.
if (typeof window.URL.createObjectURL !== 'function') {
  window.URL.createObjectURL = (() => 'blob:mock') as typeof window.URL.createObjectURL;
}
if (typeof window.URL.revokeObjectURL !== 'function') {
  window.URL.revokeObjectURL = (() => undefined) as typeof window.URL.revokeObjectURL;
}

// Mock IntersectionObserver
global.IntersectionObserver = class IntersectionObserver {
  disconnect() {}
  observe() {}
  takeRecords() {
    return [];
  }
  unobserve() {}
} as any;

// Suppress console errors in tests
const originalConsoleError = console.error;
beforeAll(() => {
  console.error = (...args: any[]) => {
    if (
      typeof args[0] === 'string' &&
      args[0].includes('Warning: ReactDOM.render')
    ) {
      return;
    }
    originalConsoleError.call(console, ...args);
  };
});

afterAll(() => {
  console.error = originalConsoleError;
});
