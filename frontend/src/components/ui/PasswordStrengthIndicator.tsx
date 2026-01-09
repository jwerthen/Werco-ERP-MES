import React, { useMemo } from 'react';
import { CheckCircleIcon, XCircleIcon } from '@heroicons/react/24/solid';
import { calculatePasswordStrength } from '../../validation/schemas';

interface PasswordStrengthIndicatorProps {
  password: string;
  showRequirements?: boolean;
}

export default function PasswordStrengthIndicator({ 
  password, 
  showRequirements = true 
}: PasswordStrengthIndicatorProps) {
  const strength = useMemo(() => calculatePasswordStrength(password), [password]);

  if (!password) return null;

  const colorClasses = {
    red: 'bg-red-500',
    yellow: 'bg-yellow-500',
    blue: 'bg-blue-500',
    green: 'bg-green-500',
  };

  const textColorClasses = {
    red: 'text-red-600',
    yellow: 'text-yellow-600',
    blue: 'text-blue-600',
    green: 'text-green-600',
  };

  return (
    <div className="mt-2 space-y-2">
      {/* Strength bar */}
      <div className="flex items-center gap-2">
        <div className="flex-1 h-2 bg-gray-200 rounded-full overflow-hidden">
          <div
            className={`h-full transition-all duration-300 ${colorClasses[strength.color as keyof typeof colorClasses]}`}
            style={{ width: `${strength.score}%` }}
          />
        </div>
        <span className={`text-sm font-medium ${textColorClasses[strength.color as keyof typeof textColorClasses]}`}>
          {strength.label}
        </span>
      </div>

      {/* Requirements checklist */}
      {showRequirements && (
        <div className="grid grid-cols-2 gap-1 text-xs">
          {strength.requirements.map((req, index) => (
            <div key={index} className="flex items-center gap-1">
              {req.met ? (
                <CheckCircleIcon className="h-4 w-4 text-green-500" />
              ) : (
                <XCircleIcon className="h-4 w-4 text-gray-300" />
              )}
              <span className={req.met ? 'text-green-700' : 'text-gray-500'}>
                {req.label}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
