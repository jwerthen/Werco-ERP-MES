import React from 'react';
import { FormField } from '../ui/FormField';
import { FormWithValidation } from '../ui/FormWithValidation';
import { UserFormData, userSchema, UserRole, calculatePasswordStrength } from '../../validation/schemas';

interface UserFormProps {
  initialData?: Partial<UserFormData>;
  onSubmit: (data: UserFormData) => Promise<void>;
  submitButtonText?: string;
  isSubmitting?: boolean;
}

/**
 * User creation form with comprehensive validation including password strength
 */
export function UserForm({
  initialData = {},
  onSubmit,
  submitButtonText = 'Create User',
  isSubmitting = false,
}: UserFormProps) {
  const defaultValues: Partial<UserFormData> = {
    role: UserRole.OPERATOR,
    ...initialData,
  };

  return (
    <FormWithValidation
      schema={userSchema}
      initialValues={defaultValues}
      onSubmit={onSubmit}
      submitButtonText={submitButtonText}
      isSubmitting={isSubmitting}
      className="space-y-6"
    >
      {({ form, errors }) => {
        const { register, watch } = form;

        return (
          <>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <FormField label="Employee ID" name="employee_id" error={errors.employee_id} required>
                <input
                  {...register('employee_id')}
                  className={`input ${errors.employee_id ? 'input-error' : ''}`}
                  placeholder="e.g., EMP001"
                  autoComplete="off"
                />
              </FormField>

              <FormField label="Email" name="email" error={errors.email} required>
                <input
                  {...register('email')}
                  type="email"
                  className={`input ${errors.email ? 'input-error' : ''}`}
                  placeholder="user@example.com"
                  autoComplete="email"
                />
              </FormField>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <FormField label="First Name" name="first_name" error={errors.first_name} required>
                <input
                  {...register('first_name')}
                  className={`input ${errors.first_name ? 'input-error' : ''}`}
                  placeholder="John"
                  autoComplete="given-name"
                />
              </FormField>

              <FormField label="Last Name" name="last_name" error={errors.last_name} required>
                <input
                  {...register('last_name')}
                  className={`input ${errors.last_name ? 'input-error' : ''}`}
                  placeholder="Doe"
                  autoComplete="family-name"
                />
              </FormField>
            </div>

            <FormField label="Department" name="department">
              <input
                {...register('department')}
                className="input"
                placeholder="e.g., Manufacturing, Quality"
                maxLength={100}
              />
            </FormField>

            <FormField label="Role" name="role" error={errors.role} required>
              <select {...register('role')} className="select">
                <option value={UserRole.ADMIN}>Administrator</option>
                <option value={UserRole.MANAGER}>Manager</option>
                <option value={UserRole.SUPERVISOR}>Supervisor</option>
                <option value={UserRole.OPERATOR}>Operator</option>
                <option value={UserRole.QUALITY}>Quality</option>
                <option value={UserRole.SHIPPING}>Shipping</option>
                <option value={UserRole.VIEWER}>Viewer (Read Only)</option>
              </select>
            </FormField>

            <FormField label="Password" name="password" error={errors.password} required>
              <input
                {...register('password')}
                type="password"
                className={`input ${errors.password ? 'input-error' : ''}`}
                placeholder="Min 12 characters"
                autoComplete="new-password"
              />
              <PasswordRequirementsHint password={watch('password') || ''} />
            </FormField>
          </>
        );
      }}
    </FormWithValidation>
  );
}

/**
 * Password requirements hint with strength indicator
 */
function PasswordRequirementsHint({ password }: { password: string }) {
  if (!password) return null;

  const strength = calculatePasswordStrength(password);

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
        <span className={`text-xs font-medium ${textColorClasses[strength.color as keyof typeof textColorClasses]}`}>
          {strength.label}
        </span>
      </div>

      {/* Requirements checklist */}
      <div className="grid grid-cols-2 gap-1">
        {strength.requirements.map((req, idx) => (
          <div key={idx} className={`flex items-center gap-1 text-xs ${req.met ? 'text-green-600' : 'text-gray-400'}`}>
            {req.met ? (
              <svg className="h-3 w-3 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
              </svg>
            ) : (
              <svg className="h-3 w-3 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            )}
            <span>{req.label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
