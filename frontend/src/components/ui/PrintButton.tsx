/**
 * PrintButton Component
 * 
 * A button that triggers the browser's print dialog.
 * Optionally can show a print preview or target specific content.
 */

import React, { useCallback } from 'react';

interface PrintButtonProps {
  /** Button text (default: "Print") */
  label?: string;
  /** Additional CSS classes */
  className?: string;
  /** Icon to show (default: printer icon) */
  icon?: React.ReactNode;
  /** Selector for content to print (optional, prints whole page if not specified) */
  contentSelector?: string;
  /** Callback before printing */
  onBeforePrint?: () => void;
  /** Callback after printing */
  onAfterPrint?: () => void;
  /** Show print preview in new window instead of printing */
  preview?: boolean;
  /** Document title for print */
  documentTitle?: string;
  /** Variant style */
  variant?: 'primary' | 'secondary' | 'outline' | 'ghost';
  /** Size */
  size?: 'sm' | 'md' | 'lg';
  /** Disabled state */
  disabled?: boolean;
}

const PrintIcon = () => (
  <svg 
    className="h-4 w-4" 
    fill="none" 
    stroke="currentColor" 
    viewBox="0 0 24 24"
    aria-hidden="true"
  >
    <path 
      strokeLinecap="round" 
      strokeLinejoin="round" 
      strokeWidth={2} 
      d="M17 17h2a2 2 0 002-2v-4a2 2 0 00-2-2H5a2 2 0 00-2 2v4a2 2 0 002 2h2m2 4h6a2 2 0 002-2v-4a2 2 0 00-2-2H9a2 2 0 00-2 2v4a2 2 0 002 2zm8-12V5a2 2 0 00-2-2H9a2 2 0 00-2 2v4h10z" 
    />
  </svg>
);

const variantStyles = {
  primary: 'bg-cyan-600 hover:bg-cyan-700 text-white shadow-sm',
  secondary: 'bg-gray-100 hover:bg-gray-200 text-gray-700',
  outline: 'border border-gray-300 hover:bg-gray-50 text-gray-700',
  ghost: 'hover:bg-gray-100 text-gray-600',
};

const sizeStyles = {
  sm: 'px-2 py-1 text-xs',
  md: 'px-3 py-2 text-sm',
  lg: 'px-4 py-2 text-base',
};

export function PrintButton({
  label = 'Print',
  className = '',
  icon,
  contentSelector,
  onBeforePrint,
  onAfterPrint,
  preview = false,
  documentTitle,
  variant = 'outline',
  size = 'md',
  disabled = false,
}: PrintButtonProps) {
  const handlePrint = useCallback(() => {
    if (disabled) return;

    // Call before print callback
    onBeforePrint?.();

    if (preview) {
      // Open print preview in new window
      const printWindow = window.open('', '_blank');
      if (!printWindow) {
        alert('Please allow popups to use print preview');
        return;
      }

      const content = contentSelector 
        ? document.querySelector(contentSelector)?.innerHTML 
        : document.body.innerHTML;

      printWindow.document.write(`
        <!DOCTYPE html>
        <html>
          <head>
            <title>${documentTitle || 'Print Preview'}</title>
            <link rel="stylesheet" href="${window.location.origin}/static/css/main.css">
            <style>
              body { padding: 20px; }
              @media print { body { padding: 0; } }
            </style>
          </head>
          <body class="print-preview">
            ${content}
          </body>
        </html>
      `);
      printWindow.document.close();
      
      // Wait for styles to load, then print
      printWindow.onload = () => {
        printWindow.print();
        onAfterPrint?.();
      };
    } else {
      // Direct print
      if (documentTitle) {
        const originalTitle = document.title;
        document.title = documentTitle;
        window.print();
        document.title = originalTitle;
      } else {
        window.print();
      }
      onAfterPrint?.();
    }
  }, [contentSelector, disabled, documentTitle, onAfterPrint, onBeforePrint, preview]);

  return (
    <button
      onClick={handlePrint}
      disabled={disabled}
      className={`
        inline-flex items-center gap-2 rounded-lg font-medium
        transition-colors duration-200
        focus:outline-none focus:ring-2 focus:ring-cyan-500 focus:ring-offset-2
        disabled:opacity-50 disabled:cursor-not-allowed
        ${variantStyles[variant]}
        ${sizeStyles[size]}
        ${className}
      `}
      aria-label={label}
    >
      {icon || <PrintIcon />}
      <span>{label}</span>
    </button>
  );
}

export default PrintButton;
