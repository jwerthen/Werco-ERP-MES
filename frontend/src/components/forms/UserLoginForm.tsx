import React from 'react';
import { FormField } from '../ui/FormField';
import { FormWithValidation } from '../ui/FormWithValidation';
import { UserLoginFormData, userLoginSchema } from '../../validation/schemas';

interface UserLoginFormProps {
  onSubmit: (data: UserLoginFormData) => Promise<void>;
  submitButtonText?: string;
  isSubmitting?: boolean;
}

/**
 * User login form with validation
 */
export function UserLoginForm({
  onSubmit,
  submitButtonText = 'Sign In',
  isSubmitting = false,
}: UserLoginFormProps) {
  const defaultValues: Partial<UserLoginFormData> = {
    email: undefined,
    password: undefined,
  };

  return (
    <FormWithValidation
      schema={userLoginSchema}
      initialValues={defaultValues}
      onSubmit={onSubmit}
      submitButtonText={submitButtonText}
      isSubmitting={isSubmitting}
      className="space-y-6"
    >
      {({ form, errors }) => {
        const { register } = form;

        return (
          <>
            <FormField label="Email" name="email" error={errors.email} required>
              <input
                {...register('email')}
                type="email"
                className={`input ${errors.email ? 'input-error' : ''}`}
                placeholder="user@example.com"
                autoComplete="email"
                autoFocus
              />
            </FormField>

            <FormField label="Password" name="password" error={errors.password} required>
              <input
                {...register('password')}
                type="password"
                className={`input ${errors.password ? 'input-error' : ''}`}
                placeholder="••••••••••••"
                autoComplete="current-password"
              />
            </FormField>

            <div className="flex items-center justify-between">
              <div className="flex items-center">
                <input
                  id="remember-me"
                  name="remember-me"
                  type="checkbox"
                  className="h-4 w-4 text-blue-600 focus:ring-blue-500 border-gray-300 rounded"
                />
                <label htmlFor="remember-me" className="ml-2 block text-sm text-gray-700">
                  Remember me
                </label>
              </div>

              <div className="text-sm">
                <a href="#" className="font-medium text-blue-600 hover:text-blue-500">
                  Forgot password?
                </a>
              </div>
            </div>
          </>
        );
      }}
    </FormWithValidation>
  );
}
