import React from 'react';
import { FormField } from '../ui/FormField';
import { FormWithValidation } from '../ui/FormWithValidation';
import { UserFormData, userSchema, UserRole } from '../../validation/schemas';

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
 * Password requirements hint that updates as user types
 */
function PasswordRequirementsHint({ password }: { password: string }) {
  const requirements = [
    { test: /[A-Z]/.test(password), label: 'Uppercase letter' },
    { test: /[a-z]/.test(password), label: 'Lowercase letter' },
    { test: /[0-9]/.test(password), label: 'Number' },
    { test: /[^A-Za-z0-9]/.test(password), label: 'Special character' },
    { test: password.length >= 12, label: '12+ characters' },
  ];

  const allMet = requirements.every((r) => r.test);

  return (
    <div className="mt-2">
      <p className="text-xs text-gray-600 mb-1">Password must contain:</p>
      <ul className="space-y-1 text-xs">
        {requirements.map((req, idx) => (
          <li key={idx} className={`flex items-center gap-1 ${req.test ? 'text-green-600' : 'text-gray-400'}`}>
            {req.test ? (
              <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
              </svg>
            ) : (
              <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            )}
            <span>{req.label}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
