import React from 'react';
import { useForm, UseFormReturn, SubmitHandler, FieldValues } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { useFormErrorMapping } from '../../hooks/useFormErrorHandling';

interface FormWithValidationProps<TData extends FieldValues, TSchema extends z.ZodSchema> {
  schema: TSchema;
  initialValues: Partial<TData>;
  onSubmit: (data: TData) => Promise<void>;
  submitButtonText: string;
  isSubmitting?: boolean;
  className?: string;
  children: (props: {
    form: UseFormReturn<TData>;
    errors: UseFormReturn<TData>['formState']['errors'];
    isDirty: boolean;
  }) => React.ReactNode;
}

/**
 * Generic form wrapper that provides Zod validation and error handling
 *
 * @example
 * <FormWithValidation
 *   schema={partSchema}
 *   initialValues={initialPart}
 *   onSubmit={handleSubmit}
 *   submitButtonText="Save Part"
 *   isSubmitting={isSubmitting}
 * >
 *   {({ form, errors, isDirty }) => (
 *     <FormField label="Part Number" name="part_number" error={errors.part_number?.message}>
 *       <input {...form.register('part_number')} />
 *     </FormField>
 *   )}
 * </FormWithValidation>
 */
export function FormWithValidation<TData extends FieldValues & z.infer<TSchema>, TSchema extends z.ZodSchema>({
  schema,
  initialValues,
  onSubmit,
  submitButtonText,
  isSubmitting = false,
  className = '',
  children,
}: FormWithValidationProps<TData, TSchema>) {
  const [formError, setFormError] = React.useState<string | null>(null);

  const form = useForm<TData>({
    resolver: zodResolver(schema),
    defaultValues: initialValues as any,
    mode: 'onBlur', // Validate on blur for immediate feedback
  });

  const { errors, dirtyFields, isValid } = form.formState;
  const { mapApiErrorToForm } = useFormErrorMapping({
    setError: form.setError as (name: string, error: { type?: string; message?: string }) => void,
    setFormError: setFormError,
  });

  const handleSubmit: SubmitHandler<TData> = async (data: TData) => {
    setFormError(null);
    try {
      await onSubmit(data);
      form.reset(data); // Reset form on successful submission
    } catch (error) {
      mapApiErrorToForm(error);
    }
  };

  return (
    <form onSubmit={form.handleSubmit(handleSubmit)} className={className} noValidate>
      {children({ form, errors, isDirty: Object.keys(dirtyFields).length > 0 })}

      {formError && (
        <div className="mb-4 p-4 bg-red-50 border border-red-200 rounded-md">
          <p className="text-sm text-red-800">{formError}</p>
        </div>
      )}

      <button
        type="submit"
        disabled={isSubmitting || !isValid}
        className="w-full btn btn-primary flex justify-center items-center"
      >
        {isSubmitting ? (
          <>
            <svg className="animate-spin -ml-1 mr-3 h-5 w-5 text-white" fill="none" viewBox="0 0 24 24">
              <circle
                className="opacity-25"
                cx="12"
                cy="12"
                r="10"
                stroke="currentColor"
                strokeWidth="4"
              />
              <path
                className="opacity-75"
                fill="currentColor"
                d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
              />
            </svg>
            Saving...
          </>
        ) : (
          submitButtonText
        )}
      </button>
    </form>
  );
}
