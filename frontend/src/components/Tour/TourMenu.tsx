import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTour } from '../../context/TourContext';
import { getAllTours, getTour } from '../../data/tours';
import { 
  QuestionMarkCircleIcon, 
  PlayIcon, 
  CheckCircleIcon,
  ArrowPathIcon,
  XMarkIcon
} from '@heroicons/react/24/outline';

export default function TourMenu() {
  const [isOpen, setIsOpen] = useState(false);
  const { startTour, isTourComplete, resetAllTours, isActive } = useTour();
  const navigate = useNavigate();
  const tours = getAllTours();

  const handleStartTour = (tourId: string) => {
    const tour = getTour(tourId);
    console.log('Starting tour:', tourId, 'startPath:', tour?.startPath);
    if (tour) {
      setIsOpen(false);
      
      // Always navigate to the tour's start path
      if (tour.startPath) {
        console.log('Navigating to:', tour.startPath);
        // Navigate even if on the same page (to ensure page is fresh)
        navigate(tour.startPath);
        // Delay tour start to allow page to fully render
        setTimeout(() => {
          console.log('Starting tour after navigation');
          startTour(tour);
        }, 600);
      } else {
        // No start path defined, start immediately
        startTour(tour);
      }
    }
  };

  if (isActive) return null;

  return (
    <div className="relative">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="p-2 rounded-xl text-slate-500 hover:text-slate-700 hover:bg-slate-100 transition-colors"
        aria-label="Help & Tours"
        data-tour="help-menu"
      >
        <QuestionMarkCircleIcon className="h-5 w-5" />
      </button>

      {isOpen && (
        <>
          {/* Backdrop */}
          <div 
            className="fixed inset-0 z-40" 
            onClick={() => setIsOpen(false)} 
          />
          
          {/* Menu */}
          <div className="absolute right-0 top-full mt-2 w-80 bg-white rounded-2xl shadow-2xl border border-slate-200 overflow-hidden z-50 animate-slide-down">
            <div className="bg-gradient-to-r from-cyan-500 to-cyan-600 px-5 py-4 flex items-center justify-between">
              <div>
                <h3 className="text-white font-bold">Help & Tours</h3>
                <p className="text-white/70 text-sm">Learn how to use the system</p>
              </div>
              <button
                onClick={() => setIsOpen(false)}
                className="text-white/70 hover:text-white p-1 rounded-lg hover:bg-white/10"
              >
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>

            <div className="p-3 max-h-80 overflow-y-auto">
              {tours.map((tour) => {
                const isComplete = isTourComplete(tour.id);
                return (
                  <button
                    key={tour.id}
                    onClick={() => handleStartTour(tour.id)}
                    className="w-full flex items-start gap-3 p-3 rounded-xl hover:bg-slate-50 transition-colors text-left group"
                  >
                    <div className={`flex-shrink-0 w-10 h-10 rounded-xl flex items-center justify-center ${
                      isComplete 
                        ? 'bg-emerald-100 text-emerald-600' 
                        : 'bg-cyan-100 text-cyan-600 group-hover:bg-cyan-200'
                    }`}>
                      {isComplete ? (
                        <CheckCircleIcon className="h-5 w-5" />
                      ) : (
                        <PlayIcon className="h-5 w-5" />
                      )}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="font-medium text-slate-800">{tour.name}</span>
                        {isComplete && (
                          <span className="text-xs text-emerald-600 bg-emerald-50 px-2 py-0.5 rounded-full">
                            Completed
                          </span>
                        )}
                      </div>
                      <p className="text-sm text-slate-500 mt-0.5 line-clamp-2">
                        {tour.description}
                      </p>
                      <p className="text-xs text-slate-400 mt-1">
                        {tour.steps.length} steps
                      </p>
                    </div>
                  </button>
                );
              })}
            </div>

            <div className="border-t border-slate-100 p-3">
              <button
                onClick={() => {
                  resetAllTours();
                  setIsOpen(false);
                }}
                className="w-full flex items-center justify-center gap-2 px-4 py-2.5 text-sm text-slate-600 hover:text-slate-800 hover:bg-slate-100 rounded-xl transition-colors"
              >
                <ArrowPathIcon className="h-4 w-4" />
                Reset All Tours
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
