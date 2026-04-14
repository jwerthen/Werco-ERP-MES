import React, { createContext, useContext, useState, useEffect, useCallback, ReactNode } from 'react';
import { Company } from '../types';
import { useAuth } from './AuthContext';
import api from '../services/api';

interface CompanyContextType {
  currentCompany: Company | null;
  companies: Company[];
  isPlatformAdmin: boolean;
  isViewingOtherCompany: boolean;
  viewingCompanyName: string | null;
  switchCompany: (companyId: number) => Promise<void>;
  switchBackToHome: () => Promise<void>;
  refreshCompanies: () => Promise<void>;
}

const CompanyContext = createContext<CompanyContextType | undefined>(undefined);

export function CompanyProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const [currentCompany, setCurrentCompany] = useState<Company | null>(null);
  const [companies, setCompanies] = useState<Company[]>([]);
  const [viewingCompanyId, setViewingCompanyId] = useState<number | null>(null);

  const isPlatformAdmin = user?.role === 'platform_admin' || user?.is_superuser === true;

  const refreshCompanies = useCallback(async () => {
    if (!isPlatformAdmin) return;
    try {
      const data = await api.getPlatformCompanies();
      setCompanies(data);
    } catch (e) {
      console.warn('Failed to load companies list');
    }
  }, [isPlatformAdmin]);

  // Load current company info and company list on mount
  useEffect(() => {
    if (!user) {
      setCurrentCompany(null);
      setCompanies([]);
      return;
    }

    const loadCompany = async () => {
      try {
        const company = await api.getCurrentCompany();
        setCurrentCompany(company);
      } catch (e) {
        console.warn('Failed to load current company');
      }
    };

    loadCompany();
    refreshCompanies();
  }, [user, refreshCompanies]);

  const switchCompany = useCallback(async (companyId: number) => {
    if (!isPlatformAdmin) return;

    try {
      const response = await api.switchCompany(companyId);
      // Update tokens
      if (response.refresh_token && response.expires_in) {
        api.setTokens(response.access_token, response.refresh_token, response.expires_in);
      } else {
        api.setToken(response.access_token);
      }
      // Clear cached data (scoped to old company)
      api.clearCache();

      // Track that we're viewing another company
      setViewingCompanyId(companyId);

      // Reload company info
      const company = await api.getCurrentCompany();
      setCurrentCompany(company);
    } catch (e) {
      console.error('Failed to switch company', e);
      throw e;
    }
  }, [isPlatformAdmin]);

  const switchBackToHome = useCallback(async () => {
    if (!user?.company_id) return;
    await switchCompany(user.company_id);
    setViewingCompanyId(null);
  }, [user, switchCompany]);

  const isViewingOtherCompany = viewingCompanyId !== null && viewingCompanyId !== user?.company_id;
  const viewingCompanyName = isViewingOtherCompany
    ? companies.find(c => c.id === viewingCompanyId)?.name || null
    : null;

  return (
    <CompanyContext.Provider value={{
      currentCompany,
      companies,
      isPlatformAdmin,
      isViewingOtherCompany,
      viewingCompanyName,
      switchCompany,
      switchBackToHome,
      refreshCompanies,
    }}>
      {children}
    </CompanyContext.Provider>
  );
}

export function useCompany() {
  const context = useContext(CompanyContext);
  if (context === undefined) {
    throw new Error('useCompany must be used within a CompanyProvider');
  }
  return context;
}
