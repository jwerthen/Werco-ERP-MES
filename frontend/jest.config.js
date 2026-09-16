process.env.NODE_ENV = 'test';

module.exports = {
  testEnvironment: 'jsdom',
  setupFilesAfterEnv: ['<rootDir>/src/setupTests.ts'],
  moduleNameMapper: {
    '^@/(.*)$': '<rootDir>/src/$1',
    '\\.(css|less|scss|sass)$': 'identity-obj-proxy',
    '\\.(jpg|jpeg|png|gif|svg)$': '<rootDir>/__mocks__/fileMock.js',
  },
  transform: {
    '^.+\\.(ts|tsx)$': ['ts-jest', {
      tsconfig: {
        jsx: 'react',
        esModuleInterop: true,
        allowSyntheticDefaultImports: true
      }
    }],
  },
  collectCoverageFrom: [
    'src/**/*.{ts,tsx}',
    '!src/**/*.d.ts',
    '!src/**/*.stories.tsx',
    '!src/main.tsx',
    '!src/vite-env.d.ts',
  ],
  // Ratchet, not an aspiration: these sit a few points under actual coverage
  // (statements 56.4 / branches 46.7 / functions 41.3 / lines 56.3) so the
  // numbers cannot silently regress. They were 2/2/2/2 -- roughly 28x below
  // actual, which gated nothing. Raise them when coverage genuinely climbs;
  // do not bump them reflexively on every point gained. `functions` is the
  // tightest and least stable of the four, so it keeps the widest margin.
  coverageThreshold: {
    global: {
      branches: 43,
      functions: 38,
      lines: 52,
      statements: 52,
    }
  },
  testMatch: [
    '<rootDir>/src/**/*.test.{ts,tsx}',
    '<rootDir>/src/**/*.spec.{ts,tsx}'
  ],
  moduleFileExtensions: ['ts', 'tsx', 'js', 'jsx'],
  verbose: true,
  testTimeout: 10000
};
