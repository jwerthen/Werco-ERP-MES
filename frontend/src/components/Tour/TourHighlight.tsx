import React, { useEffect, useState, useCallback, useRef } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { createPortal } from 'react-dom';
import { useTour } from '../../context/TourContext';
import TourTooltip from './TourTooltip';

interface TargetRect {
  top: number;
  left: number;
  width: number;
  height: number;
}

export default function TourHighlight() {
  const { activeTour, currentStepIndex, isActive } = useTour();
  const [targetRect, setTargetRect] = useState<TargetRect | null>(null);
  const [targetElement, setTargetElement] = useState<Element | null>(null);
  const navigate = useNavigate();
  const location = useLocation();
  const lastTargetRef = useRef<Element | null>(null);

  const currentStep = activeTour?.steps[currentStepIndex];

  const updateTargetPosition = useCallback(() => {
    if (!currentStep) {
      setTargetRect(null);
      setTargetElement(null);
      return;
    }

    if (currentStep.path && location.pathname !== currentStep.path) {
      setTargetRect(null);
      setTargetElement(null);
      return;
    }

    const element = document.querySelector(currentStep.target);
    if (element) {
      const rect = element.getBoundingClientRect();
      setTargetRect({
        top: rect.top,
        left: rect.left,
        width: rect.width,
        height: rect.height,
      });
      setTargetElement(element);
      
      // Scroll element into view if needed
      if (lastTargetRef.current !== element) {
        lastTargetRef.current = element;
        element.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    } else {
      setTargetRect(null);
      setTargetElement(null);
      lastTargetRef.current = null;
    }
  }, [currentStep, location.pathname]);

  useEffect(() => {
    if (!isActive) {
      setTargetRect(null);
      setTargetElement(null);
      return;
    }

    if (currentStep?.path && location.pathname !== currentStep.path) {
      navigate(currentStep.path);
      return;
    }

    // Initial position update
    updateTargetPosition();

    // Update on resize/scroll
    const handleUpdate = () => {
      requestAnimationFrame(updateTargetPosition);
    };

    window.addEventListener('resize', handleUpdate);
    window.addEventListener('scroll', handleUpdate, true);

    // Observe DOM changes
    const observer = new MutationObserver(handleUpdate);
    observer.observe(document.body, { childList: true, subtree: true });

    return () => {
      window.removeEventListener('resize', handleUpdate);
      window.removeEventListener('scroll', handleUpdate, true);
      observer.disconnect();
    };
  }, [currentStep?.path, isActive, location.pathname, navigate, updateTargetPosition]);

  if (!isActive || !currentStep) return null;

  const padding = 8;
  const borderRadius = 12;

  return createPortal(
    <div className="fixed inset-0 z-[9999] pointer-events-none">
      {/* Dark overlay with cutout */}
      <svg className="absolute inset-0 w-full h-full pointer-events-auto">
        <defs>
          <mask id="tour-spotlight-mask">
            <rect x="0" y="0" width="100%" height="100%" fill="white" />
            {targetRect && (
              <rect
                x={targetRect.left - padding}
                y={targetRect.top - padding}
                width={targetRect.width + padding * 2}
                height={targetRect.height + padding * 2}
                rx={borderRadius}
                ry={borderRadius}
                fill="black"
              />
            )}
          </mask>
        </defs>
        <rect
          x="0"
          y="0"
          width="100%"
          height="100%"
          fill="rgba(15, 23, 42, 0.75)"
          mask="url(#tour-spotlight-mask)"
        />
      </svg>

      {/* Spotlight border/glow */}
      {targetRect && (
        <div
          className="absolute border-2 border-cyan-400 rounded-xl pointer-events-none"
          style={{
            top: targetRect.top - padding,
            left: targetRect.left - padding,
            width: targetRect.width + padding * 2,
            height: targetRect.height + padding * 2,
            boxShadow: '0 0 0 4px rgba(6, 182, 212, 0.3), 0 0 30px rgba(6, 182, 212, 0.4)',
            transition: 'all 0.3s ease-out',
          }}
        />
      )}

      {/* Tooltip */}
      {targetRect && currentStep && (
        <TourTooltip
          step={currentStep}
          targetRect={targetRect}
          stepIndex={currentStepIndex}
          totalSteps={activeTour?.steps.length || 0}
        />
      )}

      {/* Fallback when target not found */}
      {!targetRect && currentStep && (
        <div className="fixed inset-0 flex items-center justify-center pointer-events-auto">
          <TourTooltip
            step={currentStep}
            targetRect={null}
            stepIndex={currentStepIndex}
            totalSteps={activeTour?.steps.length || 0}
          />
        </div>
      )}
    </div>,
    document.body
  );
}
