import React, { useEffect, useMemo } from 'react';
import { ComboBox, FormField } from '../ui';
import type { OwnerDirectory } from '../../hooks/useOwnerDirectory';
import type { OwnershipSelection, OwnershipType } from '../../types/ownership';
import { OWNERSHIP_LABELS } from '../../utils/ownership';

export interface OwnershipFieldsProps {
  value: OwnershipSelection;
  onChange: (next: OwnershipSelection) => void;
  /** One per screen (`useOwnerDirectory`), so the owner lists load once, lazily. */
  directory: OwnerDirectory;
  disabled?: boolean;
  /**
   * Pre-selected supplier when the kind is switched to "Supplier-owned" and no
   * supplier is chosen yet — on a PO receipt, the PO's own vendor (consigned stock
   * from the supplier that shipped it is the common case). Still editable.
   */
  defaultVendorId?: number | null;
  /**
   * Name of the owner currently on the record, so an owner the picker's list no
   * longer offers (an inactive customer) still shows by name instead of blanking.
   */
  currentOwnerName?: string | null;
  labelClassName?: string;
  /** Extra classes on the two-column grid. */
  className?: string;
}

const KINDS: OwnershipType[] = ['company', 'customer', 'supplier'];

/**
 * Ownership kind + owner picker, shared by every write that can create or re-own
 * property: Inventory receive, PO receipts (single line and multi-line delivery),
 * the change-of-ownership dialog and the Tool create form.
 *
 * The owner list is fetched only once the kind is switched away from company — the
 * moment the picker opens — so a screen that never leaves "Company-owned" makes no
 * extra request. Switching kind always clears the id that does not belong to it, so
 * the three fields can never contradict the server's CHECK.
 */
export function OwnershipFields({
  value,
  onChange,
  directory,
  disabled = false,
  defaultVendorId = null,
  currentOwnerName = null,
  labelClassName,
  className = 'grid grid-cols-1 sm:grid-cols-2 gap-3',
}: OwnershipFieldsProps) {
  const { ensure } = directory;
  useEffect(() => {
    if (value.ownership !== 'company') ensure(value.ownership);
  }, [value.ownership, ensure]);

  const changeKind = (kind: OwnershipType) =>
    onChange({
      ownership: kind,
      owner_customer_id: kind === 'customer' ? value.owner_customer_id : null,
      owner_vendor_id: kind === 'supplier' ? (value.owner_vendor_id ?? defaultVendorId ?? null) : null,
    });

  const customerOptions = useMemo(() => {
    const options = (directory.customers.options || []).map(customer => ({
      value: String(customer.id),
      label: customer.name,
    }));
    const selected = value.owner_customer_id;
    if (selected && !options.some(option => option.value === String(selected)))
      options.unshift({ value: String(selected), label: currentOwnerName || `Customer #${selected}` });
    return options;
  }, [directory.customers.options, value.owner_customer_id, currentOwnerName]);

  const vendorOptions = useMemo(() => {
    const options = (directory.vendors.options || []).map(vendor => ({
      value: String(vendor.id),
      label: `${vendor.name}${vendor.code ? ` (${vendor.code})` : ''}${vendor.is_active === false ? ' — inactive' : ''}`,
    }));
    const selected = value.owner_vendor_id;
    if (selected && !options.some(option => option.value === String(selected)))
      options.unshift({ value: String(selected), label: currentOwnerName || `Supplier #${selected}` });
    return options;
  }, [directory.vendors.options, value.owner_vendor_id, currentOwnerName]);

  const retryButton = (kind: 'customer' | 'supplier') => (
    <button type="button" className="ml-2 underline" onClick={() => directory.retry(kind)}>
      Retry
    </button>
  );

  return (
    <div className={className}>
      <FormField
        label="Ownership"
        labelClassName={labelClassName}
        help={
          value.ownership === 'company'
            ? undefined
            : 'Not company property: automatic issue and backflush skip it, and it is left out of inventory value.'
        }
      >
        {field => (
          <select
            {...field}
            className="input w-full"
            value={value.ownership}
            disabled={disabled}
            onChange={event => changeKind(event.target.value as OwnershipType)}
          >
            {KINDS.map(kind => (
              <option key={kind} value={kind}>
                {OWNERSHIP_LABELS[kind]}
              </option>
            ))}
          </select>
        )}
      </FormField>
      {value.ownership === 'customer' && (
        <FormField
          label="Owning customer"
          required
          labelClassName={labelClassName}
          error={directory.customers.error || null}
        >
          {field => (
            <>
              <ComboBox
                id={field.id}
                ariaDescribedBy={field['aria-describedby']}
                options={customerOptions}
                value={value.owner_customer_id ? String(value.owner_customer_id) : ''}
                onChange={next => onChange({ ...value, owner_customer_id: next ? Number(next) : null })}
                placeholder={directory.customers.loading ? 'Loading customers…' : 'Search for a customer…'}
                disabled={disabled || (directory.customers.loading && !customerOptions.length)}
                noResultsLabel="No matching customer"
              />
              {directory.customers.error && retryButton('customer')}
            </>
          )}
        </FormField>
      )}
      {value.ownership === 'supplier' && (
        <FormField
          label="Owning supplier"
          required
          labelClassName={labelClassName}
          error={directory.vendors.error || null}
        >
          {field => (
            <>
              <select
                {...field}
                className="input w-full"
                value={value.owner_vendor_id ? String(value.owner_vendor_id) : ''}
                disabled={disabled || (directory.vendors.loading && !vendorOptions.length)}
                onChange={event =>
                  onChange({ ...value, owner_vendor_id: event.target.value ? Number(event.target.value) : null })
                }
              >
                <option value="">{directory.vendors.loading ? 'Loading suppliers…' : 'Select a supplier…'}</option>
                {vendorOptions.map(option => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
              {directory.vendors.error && retryButton('supplier')}
            </>
          )}
        </FormField>
      )}
    </div>
  );
}

export default OwnershipFields;
