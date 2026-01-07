/**
 * ErrorFallback Components
 * 
 * Level-specific fallback UI for different error boundary contexts.
 * All components follow accessibility guidelines with proper ARIA attributes.
 */

import React from 'react';
import { useNavigate } from 'react-router-dom';
import { getErrorMessage, generateErrorId, ErrorMessage } from './errorMessages';
import { ErrorBoundaryLevel } from './ErrorBoundary';

interface ErrorFallbackProps {
  error: Error;
  level: ErrorBoundaryLevel;
  boundaryName?: string;
  onReset: () => void;
}

export function ErrorFallback({ error, level, boundaryName, onReset }: ErrorFallbackProps) {
  const errorMessage = getErrorMessage(error);

  switch (level) {
    case 'global':
      return <GlobalErrorFallback error={error} message={errorMessage} />;
    case 'page':
      return <PageErrorFallback error={error} message={errorMessage} onReset={onReset} />;
    case 'section':
      return <SectionErrorFallback error={error} message={errorMessage} onReset={onReset} name={boundaryName} />;
    case 'widget':
      return <WidgetErrorFallback error={error} message={errorMessage} onReset={onReset} />;
    default:
      return <SectionErrorFallback error={error} message={errorMessage} onReset={onReset} />;
  }
}

/**
 * Global Error Fallback - Full page, catastrophic failure
 * Used when the entire app crashes
 */
function GlobalErrorFallback({ error, message }: { error: Error; message: ErrorMessage }) {
  const handleRefresh = () => window.location.reload();
  const handleGoHome = () => { window.location.href = '/'; };

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-900 to-slate-800 flex items-center justify-center p-4">
      <div className="max-w-md w-full bg-white rounded-2xl shadow-2xl p-8 text-center">
        {/* Error Icon */}
        <div className="w-20 h-20 bg-gradient-to-br from-rose-500 to-red-600 rounded-full flex items-center justify-center mx-auto mb-6 shadow-lg">
          <svg className="w-10 h-10 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} 
              d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" 
            />
          </svg>
        </div>

        <h1 className="text-2xl font-bold text-gray-900 mb-3">
          {message.title}
        </h1>

        <p className="text-gray-600 mb-8 leading-relaxed">
          {message.description}
        </p>

        <div className="space-y-3">
          <button
            onClick={handleRefresh}
            className="w-full flex items-center justify-center gap-3 px-6 py-4 bg-gradient-to-r from-rose-500 to-red-600 text-white rounded-xl hover:from-rose-600 hover:to-red-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-rose-500 focus-visible:ring-offset-2 transition-all font-semibold shadow-lg hover:shadow-xl min-h-[52px]"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
            Refresh Page
          </button>

          <button
            onClick={handleGoHome}
            className="w-full flex items-center justify-center gap-3 px-6 py-4 bg-gray-100 text-gray-700 rounded-xl hover:bg-gray-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-500 focus-visible:ring-offset-2 transition-all font-medium min-h-[52px]"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6" />
            </svg>
            Go to Dashboard
          </button>
        </div>

        <div className="mt-8 pt-6 border-t border-gray-200">
          <p className="text-sm text-gray-500">
            If this problem continues, please contact IT support.
          </p>
          <p className="mt-2 text-xs text-gray-400 font-mono bg-gray-50 rounded-lg py-2 px-3 inline-block">
            Error ID: {generateErrorId(error)}
          </p>
        </div>
      </div>
    </div>
  );
}

/**
 * Page Error Fallback - Page content failed, navigation still works
 */
function PageErrorFallback({
  error,
  message,
  onReset
}: {
  error: Error;
  message: ErrorMessage;
  onReset: () => void;
}) {
  const navigate = useNavigate();

  return (
    <div className="flex-1 flex items-center justify-center p-8">
      <div className="max-w-lg w-full text-center">
        {/* Warning Icon */}
        <div className="w-16 h-16 bg-gradient-to-br from-amber-400 to-orange-500 rounded-full flex items-center justify-center mx-auto mb-6 shadow-lg">
          <svg className="w-8 h-8 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} 
              d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" 
            />
          </svg>
        </div>

        <h1 className="text-xl font-bold text-gray-900 mb-2">
          {message.title}
        </h1>

        <p className="text-gray-600 mb-8">
          {message.description}
        </p>

        <div className="flex flex-col sm:flex-row gap-3 justify-center">
          <button
            onClick={onReset}
            className="flex items-center justify-center gap-2 px-6 py-3 bg-gradient-to-r from-rose-500 to-red-600 text-white rounded-xl hover:from-rose-600 hover:to-red-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-rose-500 focus-visible:ring-offset-2 transition-all font-medium shadow-md min-h-[48px]"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
            Try Again
          </button>

          <button
            onClick={() => navigate(-1)}
            className="flex items-center justify-center gap-2 px-6 py-3 bg-gray-100 text-gray-700 rounded-xl hover:bg-gray-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-500 focus-visible:ring-offset-2 transition-all font-medium min-h-[48px]"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 19l-7-7m0 0l7-7m-7 7h18" />
            </svg>
            Go Back
          </button>
        </div>

        <p className="mt-8 text-sm text-gray-500">
          Error ID: <span className="font-mono bg-gray-100 px-2 py-1 rounded">{generateErrorId(error)}</span>
        </p>
      </div>
    </div>
  );
}

/**
 * Section Error Fallback - Part of page failed
 */
function SectionErrorFallback({
  error,
  message,
  onReset,
  name
}: {
  error: Error;
  message: ErrorMessage;
  onReset: () => void;
  name?: string;
}) {
  return (
    <div
      className="bg-gradient-to-br from-rose-50 to-red-50 border border-rose-200 rounded-xl p-6"
      role="alert"
      aria-live="polite"
    >
      <div className="flex items-start gap-4">
        <div className="flex-shrink-0">
          <div className="w-10 h-10 bg-rose-100 rounded-full flex items-center justify-center">
            <svg className="w-5 h-5 text-rose-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} 
                d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" 
              />
            </svg>
          </div>
        </div>

        <div className="flex-1 min-w-0">
          <h3 className="text-sm font-semibold text-rose-800">
            {name ? `Error loading ${name}` : message.title}
          </h3>
          <p className="mt-1 text-sm text-rose-700">
            {message.description}
          </p>

          <div className="mt-4 flex items-center gap-3">
            <button
              onClick={onReset}
              className="inline-flex items-center gap-2 px-4 py-2 text-sm bg-rose-600 text-white rounded-lg hover:bg-rose-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-rose-500 focus-visible:ring-offset-2 transition-colors min-h-[44px] font-medium"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
              </svg>
              Retry
            </button>
            
            <span className="text-xs text-rose-500 font-mono">
              {generateErrorId(error)}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * Widget Error Fallback - Minimal inline error
 */
function WidgetErrorFallback({
  error,
  message,
  onReset
}: {
  error: Error;
  message: ErrorMessage;
  onReset: () => void;
}) {
  return (
    <div
      className="flex items-center gap-3 p-4 bg-gray-100 rounded-lg text-sm text-gray-600"
      role="alert"
    >
      <div className="flex-shrink-0">
        <svg className="w-5 h-5 text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} 
            d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" 
          />
        </svg>
      </div>
      <span className="flex-1 truncate">{message.shortDescription}</span>
      <button
        onClick={onReset}
        className="flex-shrink-0 p-2 hover:bg-gray-200 rounded-lg transition-colors min-w-[44px] min-h-[44px] flex items-center justify-center"
        aria-label="Retry loading"
        title="Retry"
      >
        <svg className="w-4 h-4 text-gray-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
        </svg>
      </button>
    </div>
  );
}

export default ErrorFallback;
