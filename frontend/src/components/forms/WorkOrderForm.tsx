import React from 'react';
import { FormField } from '../ui/FormField';
import { FormWithValidation } from '../ui/FormWithValidation';
import {
  WorkOrderFormData,
  workOrderSchema,
  WorkOrderOperationFormData,
  workOrderOperationSchema,
} from '../../validation/schemas';

interface WorkOrderFormProps {
  initialData?: Partial<WorkOrderFormData>;
  onSubmit: (data: WorkOrderFormData) => Promise<void>;
  submitButtonText?: string;
  isSubmitting?: boolean;
}

/**
 * Work Order form with validation including date relationship checks
 */
export function WorkOrderForm({
  initialData = {},
  onSubmit,
  submitButtonText = 'Create Work Order',
  isSubmitting = false,
}: WorkOrderFormProps) {
  const defaultValues: Partial<WorkOrderFormData> = {
    priority: 5,
    ...initialData,
  };

  return (
    <FormWithValidation
      schema={workOrderSchema}
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
            <div className="bg-blue-50 border border-blue-200 rounded-md p-4">
              <h4 className="text-sm font-medium text-blue-800 mb-2">
                Work Order Information
              </h4>
              <p className="text-xs text-blue-600">
                All dates must be today or future.
              </p>
            </div>

            <FormField label="Part ID" name="part_id" error={errors.part_id?.message} required>
              <input
                {...register('part_id', { valueAsNumber: true })}
                type="number"
                min="1"
                className={`input ${errors.part_id ? 'input-error' : ''}`}
                placeholder="Enter part ID"
              />
            </FormField>

            <FormField label="Quantity Ordered" name="quantity_ordered" error={errors.quantity_ordered?.message} required>
              <input
                {...register('quantity_ordered', { valueAsNumber: true })}
                type="number"
                step="0.0001"
                min="0"
                max="999999.9999"
                className={`input ${errors.quantity_ordered ? 'input-error' : ''}`}
                placeholder="0.0000 - 999999.9999"
              />
            </FormField>

            <FormField label="Priority" name="priority" error={errors.priority?.message} required>
              <div className="flex items-center gap-4">
                <input
                  {...register('priority', { valueAsNumber: true })}
                  type="number"
                  min="1"
                  max="10"
                  className="input w-24"
                />
                <div className="flex flex-wrap gap-1">
                  {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((p) => (
                    <button
                      key={p}
                      type="button"
                      onClick={() => form.setValue('priority', p as any)}
                      className={`px-2 py-1 text-xs rounded ${
                        form.watch('priority') === p
                          ? p <= 3
                            ? 'bg-red-500 text-white'
                            : p <= 6
                            ? 'bg-yellow-500 text-white'
                            : 'bg-green-500 text-white'
                          : 'bg-gray-200 text-gray-700'
                      }`}
                    >
                      {p}
                    </button>
                  ))}
                </div>
                <span className="text-xs text-gray-500">
                  (1=Highest, 10=Lowest)
                </span>
              </div>
            </FormField>

            <FormField label="Due Date" name="due_date" error={errors.due_date?.message}>
              <input
                {...register('due_date')}
                type="date"
                className={`input ${errors.due_date ? 'input-error' : ''}`}
                min={new Date().toISOString().split('T')[0]}
              />
            </FormField>

            <FormField label="Customer Name" name="customer_name">
              <input {...register('customer_name')} className="input" placeholder="Optional" maxLength={255} />
            </FormField>

            <FormField label="Customer PO" name="customer_po">
              <input {...register('customer_po')} className="input" placeholder="Optional" maxLength={50} />
            </FormField>

            <FormField label="Notes" name="notes">
              <textarea
                {...register('notes')}
                rows={3}
                className="input"
                placeholder="Additional notes (optional)"
                maxLength={2000}
              />
            </FormField>

            <FormField label="Special Instructions" name="special_instructions">
              <textarea
                {...register('special_instructions')}
                rows={3}
                className="input"
                placeholder="Special manufacturing instructions (optional)"
                maxLength={2000}
              />
            </FormField>
          </>
        );
      }}
    </FormWithValidation>
  );
}

interface WorkOrderOperationFormProps {
  initialData?: Partial<WorkOrderOperationFormData>;
  onSubmit: (data: WorkOrderOperationFormData) => Promise<void>;
  submitButtonText?: string;
  isSubmitting?: boolean;
}

/**
 * Work Order Operation form with validation
 */
export function WorkOrderOperationForm({
  initialData = {},
  onSubmit,
  submitButtonText = 'Save Operation',
  isSubmitting = false,
}: WorkOrderOperationFormProps) {
  const defaultValues: Partial<WorkOrderOperationFormData> = {
    sequence: 10,
    setup_time_hours: 0,
    run_time_hours: 0,
    run_time_per_piece: 0,
    requires_inspection: false,
    ...initialData,
  };

  return (
    <FormWithValidation
      schema={workOrderOperationSchema}
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
              <FormField label="Work Center ID" name="work_center_id" error={errors.work_center_id?.message} required>
                <input
                  {...register('work_center_id', { valueAsNumber: true })}
                  type="number"
                  min="1"
                  className={`input ${errors.work_center_id ? 'input-error' : ''}`}
                  placeholder="Enter work center ID"
                />
              </FormField>

              <FormField label="Sequence" name="sequence" error={errors.sequence?.message} required>
                <input
                  {...register('sequence', { valueAsNumber: true })}
                  type="number"
                  min="10"
                  max="990"
                  step="10"
                  className={`input ${errors.sequence ? 'input-error' : ''}`}
                  placeholder="10, 20, 30... (multiples of 10)"
                />
              </FormField>
            </div>

            <FormField label="Operation Number" name="operation_number">
              <input
                {...register('operation_number')}
                className="input"
                placeholder="e.g., OP-10, OP-20 (optional)"
                maxLength={50}
              />
            </FormField>

            <FormField label="Operation Name" name="name" error={errors.name?.message} required>
              <input {...register('name')} className={`input ${errors.name ? 'input-error' : ''}`} placeholder="e.g., Machining" />
            </FormField>

            <FormField label="Description" name="description">
              <textarea
                {...register('description')}
                rows={2}
                className="input"
                placeholder="Operation description (optional)"
                maxLength={5000}
              />
            </FormField>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <FormField label="Setup Time (Hours)" name="setup_time_hours">
                <input
                  {...register('setup_time_hours', { valueAsNumber: true })}
                  type="number"
                  step="0.01"
                  min="0"
                  max="99.99"
                  className="input"
                  placeholder="0.00 - 99.99"
                />
              </FormField>

              <FormField label="Run Time (Hours)" name="run_time_hours">
                <input
                  {...register('run_time_hours', { valueAsNumber: true })}
                  type="number"
                  step="0.01"
                  min="0"
                  max="999.99"
                  className="input"
                  placeholder="0.00 - 999.99"
                />
              </FormField>
            </div>

            <FormField label="Run Time Per Piece (Hours)" name="run_time_per_piece">
              <input
                {...register('run_time_per_piece', { valueAsNumber: true })}
                type="number"
                step="0.0001"
                min="0"
                className="input"
                placeholder="0.0000 - unlimited"
              />
            </FormField>

            <div className="space-y-4">
              <FormField label="Setup Instructions" name="setup_instructions">
                <textarea
                  {...register('setup_instructions')}
                  rows={2}
                  className="input"
                  placeholder="Setup instructions (optional)"
                  maxLength={5000}
                />
              </FormField>

              <FormField label="Run Instructions" name="run_instructions">
                <textarea
                  {...register('run_instructions')}
                  rows={2}
                  className="input"
                  placeholder="Run instructions (optional)"
                  maxLength={5000}
                />
              </FormField>
            </div>

            <div className="space-y-3">
              <label className="flex items-center space-x-3">
                <input
                  {...register('requires_inspection')}
                  type="checkbox"
                  className="h-4 w-4 text-blue-600 focus:ring-blue-500 border-gray-300 rounded"
                />
                <span className="text-sm text-gray-700">Requires Inspection</span>
              </label>

              <FormField label="Inspection Type" name="inspection_type">
                <select {...register('inspection_type')} className="select">
                  <option value="">Select inspection type (optional)</option>
                  <option value="visual">Visual Inspection</option>
                  <option value="dimensional">Dimensional Inspection</option>
                  <option value="functional">Functional Testing</option>
                  <option value="documentation">Documentation Review</option>
                </select>
              </FormField>
            </div>
          </>
        );
      }}
    </FormWithValidation>
  );
}
