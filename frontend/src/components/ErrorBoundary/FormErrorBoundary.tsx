/**
 * FormErrorBoundary Component
 * 
 * Specialized error boundary for forms that preserves form data
 * and offers recovery options specific to form context.
 */

import React, { Component, ErrorInfo, ReactNode } from 'react';
import { logError } from '../../services/errorLogging';
import { generateErrorId } from './errorMessages';

interface FormErrorBoundaryProps {
  children: ReactNode;
  formName: string;
  onError?: (error: Error, errorInfo: ErrorInfo) => void;
}

interface FormErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
  preservedData: Record<string, unknown> | null;
}

export class FormErrorBoundary extends Component<FormErrorBoundaryProps, FormErrorBoundaryState> {
  private backupKey: string;

  constructor(props: FormErrorBoundaryProps) {
    super(props);
    this.backupKey = `form_backup_${props.formName}`;
    this.state = {
      hasError: false,
      error: null,
      preservedData: null,
    };
  }

  static getDerivedStateFromError(error: Error): Partial<FormErrorBoundaryState> {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    const { formName, onError } = this.props;

    // Try to recover form data from backup
    const preservedData = this.recoverFormData();

    // Log error with preserved data
    logError({
      error,
      errorInfo,
      boundaryName: `Form: ${formName}`,
      boundaryLevel: 'section',
      preservedData,
    });

    if (onError) {
      onError(error, errorInfo);
    }

    this.setState({ preservedData });
  }

  private recoverFormData(): Record<string, unknown> | null {
    try {
      const backup = localStorage.getItem(this.backupKey);
      if (backup) {
        return JSON.parse(backup);
      }
    } catch {
      // Ignore parse errors
    }
    return null;
  }

  resetError = (): void => {
    this.setState({
      hasError: false,
      error: null,
      preservedData: null,
    });
  };

  copyDataToClipboard = async (): Promise<void> => {
    const { preservedData } = this.state;
    if (preservedData) {
      try {
        await navigator.clipboard.writeText(JSON.stringify(preservedData, null, 2));
        alert('Form data copied to clipboard!');
      } catch {
        // Fallback for older browsers
        const textArea = document.createElement('textarea');
        textArea.value = JSON.stringify(preservedData, null, 2);
        document.body.appendChild(textArea);
        textArea.select();
        document.execCommand('copy');
        document.body.removeChild(textArea);
        alert('Form data copied to clipboard!');
      }
    }
  };

  render(): ReactNode {
    const { hasError, error, preservedData } = this.state;
    const { children, formName } = this.props;

    if (hasError && error) {
      return (
        <div 
          className="bg-gradient-to-br from-amber-50 to-orange-50 border border-amber-300 rounded-xl p-6"
          role="alert"
          aria-live="assertive"
        >
          <div className="flex items-start gap-4">
            <div className="flex-shrink-0">
              <div className="w-12 h-12 bg-amber-100 rounded-full flex items-center justify-center">
                <svg className="w-6 h-6 text-amber-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} 
                    d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" 
                  />
                </svg>
              </div>
            </div>

            <div className="flex-1">
              <h3 className="text-lg font-semibold text-amber-800">
                Error in {formName} Form
              </h3>
              <p className="mt-1 text-sm text-amber-700">
                Something went wrong while processing the form. 
                {preservedData && " Your data has been preserved."}
              </p>

              {/* Preserved data indicator */}
              {preservedData && (
                <div className="mt-4 p-3 bg-white/60 rounded-lg border border-amber-200">
                  <div className="flex items-center gap-2 text-sm text-amber-800">
                    <svg className="w-4 h-4 text-green-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                    </svg>
                    <span className="font-medium">Form data preserved</span>
                  </div>
                  <p className="mt-1 text-xs text-amber-600">
                    {Object.keys(preservedData).length} field(s) saved
                  </p>
                </div>
              )}

              {/* Action buttons */}
              <div className="mt-4 flex flex-wrap gap-3">
                <button
                  onClick={this.resetError}
                  className="inline-flex items-center gap-2 px-4 py-2.5 bg-amber-600 text-white rounded-lg hover:bg-amber-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:ring-offset-2 transition-colors min-h-[44px] font-medium text-sm"
                >
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                  </svg>
                  Try Again
                </button>

                {preservedData && (
                  <button
                    onClick={this.copyDataToClipboard}
                    className="inline-flex items-center gap-2 px-4 py-2.5 bg-white text-amber-700 border border-amber-300 rounded-lg hover:bg-amber-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:ring-offset-2 transition-colors min-h-[44px] font-medium text-sm"
                  >
                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 5H6a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2v-1M8 5a2 2 0 002 2h2a2 2 0 002-2M8 5a2 2 0 012-2h2a2 2 0 012 2m0 0h2a2 2 0 012 2v3m2 4H10m0 0l3-3m-3 3l3 3" />
                    </svg>
                    Copy Data
                  </button>
                )}
              </div>

              {/* Error ID */}
              <p className="mt-4 text-xs text-amber-500 font-mono">
                Error ID: {generateErrorId(error)}
              </p>
            </div>
          </div>
        </div>
      );
    }

    return children;
  }
}

/**
 * Hook to backup form data periodically
 */
export function useFormBackup(formName: string, data: Record<string, unknown>): void {
  React.useEffect(() => {
    const backupKey = `form_backup_${formName}`;
    
    // Save form data
    try {
      localStorage.setItem(backupKey, JSON.stringify(data));
    } catch {
      // Storage quota exceeded or other error
    }

    // Cleanup on unmount
    return () => {
      localStorage.removeItem(backupKey);
    };
  }, [formName, data]);
}

export default FormErrorBoundary;
