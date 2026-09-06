import { createContext, useContext } from 'react';

// Popups stay inside the estimator's theme without altering the ERP's styles.
export const NestingPortalContext = createContext<HTMLElement | null>(null);
export const useNestingPortal = () => useContext(NestingPortalContext);
