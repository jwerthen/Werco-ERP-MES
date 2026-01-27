/**
 * Unauthorized Access Page
 * 
 * Displayed when a user tries to access a resource they don't have permission for.
 */

import React from 'react';
import { useNavigate } from 'react-router-dom';
import { usePermissions } from '../hooks/usePermissions';
import { ROLE_LABELS, ROLE_DESCRIPTIONS } from '../utils/permissions';

export default function Unauthorized() {
  const navigate = useNavigate();
  const { role } = usePermissions();
  
  return (
    <div className="min-h-screen bg-gray-100 flex flex-col items-center justify-center px-4">
      <div className="max-w-md w-full bg-white rounded-lg shadow-lg p-8 text-center">
        {/* Icon */}
        <div className="mx-auto w-16 h-16 bg-red-100 rounded-full flex items-center justify-center mb-6">
          <svg
            className="w-8 h-8 text-red-600"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"
            />
          </svg>
        </div>
        
        {/* Title */}
        <h1 className="text-2xl font-bold text-gray-900 mb-2">
          Access Denied
        </h1>
        
        {/* Message */}
        <p className="text-gray-600 mb-6">
          You don't have permission to access this page.
        </p>
        
        {/* User Role Info */}
        {role && (
          <div className="bg-gray-50 rounded-lg p-4 mb-6 text-left">
            <p className="text-sm text-gray-500 mb-1">Your current role:</p>
            <p className="font-medium text-gray-900">{ROLE_LABELS[role]}</p>
            <p className="text-sm text-gray-600 mt-1">{ROLE_DESCRIPTIONS[role]}</p>
          </div>
        )}
        
        {/* Actions */}
        <div className="flex flex-col sm:flex-row gap-3 justify-center">
          <button
            onClick={() => navigate(-1)}
            className="px-4 py-2 border border-gray-300 rounded-lg text-gray-700 hover:bg-gray-50 transition-colors"
          >
            Go Back
          </button>
          <button
            onClick={() => navigate('/')}
            className="px-4 py-2 bg-cyan-600 text-white rounded-lg hover:bg-cyan-700 transition-colors"
          >
            Go to Dashboard
          </button>
        </div>
        
        {/* Contact Admin */}
        <p className="text-sm text-gray-500 mt-6">
          If you believe you should have access, please contact your administrator.
        </p>
      </div>
    </div>
  );
}
