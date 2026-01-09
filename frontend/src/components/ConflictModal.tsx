/**
 * ConflictModal Component
 * 
 * Displays when a version conflict is detected during save.
 * Shows diff between user's changes and server's current state.
 * Allows user to choose: keep theirs, keep mine, or merge.
 */

import React from 'react';
import { ConflictData, formatFieldName, formatValue, getChangedFields } from '../utils/optimisticLock';

interface ConflictModalProps<T extends Record<string, unknown>> {
  conflict: ConflictData<T>;
  originalData: T;
  onResolve: (resolution: 'mine' | 'theirs' | 'merge') => void;
  onCancel: () => void;
  entityName?: string;
}

export function ConflictModal<T extends Record<string, unknown>>({
  conflict,
  originalData,
  onResolve,
  onCancel,
  entityName = 'record'
}: ConflictModalProps<T>) {
  const yourChanges = conflict.submitted_changes;
  const serverData = conflict.current_data;
  
  // Get fields that were changed by the user
  const yourChangedFields = Object.keys(yourChanges).filter(
    key => key !== 'version' && yourChanges[key as keyof T] !== undefined
  );
  
  // Get fields that were changed on the server
  const serverChangedFields = getChangedFields(originalData, serverData);
  
  // Fields that both changed (potential conflicts)
  const conflictingFields = yourChangedFields.filter(field => 
    serverChangedFields.includes(field as keyof T)
  );
  
  // Fields only you changed
  const onlyYourFields = yourChangedFields.filter(
    field => !serverChangedFields.includes(field as keyof T)
  );
  
  // Fields only server changed
  const onlyServerFields = serverChangedFields.filter(
    field => !yourChangedFields.includes(field as string)
  );

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50">
      <div className="bg-white rounded-xl shadow-2xl max-w-2xl w-full max-h-[90vh] overflow-hidden">
        {/* Header */}
        <div className="bg-gradient-to-r from-amber-500 to-orange-500 px-6 py-4">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-white/20 rounded-lg">
              <svg className="w-6 h-6 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} 
                  d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" 
                />
              </svg>
            </div>
            <div>
              <h2 className="text-xl font-bold text-white">Conflict Detected</h2>
              <p className="text-white/80 text-sm">
                This {entityName} was modified while you were editing
              </p>
            </div>
          </div>
        </div>

        {/* Content */}
        <div className="p-6 overflow-y-auto max-h-[60vh]">
          {/* Info message */}
          <div className="bg-amber-50 border border-amber-200 rounded-lg p-4 mb-6">
            <p className="text-amber-800 text-sm">
              <strong>Your version:</strong> {conflict.submitted_version} &rarr; 
              <strong> Current version:</strong> {conflict.current_version}
              {conflict.updated_at && (
                <span className="ml-2 text-amber-600">
                  (Updated {new Date(conflict.updated_at).toLocaleString()})
                </span>
              )}
            </p>
          </div>

          {/* Conflicting fields - both changed */}
          {conflictingFields.length > 0 && (
            <div className="mb-6">
              <h3 className="text-sm font-semibold text-red-600 uppercase tracking-wide mb-3 flex items-center gap-2">
                <span className="w-2 h-2 bg-red-500 rounded-full"></span>
                Conflicting Changes
              </h3>
              <div className="space-y-3">
                {conflictingFields.map(field => (
                  <div key={field} className="bg-red-50 border border-red-200 rounded-lg p-3">
                    <div className="font-medium text-gray-900 mb-2">
                      {formatFieldName(field)}
                    </div>
                    <div className="grid grid-cols-2 gap-4 text-sm">
                      <div>
                        <span className="text-red-600 font-medium">Your value:</span>
                        <div className="mt-1 p-2 bg-white rounded border border-red-200">
                          {formatValue(yourChanges[field as keyof T])}
                        </div>
                      </div>
                      <div>
                        <span className="text-blue-600 font-medium">Server value:</span>
                        <div className="mt-1 p-2 bg-white rounded border border-blue-200">
                          {formatValue(serverData[field as keyof T])}
                        </div>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Your non-conflicting changes */}
          {onlyYourFields.length > 0 && (
            <div className="mb-6">
              <h3 className="text-sm font-semibold text-green-600 uppercase tracking-wide mb-3 flex items-center gap-2">
                <span className="w-2 h-2 bg-green-500 rounded-full"></span>
                Your Changes (No Conflict)
              </h3>
              <div className="bg-green-50 border border-green-200 rounded-lg p-3">
                <div className="space-y-2">
                  {onlyYourFields.map(field => (
                    <div key={field} className="flex justify-between text-sm">
                      <span className="text-gray-600">{formatFieldName(field)}:</span>
                      <span className="font-medium text-gray-900">
                        {formatValue(yourChanges[field as keyof T])}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}

          {/* Server's non-conflicting changes */}
          {onlyServerFields.length > 0 && (
            <div className="mb-6">
              <h3 className="text-sm font-semibold text-blue-600 uppercase tracking-wide mb-3 flex items-center gap-2">
                <span className="w-2 h-2 bg-blue-500 rounded-full"></span>
                Other User's Changes
              </h3>
              <div className="bg-blue-50 border border-blue-200 rounded-lg p-3">
                <div className="space-y-2">
                  {onlyServerFields.map(field => (
                    <div key={String(field)} className="flex justify-between text-sm">
                      <span className="text-gray-600">{formatFieldName(String(field))}:</span>
                      <span className="font-medium text-gray-900">
                        {formatValue(serverData[field])}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Actions */}
        <div className="border-t border-gray-200 px-6 py-4 bg-gray-50">
          <div className="flex flex-col sm:flex-row gap-3">
            <button
              onClick={() => onResolve('theirs')}
              className="flex-1 px-4 py-2.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 
                       transition-colors font-medium flex items-center justify-center gap-2"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
              </svg>
              Keep Server Version
            </button>
            
            <button
              onClick={() => onResolve('mine')}
              className="flex-1 px-4 py-2.5 bg-cyan-600 text-white rounded-lg hover:bg-cyan-700 
                       transition-colors font-medium flex items-center justify-center gap-2"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
              </svg>
              Keep My Changes
            </button>
            
            {conflictingFields.length === 0 && (onlyYourFields.length > 0 || onlyServerFields.length > 0) && (
              <button
                onClick={() => onResolve('merge')}
                className="flex-1 px-4 py-2.5 bg-green-600 text-white rounded-lg hover:bg-green-700 
                         transition-colors font-medium flex items-center justify-center gap-2"
              >
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7h12m0 0l-4-4m4 4l-4 4m0 6H4m0 0l4 4m-4-4l4-4" />
                </svg>
                Merge Both
              </button>
            )}
          </div>
          
          <button
            onClick={onCancel}
            className="w-full mt-3 px-4 py-2 text-gray-600 hover:text-gray-800 
                     transition-colors text-sm font-medium"
          >
            Cancel and Review
          </button>
        </div>
      </div>
    </div>
  );
}

export default ConflictModal;
