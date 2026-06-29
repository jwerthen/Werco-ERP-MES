import React, { useId } from 'react';

/**
 * Wiring props handed to a FormField's render-prop child. Spread these onto the
 * control (native <input>/<select>/<textarea> or the custom <SelectField>) so the
 * label, help text, and error are all programmatically associated with it.
 *
 *   - `id` matches the <label htmlFor>, so clicking the label focuses the control
 *     and `getByLabelText` resolves it.
 *   - `aria-describedby` points at the help text and/or the error message.
 *   - `aria-invalid` is true while an error is shown.
 *   - `aria-required` mirrors the required marker for assistive tech (the native
 *     `required` attribute is left to the caller, since the app's controlled
 *     inputs already set it where the browser should enforce it).
 */
export interface FormFieldRenderProps {
  id: string;
  'aria-describedby'?: string;
  'aria-invalid'?: boolean;
  'aria-required'?: boolean;
}

interface FormFieldProps {
  /** Visible field label. */
  label: React.ReactNode;
  /**
   * The control. Either a render function that receives the wiring props (the
   * common, most explicit form), or a plain node (e.g. a checkbox row) when you
   * only need the label/error chrome and wire the control yourself.
   */
  children: ((field: FormFieldRenderProps) => React.ReactNode) | React.ReactNode;
  /** Marks the field required: red asterisk + aria-required on the control. */
  required?: boolean;
  /** Field-level error message. When set, role="alert" + aria-invalid + red text. */
  error?: string | null;
  /** Optional help/hint text, linked via aria-describedby. */
  help?: React.ReactNode;
  /** Override the auto-generated control id (rarely needed). */
  htmlFor?: string;
  /** Extra classes on the field wrapper. */
  className?: string;
  /** Extra classes on the <label>. */
  labelClassName?: string;
}

/**
 * FormField — the label-association + accessible-error primitive for the app's
 * forms. It closes the gap where the codebase has ~537 <label>s but almost none
 * are tied to their control via htmlFor.
 *
 * Render-prop usage (preferred — works for native inputs AND <SelectField>):
 *
 *   <FormField label="Customer Name" required error={errors.name}>
 *     {(field) => (
 *       <input
 *         {...field}
 *         type="text"
 *         required
 *         className={errors.name ? 'input-error' : 'input'}
 *         value={formData.name}
 *         onChange={(e) => setFormData({ ...formData, name: e.target.value })}
 *       />
 *     )}
 *   </FormField>
 *
 *   <FormField label="Payment Terms">
 *     {(field) => (
 *       <SelectField {...field} value={...} options={...} onChange={...} />
 *     )}
 *   </FormField>
 *
 * The instrument-panel chrome (`.label`, `.input`/`.input-error`) is reused, so
 * fields visually match the rest of the app.
 */
export function FormField({
  label,
  children,
  required = false,
  error = null,
  help,
  htmlFor,
  className,
  labelClassName,
}: FormFieldProps) {
  const autoId = useId();
  const id = htmlFor ?? autoId;
  const helpId = `${id}-help`;
  const errorId = `${id}-error`;

  const hasError = Boolean(error);
  const describedBy = [help ? helpId : null, hasError ? errorId : null]
    .filter(Boolean)
    .join(' ') || undefined;

  const field: FormFieldRenderProps = {
    id,
    'aria-describedby': describedBy,
    'aria-invalid': hasError ? true : undefined,
    'aria-required': required ? true : undefined,
  };

  return (
    <div className={className}>
      <label htmlFor={id} className={['label', labelClassName].filter(Boolean).join(' ')}>
        {label}
        {required && (
          <>
            {/* Visible marker for sighted users; the accessible name is carried
                by aria-required + the visually-hidden "required" text below. */}
            <span aria-hidden="true" className="ml-0.5 text-fd-red">
              *
            </span>
            <span className="sr-only"> (required)</span>
          </>
        )}
      </label>

      {typeof children === 'function' ? children(field) : children}

      {help && (
        <p id={helpId} className="mt-1 text-xs text-slate-400">
          {help}
        </p>
      )}

      {hasError && (
        <p id={errorId} role="alert" aria-live="polite" className="mt-1 text-xs text-fd-red">
          {error}
        </p>
      )}
    </div>
  );
}

export default FormField;
