import React from 'react';
import { FormField } from '../ui/FormField';
import { FormWithValidation } from '../ui/FormWithValidation';
import { VendorFormData, vendorSchema } from '../../validation/schemas';

interface VendorFormProps {
  initialData?: Partial<VendorFormData>;
  onSubmit: (data: VendorFormData) => Promise<void>;
  submitButtonText?: string;
  isSubmitting?: boolean;
}

/**
 * Vendor form with comprehensive Zod validation
 */
export function VendorForm({
  initialData = {},
  onSubmit,
  submitButtonText = 'Save Vendor',
  isSubmitting = false,
}: VendorFormProps) {
  const defaultValues: Partial<VendorFormData> = {
    country: 'USA',
    is_approved: false,
    is_as9100_certified: false,
    is_iso9001_certified: false,
    ...initialData,
  };

  return (
    <FormWithValidation
      schema={vendorSchema}
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
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <FormField label="Vendor Code" name="code" error={errors.code} required>
                <input
                  {...register('code')}
                  className={`input ${errors.code ? 'input-error' : ''}`}
                  placeholder="e.g., ABC-001"
                  autoComplete="off"
                />
                <p className="text-xs text-gray-500 mt-1">2-20 characters, uppercase</p>
              </FormField>

              <FormField label="Vendor Name" name="name" error={errors.name} required>
                <input
                  {...register('name')}
                  className={`input ${errors.name ? 'input-error' : ''}`}
                  placeholder="e.g., Acme Manufacturing"
                />
              </FormField>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <FormField label="Contact Name" name="contact_name">
                <input
                  {...register('contact_name')}
                  className="input"
                  placeholder="Primary contact"
                  maxLength={100}
                />
              </FormField>

              <FormField label="Email" name="email">
                <input
                  {...register('email')}
                  type="email"
                  className="input"
                  placeholder="contact@vendor.com"
                  maxLength={255}
                />
              </FormField>
            </div>

            <FormField label="Phone" name="phone">
              <input
                {...register('phone')}
                type="tel"
                className="input"
                placeholder="(555) 123-4567"
                maxLength={50}
              />
            </FormField>

            {/* Address */}
            <div>
              <h4 className="text-sm font-medium text-gray-700 mb-3">Address</h4>
              <FormField label="Address Line 1" name="address_line1">
                <input
                  {...register('address_line1')}
                  className="input"
                  placeholder="Street address"
                  maxLength={200}
                />
              </FormField>

              <FormField label="Address Line 2" name="address_line2">
                <input
                  {...register('address_line2')}
                  className="input"
                  placeholder="Suite, unit, etc. (optional)"
                  maxLength={200}
                />
              </FormField>

              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <FormField label="City" name="city">
                  <input {...register('city')} className="input" placeholder="City" maxLength={100} />
                </FormField>

                <FormField label="State" name="state">
                  <input
                    {...register('state')}
                    className="input"
                    placeholder="CA"
                    maxLength={2}
                    style={{ textTransform: 'uppercase' }}
                  />
                  <p className="text-xs text-gray-500 mt-1">2-letter code</p>
                </FormField>

                <FormField label="Postal Code" name="postal_code">
                  <input {...register('postal_code')} className="input" placeholder="ZIP/Postal Code" maxLength={20} />
                </FormField>
              </div>

              <FormField label="Country" name="country">
                <select {...register('country')} className="select" style={{ textTransform: 'uppercase' }}>
                  <option value="USA">USA</option>
                  <option value="CA">Canada</option>
                  <option value="MX">Mexico</option>
                  <option value="CN">China</option>
                  <option value="JP">Japan</option>
                  <option value="DE">Germany</option>
                  <option value="OTHER">Other</option>
                </select>
              </FormField>
            </div>

            {/* Vendor Terms */}
            <div>
              <h4 className="text-sm font-medium text-gray-700 mb-3">Terms</h4>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <FormField label="Payment Terms" name="payment_terms">
                  <input
                    {...register('payment_terms')}
                    className="input"
                    placeholder="e.g., NET 30, NET 60"
                    maxLength={100}
                  />
                </FormField>
              </div>
            </div>

            {/* AS9100D Certification */}
            <div className="bg-yellow-50 border border-yellow-200 rounded-md p-4">
              <h4 className="text-sm font-medium text-yellow-800 mb-3">Quality Certifications</h4>

              <div className="space-y-2">
                <label className="flex items-center space-x-3">
                  <input
                    {...register('is_approved')}
                    type="checkbox"
                    className="h-4 w-4 text-yellow-600 focus:ring-yellow-500 border-gray-300 rounded"
                  />
                  <span className="text-sm text-gray-700">Approved Vendor (can receive POs)</span>
                </label>

                <label className="flex items-center space-x-3">
                  <input
                    {...register('is_as9100_certified')}
                    type="checkbox"
                    className="h-4 w-4 text-yellow-600 focus:ring-yellow-500 border-gray-300 rounded"
                  />
                  <span className="text-sm text-gray-700">AS9100D Certified</span>
                </label>

                <label className="flex items-center space-x-3">
                  <input
                    {...register('is_iso9001_certified')}
                    type="checkbox"
                    className="h-4 w-4 text-yellow-600 focus:ring-yellow-500 border-gray-300 rounded"
                  />
                  <span className="text-sm text-gray-700">ISO 9001 Certified</span>
                </label>
              </div>
            </div>

            <FormField label="Notes" name="notes">
              <textarea
                {...register('notes')}
                rows={3}
                className="input"
                placeholder="Additional vendor notes (optional)"
                maxLength={2000}
              />
            </FormField>
          </>
        );
      }}
    </FormWithValidation>
  );
}
