import React, { useState } from 'react';
import { z } from 'zod';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { FormField } from '../ui/FormField';
import { FormWithValidation } from '../ui/FormWithValidation';
import { PartFormData, partSchema, PartType, UnitOfMeasure } from '../../validation/schemas';
import { useFormErrorMapping, useAsyncValidation } from '../../hooks/useFormErrorHandling';

interface PartFormProps {
  initialData?: Partial<PartFormData>;
  onSubmit: (data: PartFormData) => Promise<void>;
  submitButtonText?: string;
  isSubmitting?: boolean;
}

/**
 * Part form with comprehensive Zod validation
 * Supports both create and update modes via initialData prop
 */
export function PartForm({
  initialData = {},
  onSubmit,
  submitButtonText = 'Save Part',
  isSubmitting = false,
}: PartFormProps) {
  // Default values
  const defaultValues: Partial<PartFormData> = {
    revision: 'A',
    unit_of_measure: UnitOfMeasure.EACH,
    part_type: PartType.MANUFACTURED,
    standard_cost: 0,
    material_cost: 0,
    labor_cost: 0,
    overhead_cost: 0,
    lead_time_days: 0,
    safety_stock: 0,
    reorder_point: 0,
    reorder_quantity: 0,
    is_critical: false,
    requires_inspection: true,
    ...initialData,
  };

  return (
    <FormWithValidation
      schema={partSchema}
      initialValues={defaultValues}
      onSubmit={onSubmit}
      submitButtonText={submitButtonText}
      isSubmitting={isSubmitting}
      className="space-y-6"
    >
      {({ form, errors }) => {
        const { register, watch, setValue } = form;
        const partType = watch('part_type');

        return (
          <>
            {/* Basic Info */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <FormField label="Part Number" name="part_number" error={errors.part_number?.message} required>
                <input
                  {...register('part_number')}
                  className={`input ${errors.part_number ? 'input-error' : ''}`}
                  placeholder="e.g., WIDGET-001"
                  autoComplete="off"
                />
              </FormField>

              <FormField label="Revision" name="revision" error={errors.revision?.message} required>
                <input
                  {...register('revision')}
                  className={`input ${errors.revision ? 'input-error' : ''}`}
                  placeholder="e.g., A, B, 01"
                  maxLength={5}
                />
              </FormField>
            </div>

            <FormField label="Part Name" name="name" error={errors.name?.message} required>
              <input
                {...register('name')}
                className={`input ${errors.name ? 'input-error' : ''}`}
                placeholder="e.g., Widget Assembly"
              />
            </FormField>

            <FormField label="Description" name="description" error={errors.description?.message}>
              <textarea
                {...register('description')}
                rows={3}
                className={`input ${errors.description ? 'input-error' : ''}`}
                placeholder="Detailed part description (optional)"
                maxLength={2000}
              />
            </FormField>

            {/* Classification */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <FormField label="Part Type" name="part_type" error={errors.part_type?.message} required>
                <select {...register('part_type')} className="select">
                  <option value={PartType.MANUFACTURED}>Manufactured (Make)</option>
                  <option value={PartType.PURCHASED}>Purchased (Buy)</option>
                  <option value={PartType.ASSEMBLY}>Assembly</option>
                  <option value={PartType.RAW_MATERIAL}>Raw Material</option>
                </select>
              </FormField>

              <FormField label="Unit of Measure" name="unit_of_measure" error={errors.unit_of_measure?.message} required>
                <select {...register('unit_of_measure')} className="select">
                  <option value={UnitOfMeasure.EACH}>Each (EA)</option>
                  <option value={UnitOfMeasure.FEET}>Feet (FT)</option>
                  <option value={UnitOfMeasure.INCHES}>Inches (IN)</option>
                  <option value={UnitOfMeasure.POUNDS}>Pounds (LB)</option>
                  <option value={UnitOfMeasure.KILOGRAMS}>Kilograms (KG)</option>
                  <option value={UnitOfMeasure.SHEETS}>Sheets</option>
                  <option value={UnitOfMeasure.GALLONS}>Gallons (GAL)</option>
                  <option value={UnitOfMeasure.LITERS}>Liters</option>
                </select>
              </FormField>
            </div>

            {/* AS9100D Classification */}
            <div className="bg-yellow-50 border border-yellow-200 rounded-md p-4">
              <h4 className="text-sm font-medium text-yellow-800 mb-3">AS9100D Quality Requirements</h4>

              <div className="space-y-3">
                <label className="flex items-center space-x-3">
                  <input
                    {...register('is_critical')}
                    type="checkbox"
                    className="h-4 w-4 text-yellow-600 focus:ring-yellow-500 border-gray-300 rounded"
                  />
                  <span className="text-sm text-gray-700">Critical Characteristic (requires 100% inspection)</span>
                </label>

                <label className="flex items-center space-x-3">
                  <input
                    {...register('requires_inspection')}
                    type="checkbox"
                    className="h-4 w-4 text-yellow-600 focus:ring-yellow-500 border-gray-300 rounded"
                  />
                  <span className="text-sm text-gray-700">Requires Receiving Inspection</span>
                </label>
              </div>

              <FormField label="Inspection Requirements" name="inspection_requirements" className="mt-3">
                <textarea
                  {...register('inspection_requirements')}
                  rows={2}
                  className="input"
                  placeholder="Special inspection notes (optional)"
                  maxLength={2000}
                />
              </FormField>
            </div>

            {/* Costing */}
            <div>
              <h4 className="text-sm font-medium text-gray-700 mb-3">Costing</h4>
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
                <FormField label="Standard Cost" name="standard_cost">
                  <div className="relative">
                    <span className="absolute left-3 top-1/2 transform -translate-y-1/2 text-gray-500">$</span>
                    <input
                      {...register('standard_cost', { valueAsNumber: true })}
                      type="number"
                      step="0.01"
                      min="0"
                      max="999999.99"
                      className="input pl-7"
                    />
                  </div>
                </FormField>

                <FormField label="Material Cost" name="material_cost">
                  <div className="relative">
                    <span className="absolute left-3 top-1/2 transform -translate-y-1/2 text-gray-500">$</span>
                    <input
                      {...register('material_cost', { valueAsNumber: true })}
                      type="number"
                      step="0.01"
                      min="0"
                      max="999999.99"
                      className="input pl-7"
                    />
                  </div>
                </FormField>

                <FormField label="Labor Cost" name="labor_cost">
                  <div className="relative">
                    <span className="absolute left-3 top-1/2 transform -translate-y-1/2 text-gray-500">$</span>
                    <input
                      {...register('labor_cost', { valueAsNumber: true })}
                      type="number"
                      step="0.01"
                      min="0"
                      max="999999.99"
                      className="input pl-7"
                    />
                  </div>
                </FormField>

                <FormField label="Overhead Cost" name="overhead_cost">
                  <div className="relative">
                    <span className="absolute left-3 top-1/2 transform -translate-y-1/2 text-gray-500">$</span>
                    <input
                      {...register('overhead_cost', { valueAsNumber: true })}
                      type="number"
                      step="0.01"
                      min="0"
                      max="999999.99"
                      className="input pl-7"
                    />
                  </div>
                </FormField>
              </div>
            </div>

            {/* Lead Time */}
            <FormField label="Lead Time (Days)" name="lead_time_days">
              <div className="relative">
                <input
                  {...register('lead_time_days', { valueAsNumber: true })}
                  type="number"
                  min="0"
                  max="365"
                  className="input"
                />
                <span className="absolute right-3 top-1/2 transform -translate-y-1/2 text-gray-500 text-sm">days</span>
              </div>
            </FormField>

            {/* Inventory Settings */}
            <div>
              <h4 className="text-sm font-medium text-gray-700 mb-3">Reorder Settings (MRP)</h4>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <FormField label="Safety Stock" name="safety_stock">
                  <input
                    {...register('safety_stock', { valueAsNumber: true })}
                    type="number"
                    step="0.0001"
                    min="0"
                    className="input"
                  />
                </FormField>

                <FormField label="Reorder Point" name="reorder_point">
                  <input
                    {...register('reorder_point', { valueAsNumber: true })}
                    type="number"
                    step="0.0001"
                    min="0"
                    className="input"
                  />
                </FormField>

                <FormField label="Reorder Quantity" name="reorder_quantity" error={errors.reorder_quantity?.message}>
                  <input
                    {...register('reorder_quantity', { valueAsNumber: true })}
                    type="number"
                    step="0.0001"
                    min="0"
                    className={`input ${errors.reorder_quantity ? 'input-error' : ''}`}
                  />
                </FormField>
              </div>
              <p className="text-xs text-gray-500 mt-1">
                Reorder quantity must be greater than 0 when reorder point is set
              </p>
            </div>

            {/* Customer Info */}
            <div>
              <h4 className="text-sm font-medium text-gray-700 mb-3">Customer Information</h4>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <FormField label="Customer Part Number" name="customer_part_number">
                  <input
                    {...register('customer_part_number')}
                    className="input"
                    placeholder="Customer's part number (optional)"
                    maxLength={100}
                  />
                </FormField>

                <FormField label="Drawing Number" name="drawing_number">
                  <input
                    {...register('drawing_number')}
                    className="input"
                    placeholder="Drawing reference (optional)"
                    maxLength={100}
                  />
                </FormField>
              </div>
            </div>
          </>
        );
      }}
    </FormWithValidation>
  );
}
