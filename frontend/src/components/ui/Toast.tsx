import React, { createContext, useCallback, useContext, useState } from 'react';
import { CheckCircleIcon, ExclamationTriangleIcon, InformationCircleIcon, XMarkIcon } from '@heroicons/react/24/outline';
import { toDisplayString } from '../../utils/apiError';

interface Toast {
  id: number;
  /**
   * `warning` is for an action that SUCCEEDED but did not do everything the
   * caller asked — a partial result the user has to act on. It exists because
   * the alternatives both mislead: `success` hides the shortfall, and `error`
   * claims a failure that did not happen (the record was created, and saying
   * otherwise sends someone looking for it in vain). First caller: a duplicated
   * work order that could not carry a material tie across, where believing the
   * copy was complete means releasing a job whose stock is never deducted.
   */
  type: 'success' | 'error' | 'warning' | 'info';
  message: string;
}

interface ToastContextValue {
  showToast: (type: Toast['type'], message: string) => void;
}

const ToastContext = createContext<ToastContextValue>({ showToast: () => {} });

// Module-scoped monotonic counter so toast IDs are guaranteed unique even
// when several toasts fire in the same millisecond. Date.now() + Math.random()
// is collision-prone under bursts.
let nextToastId = 0;

export function useToast() {
  return useContext(ToastContext);
}

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const showToast = useCallback((type: Toast['type'], message: string) => {
    const id = ++nextToastId;
    // Defensive: the toast list renders above the router error boundary, so a
    // non-string message (e.g. a raw 422 detail array slipping past normalization)
    // would blank the whole app. Coerce to a renderable string here so it can't.
    const text = toDisplayString(message);
    setToasts(prev => [...prev, { id, type, message: text }]);
    setTimeout(() => {
      setToasts(prev => prev.filter(t => t.id !== id));
    }, 4000);
  }, []);

  const dismiss = useCallback((id: number) => {
    setToasts(prev => prev.filter(t => t.id !== id));
  }, []);

  const icons = {
    success: <CheckCircleIcon className="h-5 w-5 flex-shrink-0" />,
    error: <ExclamationTriangleIcon className="h-5 w-5 flex-shrink-0" />,
    warning: <ExclamationTriangleIcon className="h-5 w-5 flex-shrink-0" />,
    info: <InformationCircleIcon className="h-5 w-5 flex-shrink-0" />,
  };

  const colors = {
    success: 'bg-green-600',
    error: 'bg-red-600',
    warning: 'bg-amber-600',
    info: 'bg-blue-600',
  };

  return (
    <ToastContext.Provider value={{ showToast }}>
      {children}
      <div
        className="fixed top-4 right-4 z-[100] space-y-2 pointer-events-none"
        aria-live="polite"
        aria-atomic="false"
      >
        {toasts.map(toast => (
          <div
            key={toast.id}
            // `alert` interrupts the screen reader; `status` waits for a pause.
            // A warning earns the interruption for the same reason an error
            // does — it reports something the user has to act on now.
            role={toast.type === 'error' || toast.type === 'warning' ? 'alert' : 'status'}
            className={`${colors[toast.type]} text-white px-4 py-3 rounded-lg shadow-lg flex items-center gap-3 animate-slide-up pointer-events-auto max-w-sm`}
          >
            {icons[toast.type]}
            <span className="text-sm font-medium flex-1 whitespace-pre-line">{toast.message}</span>
            <button onClick={() => dismiss(toast.id)} className="hover:opacity-75" aria-label="Dismiss notification">
              <XMarkIcon className="h-4 w-4" />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
