import React, { createContext, useContext, useState, useCallback, ReactNode } from 'react';

export interface TourStep {
  target: string;
  title: string;
  description: string;
  position?: 'top' | 'bottom' | 'left' | 'right' | 'auto';
  path?: string;  // Optional path to navigate to for this step
}

export interface Tour {
  id: string;
  name: string;
  description: string;
  startPath?: string;  // Path to navigate to before starting the tour
  steps: TourStep[];
}

interface TourContextType {
  activeTour: Tour | null;
  currentStepIndex: number;
  isActive: boolean;
  completedTours: string[];
  startTour: (tour: Tour) => void;
  endTour: () => void;
  nextStep: () => void;
  prevStep: () => void;
  goToStep: (index: number) => void;
  markTourComplete: (tourId: string) => void;
  isTourComplete: (tourId: string) => boolean;
  resetAllTours: () => void;
}

const TourContext = createContext<TourContextType | undefined>(undefined);

const STORAGE_KEY = 'werco-completed-tours';

export function TourProvider({ children }: { children: ReactNode }) {
  const [activeTour, setActiveTour] = useState<Tour | null>(null);
  const [currentStepIndex, setCurrentStepIndex] = useState(0);
  const [completedTours, setCompletedTours] = useState<string[]>(() => {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      return saved ? JSON.parse(saved) : [];
    } catch {
      return [];
    }
  });

  const isActive = activeTour !== null;

  const startTour = useCallback((tour: Tour) => {
    setActiveTour(tour);
    setCurrentStepIndex(0);
  }, []);

  const endTour = useCallback(() => {
    setActiveTour(null);
    setCurrentStepIndex(0);
  }, []);

  const nextStep = useCallback(() => {
    if (activeTour && currentStepIndex < activeTour.steps.length - 1) {
      setCurrentStepIndex(prev => prev + 1);
    } else if (activeTour) {
      markTourComplete(activeTour.id);
      endTour();
    }
  }, [activeTour, currentStepIndex]);

  const prevStep = useCallback(() => {
    if (currentStepIndex > 0) {
      setCurrentStepIndex(prev => prev - 1);
    }
  }, [currentStepIndex]);

  const goToStep = useCallback((index: number) => {
    if (activeTour && index >= 0 && index < activeTour.steps.length) {
      setCurrentStepIndex(index);
    }
  }, [activeTour]);

  const markTourComplete = useCallback((tourId: string) => {
    setCompletedTours(prev => {
      if (prev.includes(tourId)) return prev;
      const updated = [...prev, tourId];
      localStorage.setItem(STORAGE_KEY, JSON.stringify(updated));
      return updated;
    });
  }, []);

  const isTourComplete = useCallback((tourId: string) => {
    return completedTours.includes(tourId);
  }, [completedTours]);

  const resetAllTours = useCallback(() => {
    setCompletedTours([]);
    localStorage.removeItem(STORAGE_KEY);
  }, []);

  return (
    <TourContext.Provider value={{
      activeTour,
      currentStepIndex,
      isActive,
      completedTours,
      startTour,
      endTour,
      nextStep,
      prevStep,
      goToStep,
      markTourComplete,
      isTourComplete,
      resetAllTours,
    }}>
      {children}
    </TourContext.Provider>
  );
}

export function useTour() {
  const context = useContext(TourContext);
  if (context === undefined) {
    throw new Error('useTour must be used within a TourProvider');
  }
  return context;
}
