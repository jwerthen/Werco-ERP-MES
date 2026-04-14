import React, { useState, useRef, useEffect } from 'react';
import { useCompany } from '../context/CompanyContext';

export default function CompanySwitcher() {
  const { companies, currentCompany, isPlatformAdmin, isViewingOtherCompany, switchCompany, switchBackToHome } = useCompany();
  const [isOpen, setIsOpen] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  // Close dropdown when clicking outside
  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  if (!isPlatformAdmin || companies.length <= 1) return null;

  const handleSwitch = async (companyId: number) => {
    setIsLoading(true);
    try {
      await switchCompany(companyId);
      setIsOpen(false);
      // Reload the page to refresh all data
      window.location.reload();
    } catch (e) {
      console.error('Failed to switch company');
    } finally {
      setIsLoading(false);
    }
  };

  const handleBackToHome = async () => {
    setIsLoading(true);
    try {
      await switchBackToHome();
      setIsOpen(false);
      window.location.reload();
    } catch (e) {
      console.error('Failed to switch back');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
          isViewingOtherCompany
            ? 'bg-amber-500/20 text-amber-300 border border-amber-500/30'
            : 'bg-base-300/50 text-base-content/70 hover:bg-base-300'
        }`}
        disabled={isLoading}
      >
        <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
          <path fillRule="evenodd" d="M4 4a2 2 0 012-2h8a2 2 0 012 2v12a1 1 0 01-1 1h-2a1 1 0 01-1-1v-2a1 1 0 00-1-1H9a1 1 0 00-1 1v2a1 1 0 01-1 1H5a1 1 0 01-1-1V4zm3 1h2v2H7V5zm2 4H7v2h2V9zm2-4h2v2h-2V5zm2 4h-2v2h2V9z" clipRule="evenodd" />
        </svg>
        <span className="max-w-[150px] truncate">{currentCompany?.name || 'Company'}</span>
        <svg xmlns="http://www.w3.org/2000/svg" className="h-3 w-3" viewBox="0 0 20 20" fill="currentColor">
          <path fillRule="evenodd" d="M5.293 7.293a1 1 0 011.414 0L10 10.586l3.293-3.293a1 1 0 111.414 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 010-1.414z" clipRule="evenodd" />
        </svg>
      </button>

      {isOpen && (
        <div className="absolute right-0 mt-2 w-64 bg-base-200 border border-base-300 rounded-lg shadow-xl z-50">
          <div className="p-2">
            <p className="text-xs text-base-content/50 px-2 py-1 uppercase tracking-wider">Switch Company</p>
            {companies.map((company) => (
              <button
                key={company.id}
                onClick={() => handleSwitch(company.id)}
                className={`w-full text-left px-3 py-2 rounded-md text-sm transition-colors ${
                  currentCompany?.id === company.id
                    ? 'bg-primary/20 text-primary font-medium'
                    : 'hover:bg-base-300 text-base-content/80'
                }`}
                disabled={isLoading}
              >
                <div className="flex items-center justify-between">
                  <span className="truncate">{company.name}</span>
                  {currentCompany?.id === company.id && (
                    <span className="text-xs text-primary ml-2">Active</span>
                  )}
                </div>
                <div className="text-xs text-base-content/50 mt-0.5">
                  {company.user_count || 0} users · {company.active_work_orders || 0} active WOs
                </div>
              </button>
            ))}
          </div>
          {isViewingOtherCompany && (
            <div className="border-t border-base-300 p-2">
              <button
                onClick={handleBackToHome}
                className="w-full text-left px-3 py-2 rounded-md text-sm text-amber-400 hover:bg-base-300 transition-colors"
                disabled={isLoading}
              >
                Back to home company
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
