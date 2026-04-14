import React from 'react';
import { useCompany } from '../context/CompanyContext';

export default function ReadOnlyBanner() {
  const { isViewingOtherCompany, viewingCompanyName, switchBackToHome } = useCompany();

  if (!isViewingOtherCompany) return null;

  return (
    <div className="bg-amber-500/10 border-b border-amber-500/20 px-4 py-2 flex items-center justify-between">
      <div className="flex items-center gap-2 text-amber-300 text-sm">
        <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
          <path d="M10 12a2 2 0 100-4 2 2 0 000 4z" />
          <path fillRule="evenodd" d="M.458 10C1.732 5.943 5.522 3 10 3s8.268 2.943 9.542 7c-1.274 4.057-5.064 7-9.542 7S1.732 14.057.458 10zM14 10a4 4 0 11-8 0 4 4 0 018 0z" clipRule="evenodd" />
        </svg>
        <span>
          Viewing <strong>{viewingCompanyName}</strong> — Read Only
        </span>
      </div>
      <button
        onClick={switchBackToHome}
        className="text-xs text-amber-400 hover:text-amber-300 underline transition-colors"
      >
        Return to home company
      </button>
    </div>
  );
}
