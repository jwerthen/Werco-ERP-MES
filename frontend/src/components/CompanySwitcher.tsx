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
    } catch {
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
    } catch {
      console.error('Failed to switch back');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className={`flex items-center gap-2 h-[34px] px-3 rounded-[3px] text-xs font-mono tracking-[0.04em] transition-colors ${
          isViewingOtherCompany
            ? 'bg-fd-amber/15 text-fd-amber border border-fd-amber/40'
            : 'text-fd-mute hover:text-fd-body border border-fd-line'
        }`}
        style={isViewingOtherCompany ? undefined : { background: 'var(--fd-sunken)' }}
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
        <div
          className="absolute right-0 mt-2 w-64 rounded-sm shadow-2xl z-50"
          style={{ background: 'var(--fd-panel)', border: '1px solid var(--fd-line-bright)' }}
        >
          <div className="p-2">
            <p className="text-[10px] font-mono uppercase tracking-[0.14em] text-fd-faint px-2 py-1.5">Switch Company</p>
            {companies.map((company) => (
              <button
                key={company.id}
                onClick={() => handleSwitch(company.id)}
                className={`w-full text-left px-3 py-2 rounded-[3px] text-sm transition-colors ${
                  currentCompany?.id === company.id
                    ? 'bg-[rgba(47,129,247,0.1)] text-fd-ink font-medium shadow-[inset_2px_0_0_#2f81f7]'
                    : 'text-fd-body hover:bg-white/[0.03]'
                }`}
                disabled={isLoading}
              >
                <div className="flex items-center justify-between">
                  <span className="truncate">{company.name}</span>
                  {currentCompany?.id === company.id && (
                    <span className="text-[10px] font-mono uppercase tracking-wide text-fd-blue ml-2">Active</span>
                  )}
                </div>
                <div className="text-[11px] font-mono text-fd-mute mt-0.5 tabular-nums">
                  {company.user_count || 0} users · {company.active_work_orders || 0} active WOs
                </div>
              </button>
            ))}
          </div>
          {isViewingOtherCompany && (
            <div className="p-2" style={{ borderTop: '1px solid var(--fd-line)' }}>
              <button
                onClick={handleBackToHome}
                className="w-full text-left px-3 py-2 rounded-[3px] text-sm text-fd-amber hover:bg-white/[0.03] transition-colors"
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
