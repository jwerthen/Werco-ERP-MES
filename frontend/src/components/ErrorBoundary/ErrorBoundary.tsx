/**
 * ErrorBoundary Component
 * 
 * React error boundary that catches errors in child components and displays
 * appropriate fallback UI based on the boundary level.
 */

import React, { Component, ErrorInfo, ReactNode } from 'react';
import { ErrorFallback } from './ErrorFallback';
import { logError } from '../../services/errorLogging';

export type ErrorBoundaryLevel = 'global' | 'page' | 'section' | 'widget';

interface ErrorBoundaryProps {
  children: ReactNode;
  fallback?: ReactNode | ((error: Error, reset: () => void) => ReactNode);
  onError?: (error: Error, errorInfo: ErrorInfo) => void;
  level?: ErrorBoundaryLevel;
  name?: string;
}

interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
  errorInfo: ErrorInfo | null;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { 
      hasError: false, 
      error: null, 
      errorInfo: null 
    };
  }

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    const { onError, name, level = 'section' } = this.props;

    // Log error with context
    logError({
      error,
      errorInfo,
      boundaryName: name,
      boundaryLevel: level,
      url: window.location.href,
      timestamp: new Date().toISOString(),
      userAgent: navigator.userAgent,
    });

    // Call custom error handler if provided
    if (onError) {
      onError(error, errorInfo);
    }

    this.setState({ errorInfo });
  }

  resetError = (): void => {
    this.setState({ 
      hasError: false, 
      error: null, 
      errorInfo: null 
    });
  };

  render(): ReactNode {
    const { hasError, error } = this.state;
    const { children, fallback, level = 'section', name } = this.props;

    if (hasError && error) {
      // Custom fallback function
      if (typeof fallback === 'function') {
        return fallback(error, this.resetError);
      }

      // Custom fallback component
      if (fallback) {
        return fallback;
      }

      // Default fallback based on level
      return (
        <ErrorFallback
          error={error}
          level={level}
          boundaryName={name}
          onReset={this.resetError}
        />
      );
    }

    return children;
  }
}

export default ErrorBoundary;
