import React, { useEffect, useState } from 'react';
import { useTour, TourStep } from '../../context/TourContext';
import { XMarkIcon, ChevronLeftIcon, ChevronRightIcon } from '@heroicons/react/24/outline';

interface TargetRect {
  top: number;
  left: number;
  width: number;
  height: number;
}

interface TourTooltipProps {
  step: TourStep;
  targetRect: TargetRect | null;
  stepIndex: number;
  totalSteps: number;
}

export default function TourTooltip({ step, targetRect, stepIndex, totalSteps }: TourTooltipProps) {
  const { nextStep, prevStep, endTour } = useTour();
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const [placement, setPlacement] = useState<'top' | 'bottom' | 'left' | 'right'>('bottom');

  const tooltipWidth = 340;
  const tooltipHeight = 200; // Approximate
  const gap = 16;

  useEffect(() => {
    if (!targetRect) {
      // Center in viewport when no target
      setPosition({
        top: window.innerHeight / 2 - tooltipHeight / 2,
        left: window.innerWidth / 2 - tooltipWidth / 2,
      });
      return;
    }

    const viewport = {
      width: window.innerWidth,
      height: window.innerHeight,
    };

    // Calculate best position based on available space
    let bestPlacement = step.position || 'auto';
    
    if (bestPlacement === 'auto') {
      const spaceTop = targetRect.top;
      const spaceBottom = viewport.height - (targetRect.top + targetRect.height);
      const spaceLeft = targetRect.left;
      const spaceRight = viewport.width - (targetRect.left + targetRect.width);

      if (spaceBottom >= tooltipHeight + gap) {
        bestPlacement = 'bottom';
      } else if (spaceTop >= tooltipHeight + gap) {
        bestPlacement = 'top';
      } else if (spaceRight >= tooltipWidth + gap) {
        bestPlacement = 'right';
      } else if (spaceLeft >= tooltipWidth + gap) {
        bestPlacement = 'left';
      } else {
        bestPlacement = 'bottom';
      }
    }

    let newTop = 0;
    let newLeft = 0;

    switch (bestPlacement) {
      case 'top':
        newTop = targetRect.top - tooltipHeight - gap - 8;
        newLeft = targetRect.left + targetRect.width / 2 - tooltipWidth / 2;
        break;
      case 'bottom':
        newTop = targetRect.top + targetRect.height + gap + 8;
        newLeft = targetRect.left + targetRect.width / 2 - tooltipWidth / 2;
        break;
      case 'left':
        newTop = targetRect.top + targetRect.height / 2 - tooltipHeight / 2;
        newLeft = targetRect.left - tooltipWidth - gap - 8;
        break;
      case 'right':
        newTop = targetRect.top + targetRect.height / 2 - tooltipHeight / 2;
        newLeft = targetRect.left + targetRect.width + gap + 8;
        break;
    }

    // Keep tooltip within viewport bounds
    newLeft = Math.max(16, Math.min(newLeft, viewport.width - tooltipWidth - 16));
    newTop = Math.max(16, Math.min(newTop, viewport.height - tooltipHeight - 16));

    setPosition({ top: newTop, left: newLeft });
    setPlacement(bestPlacement as 'top' | 'bottom' | 'left' | 'right');
  }, [targetRect, step.position]);

  const isFirstStep = stepIndex === 0;
  const isLastStep = stepIndex === totalSteps - 1;

  return (
    <div
      className="absolute pointer-events-auto bg-white rounded-2xl shadow-2xl border border-slate-200 overflow-hidden animate-fade-in"
      style={{
        top: position.top,
        left: position.left,
        width: tooltipWidth,
        zIndex: 10000,
      }}
    >
      {/* Header */}
      <div className="bg-gradient-to-r from-cyan-500 to-cyan-600 px-5 py-4 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-lg bg-white/20 flex items-center justify-center text-white font-bold text-sm">
            {stepIndex + 1}
          </div>
          <span className="text-white/80 text-sm font-medium">
            of {totalSteps}
          </span>
        </div>
        <button
          onClick={endTour}
          className="text-white/80 hover:text-white p-1 rounded-lg hover:bg-white/10 transition-colors"
          aria-label="Close tour"
        >
          <XMarkIcon className="h-5 w-5" />
        </button>
      </div>

      {/* Content */}
      <div className="p-5">
        <h3 className="text-lg font-bold text-slate-800 mb-2">
          {step.title}
        </h3>
        <p className="text-slate-600 text-sm leading-relaxed">
          {step.description}
        </p>
      </div>

      {/* Footer */}
      <div className="px-5 pb-5 flex items-center justify-between">
        {/* Step dots */}
        <div className="flex gap-1.5">
          {Array.from({ length: totalSteps }).map((_, i) => (
            <div
              key={i}
              className={`w-2 h-2 rounded-full transition-colors ${
                i === stepIndex ? 'bg-cyan-500' : 'bg-slate-200'
              }`}
            />
          ))}
        </div>

        {/* Navigation buttons */}
        <div className="flex items-center gap-2">
          {!isFirstStep && (
            <button
              onClick={prevStep}
              className="flex items-center gap-1 px-3 py-2 text-sm font-medium text-slate-600 hover:text-slate-800 hover:bg-slate-100 rounded-lg transition-colors"
            >
              <ChevronLeftIcon className="h-4 w-4" />
              Back
            </button>
          )}
          <button
            onClick={nextStep}
            className="flex items-center gap-1 px-4 py-2 text-sm font-medium text-white bg-cyan-500 hover:bg-cyan-600 rounded-lg transition-colors shadow-sm"
          >
            {isLastStep ? 'Finish' : 'Next'}
            {!isLastStep && <ChevronRightIcon className="h-4 w-4" />}
          </button>
        </div>
      </div>
    </div>
  );
}
