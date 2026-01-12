/**
 * Test Utilities
 * 
 * Common utilities for testing React components with all providers.
 */

import React, { ReactElement } from 'react';
import { render, RenderOptions } from '@testing-library/react';
import { BrowserRouter } from 'react-router-dom';
import { AuthProvider } from '../context/AuthContext';
import { TourProvider } from '../context/TourContext';

// Mock user for authenticated tests
export const mockUser = {
  id: 1,
  email: 'test@werco.com',
  first_name: 'Test',
  last_name: 'User',
  role: 'admin' as const,
  is_active: true,
  is_superuser: true,
};

// Mock auth context value
export const mockAuthContext = {
  user: mockUser,
  isAuthenticated: true,
  isLoading: false,
  login: jest.fn(),
  logout: jest.fn(),
  refreshToken: jest.fn(),
};

// All providers wrapper for testing
const AllProviders: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  return (
    <BrowserRouter>
      <AuthProvider>
        <TourProvider>
          {children}
        </TourProvider>
      </AuthProvider>
    </BrowserRouter>
  );
};

// Router only wrapper
export const RouterWrapper: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  return <BrowserRouter>{children}</BrowserRouter>;
};

// Custom render with all providers
const customRender = (
  ui: ReactElement,
  options?: Omit<RenderOptions, 'wrapper'>
) => render(ui, { wrapper: AllProviders, ...options });

// Custom render with router only
export const renderWithRouter = (
  ui: ReactElement,
  options?: Omit<RenderOptions, 'wrapper'>
) => render(ui, { wrapper: RouterWrapper, ...options });

// Re-export everything from testing-library
export * from '@testing-library/react';
export { customRender as render };

// Helper to wait for async operations
export const waitForLoadingToFinish = () => 
  new Promise(resolve => setTimeout(resolve, 0));

// Mock localStorage
export const mockLocalStorage = () => {
  const store: Record<string, string> = {};
  return {
    getItem: jest.fn((key: string) => store[key] || null),
    setItem: jest.fn((key: string, value: string) => { store[key] = value; }),
    removeItem: jest.fn((key: string) => { delete store[key]; }),
    clear: jest.fn(() => { Object.keys(store).forEach(key => delete store[key]); }),
  };
};

// Mock API response helper
export function mockApiResponse<T>(data: T, status = 200) {
  return {
    data,
    status,
    statusText: 'OK',
    headers: {},
    config: {} as any,
  };
}

// Mock API error helper
export function mockApiError(message: string, status = 400) {
  const error = new Error(message) as any;
  error.response = {
    data: { detail: message },
    status,
    statusText: 'Bad Request',
  };
  return error;
}
