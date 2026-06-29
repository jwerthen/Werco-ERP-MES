import React, { useEffect, useMemo, useState, useCallback } from 'react';
import api from '../services/api';
import { PlusIcon, PencilIcon, MagnifyingGlassIcon, XMarkIcon, ArrowLeftIcon, UserGroupIcon } from '@heroicons/react/24/outline';
import { Modal } from '../components/ui/Modal';
import { FormField } from '../components/ui/FormField';
import { LoadingButton } from '../components/ui/LoadingButton';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';
import {
  EmptyState,
  ErrorState,
  useToast,
  DataTable,
  DataTableColumn,
  StatusBadge,
  MobileDataCard,
  Button,
  statusColor,
} from '../components/ui';
import { useNavigate, useSearchParams } from 'react-router-dom';

interface Customer {
  id: number;
  name: string;
  code?: string;
  contact_name?: string;
  email?: string;
  phone?: string;
  address_line1?: string;
  city?: string;
  state?: string;
  zip_code?: string;
  payment_terms?: string;
  requires_coc: boolean;
  requires_fai: boolean;
  is_active: boolean;
  created_at: string;
}

interface CustomerStats {
  customer_id: number;
  customer_name: string;
  part_count: number;
  work_order_counts: {
    total: number;
    by_status: Record<string, number>;
  };
  parts: Array<{
    id: number;
    part_number: string;
    name: string;
    part_type: string;
    revision?: string;
  }>;
  assemblies: Array<{
    id: number;
    part_number: string;
    name: string;
    part_type: string;
    revision?: string;
  }>;
  current_work_orders: Array<{
    id: number;
    work_order_number: string;
    status: string;
    due_date?: string;
    quantity_ordered: number;
    created_at?: string;
    part_number?: string;
  }>;
  past_work_orders: Array<{
    id: number;
    work_order_number: string;
    status: string;
    due_date?: string;
    quantity_ordered: number;
    created_at?: string;
    part_number?: string;
  }>;
  recent_work_orders: Array<{
    id: number;
    work_order_number: string;
    status: string;
    due_date?: string;
    quantity_ordered: number;
    created_at?: string;
  }>;
}

// Default/empty values for the create form. Shared by resetForm() and the
// unsaved-changes dirty check so "pristine" is defined in exactly one place.
const EMPTY_CUSTOMER_FORM = {
  name: '',
  contact_name: '',
  email: '',
  phone: '',
  address_line1: '',
  address_line2: '',
  city: '',
  state: '',
  zip_code: '',
  country: 'USA',
  ship_to_name: '',
  ship_address_line1: '',
  ship_city: '',
  ship_state: '',
  ship_zip_code: '',
  payment_terms: 'Net 30',
  requires_coc: true,
  requires_fai: false,
  special_requirements: '',
  notes: '',
};

type CustomerFormData = typeof EMPTY_CUSTOMER_FORM;

export default function Customers() {
  const navigate = useNavigate();
  const { showToast } = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [statsError, setStatsError] = useState(false);
  const [search, setSearch] = useState('');
  const [showInactive, setShowInactive] = useState(false);
  const [showModal, setShowModal] = useState(false);
  const [editingCustomer, setEditingCustomer] = useState<Customer | null>(null);
  const [saving, setSaving] = useState(false);
  const [selectedCustomer, setSelectedCustomer] = useState<Customer | null>(null);
  const [customerStats, setCustomerStats] = useState<CustomerStats | null>(null);
  const [loadingStats, setLoadingStats] = useState(false);

  const [formData, setFormData] = useState<CustomerFormData>(EMPTY_CUSTOMER_FORM);
  // Snapshot of the values the modal opened with (empty for create, the
  // customer's values for edit). The form is "dirty" when the live formData
  // diverges from this baseline.
  const [initialFormData, setInitialFormData] = useState<CustomerFormData>(EMPTY_CUSTOMER_FORM);

  const isFormDirty = useMemo(
    () => showModal && JSON.stringify(formData) !== JSON.stringify(initialFormData),
    [showModal, formData, initialFormData]
  );

  const { confirmDiscard } = useUnsavedChanges(isFormDirty);

  // Close the create/edit modal, prompting first if there are unsaved edits.
  const requestCloseModal = () => {
    if (!confirmDiscard()) return;
    setShowModal(false);
    resetForm();
  };

  const loadCustomers = useCallback(async () => {
    setLoading(true);
    setLoadError(false);
    try {
      const response = await api.getCustomers(!showInactive);
      setCustomers(response);
    } catch (err) {
      console.error('Failed to load customers:', err);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, [showInactive]);

  useEffect(() => {
    loadCustomers();
  }, [loadCustomers]);

  useEffect(() => {
    const requestedId = Number(searchParams.get('id') || 0);
    if (!requestedId || customers.length === 0) return;
    const customer = customers.find(c => c.id === requestedId);
    if (customer && selectedCustomer?.id !== requestedId) {
      viewCustomerDetails(customer);
    }
  }, [customers, searchParams, selectedCustomer?.id]);

  const filteredCustomers = customers.filter(c => {
    if (!search) return true;
    const searchLower = search.toLowerCase();
    return (
      c.name.toLowerCase().includes(searchLower) ||
      c.code?.toLowerCase().includes(searchLower) ||
      c.contact_name?.toLowerCase().includes(searchLower) ||
      c.city?.toLowerCase().includes(searchLower)
    );
  });

  const viewCustomerDetails = async (customer: Customer) => {
    setSelectedCustomer(customer);
    setLoadingStats(true);
    setStatsError(false);
    try {
      const stats = await api.getCustomerStats(customer.id);
      setCustomerStats(stats);
    } catch (err) {
      console.error('Failed to load customer stats:', err);
      setCustomerStats(null);
      setStatsError(true);
    } finally {
      setLoadingStats(false);
    }
  };

  const closeDetails = () => {
    setSelectedCustomer(null);
    setCustomerStats(null);
    const nextParams = new URLSearchParams(searchParams);
    nextParams.delete('id');
    setSearchParams(nextParams, { replace: true });
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (saving) return;
    setSaving(true);
    try {
      if (editingCustomer) {
        await api.updateCustomer(editingCustomer.id, formData);
        showToast('success', 'Customer updated');
      } else {
        await api.createCustomer(formData);
        showToast('success', 'Customer created');
      }
      setShowModal(false);
      resetForm();
      loadCustomers();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to save customer');
    } finally {
      setSaving(false);
    }
  };

  const handleEdit = (customer: Customer) => {
    setEditingCustomer(customer);
    const editValues: CustomerFormData = {
      ...EMPTY_CUSTOMER_FORM,
      name: customer.name,
      contact_name: customer.contact_name || '',
      email: customer.email || '',
      phone: customer.phone || '',
      address_line1: customer.address_line1 || '',
      city: customer.city || '',
      state: customer.state || '',
      zip_code: customer.zip_code || '',
      payment_terms: customer.payment_terms || 'Net 30',
      requires_coc: customer.requires_coc,
      requires_fai: customer.requires_fai,
    };
    setFormData(editValues);
    setInitialFormData(editValues);
    setShowModal(true);
  };

  const resetForm = () => {
    setEditingCustomer(null);
    setFormData(EMPTY_CUSTOMER_FORM);
    setInitialFormData(EMPTY_CUSTOMER_FORM);
  };

  const renderRequirements = (customer: Customer) => (
    <div className="flex gap-1">
      {customer.requires_coc && (
        <span className="px-2 py-0.5 bg-blue-500/20 text-blue-300 text-xs rounded">COC</span>
      )}
      {customer.requires_fai && (
        <span className="px-2 py-0.5 bg-purple-500/20 text-purple-300 text-xs rounded">FAI</span>
      )}
      {!customer.requires_coc && !customer.requires_fai && (
        <span className="text-slate-500 text-xs">-</span>
      )}
    </div>
  );

  const columns = useMemo<Array<DataTableColumn<Customer>>>(() => [
    {
      key: 'code',
      header: 'Code',
      sortable: true,
      className: 'font-mono',
      accessor: (c) => c.code ?? '',
    },
    {
      key: 'name',
      header: 'Name',
      sortable: true,
      className: 'font-medium text-werco-primary',
      accessor: (c) => c.name,
    },
    {
      key: 'contact',
      header: 'Contact',
      sortable: true,
      accessor: (c) => c.contact_name ?? '',
      csv: (c) => [c.contact_name, c.email].filter(Boolean).join(' '),
      render: (c) => (
        <div>
          <div className="text-sm">{c.contact_name || '-'}</div>
          {c.email && <div className="text-xs text-slate-400">{c.email}</div>}
        </div>
      ),
    },
    {
      key: 'location',
      header: 'Location',
      sortable: true,
      accessor: (c) => (c.city && c.state ? `${c.city}, ${c.state}` : ''),
      render: (c) => (c.city && c.state ? `${c.city}, ${c.state}` : '-'),
    },
    {
      key: 'terms',
      header: 'Terms',
      sortable: true,
      accessor: (c) => c.payment_terms ?? '',
      render: (c) => c.payment_terms || '-',
    },
    {
      key: 'requirements',
      header: 'Requirements',
      csv: (c) => [c.requires_coc ? 'COC' : '', c.requires_fai ? 'FAI' : ''].filter(Boolean).join(' '),
      render: renderRequirements,
    },
    {
      key: 'status',
      header: 'Status',
      sortable: true,
      accessor: (c) => (c.is_active ? 'active' : 'inactive'),
      render: (c) => (
        <StatusBadge status={c.is_active ? 'active' : 'inactive'} />
      ),
    },
    {
      key: 'actions',
      header: 'Actions',
      align: 'center',
      render: (c) => (
        <button
          onClick={(e) => { e.stopPropagation(); handleEdit(c); }}
          className="text-slate-500 hover:text-slate-400"
          aria-label={`Edit ${c.name}`}
        >
          <PencilIcon className="h-5 w-5" aria-hidden="true" />
        </button>
      ),
    },
  ], []);

  const renderMobileCard = (customer: Customer) => (
    <MobileDataCard
      title={customer.name}
      subtitle={customer.code ? `Code: ${customer.code}` : undefined}
      badge={<StatusBadge status={customer.is_active ? 'active' : 'inactive'} />}
      onClick={() => viewCustomerDetails(customer)}
      className={!customer.is_active ? 'opacity-60' : ''}
      fields={[
        { label: 'Contact', value: customer.contact_name || '-' },
        { label: 'Email', value: customer.email || '-' },
        {
          label: 'Location',
          value: customer.city && customer.state ? `${customer.city}, ${customer.state}` : '-',
        },
        { label: 'Terms', value: customer.payment_terms || '-' },
        { label: 'Requirements', value: renderRequirements(customer), fullWidth: true },
      ]}
      actions={
        <button
          onClick={(e) => { e.stopPropagation(); handleEdit(customer); }}
          className="inline-flex items-center gap-1 text-sm text-slate-300 hover:text-slate-100"
        >
          <PencilIcon className="h-4 w-4" aria-hidden="true" />
          Edit
        </button>
      }
    />
  );

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-white">Customers</h1>
        <Button
          onClick={() => { resetForm(); setShowModal(true); }}
          className="flex items-center"
        >
          <PlusIcon className="h-5 w-5 mr-2" />
          Add Customer
        </Button>
      </div>

      {/* Filters */}
      <div className="flex flex-col sm:flex-row gap-4">
        <div className="relative flex-1">
          <MagnifyingGlassIcon className="h-5 w-5 absolute left-3 top-1/2 transform -translate-y-1/2 text-slate-500" />
          <input
            type="text"
            placeholder="Search customers..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="input pl-10"
          />
        </div>
        <label className="flex items-center">
          <input
            type="checkbox"
            checked={showInactive}
            onChange={(e) => setShowInactive(e.target.checked)}
            className="mr-2 rounded border-slate-600"
          />
          <span className="text-sm text-slate-300">Show inactive</span>
        </label>
      </div>

      {/* Customers Table */}
      <DataTable
        columns={columns}
        data={filteredCustomers}
        rowKey={(customer) => customer.id}
        onRowClick={(customer) => viewCustomerDetails(customer)}
        defaultSort={{ key: 'name', dir: 'asc' }}
        pageSize={25}
        loading={loading}
        error={loadError}
        onRetry={loadCustomers}
        csvExport={{ filename: 'customers' }}
        mobileCards={renderMobileCard}
        empty={{
          icon: UserGroupIcon,
          title: 'No customers found',
          description: search ? 'No customers match your search.' : 'Add your first customer to get started.',
          action: search ? undefined : { label: 'Add your first customer', onClick: () => { resetForm(); setShowModal(true); } },
        }}
      />

      {/* Add/Edit Modal */}
      <Modal
        open={showModal}
        onClose={requestCloseModal}
        size="2xl"
        closeOnBackdrop={false}
      >
            <h3 className="text-lg font-semibold mb-4">
              {editingCustomer ? 'Edit Customer' : 'Add Customer'}
            </h3>
            <form onSubmit={handleSubmit} className="space-y-4">
              <FormField label="Customer Name" required>
                {(field) => (
                  <input
                    {...field}
                    type="text"
                    value={formData.name}
                    onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                    className="input"
                    required
                    autoFocus
                  />
                )}
              </FormField>

              <div className="grid grid-cols-2 gap-4">
                <FormField label="Contact Name">
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={formData.contact_name}
                      onChange={(e) => setFormData({ ...formData, contact_name: e.target.value })}
                      className="input"
                    />
                  )}
                </FormField>
                <FormField label="Email">
                  {(field) => (
                    <input
                      {...field}
                      type="email"
                      value={formData.email}
                      onChange={(e) => setFormData({ ...formData, email: e.target.value })}
                      className="input"
                    />
                  )}
                </FormField>
              </div>

              <FormField label="Phone">
                {(field) => (
                  <input
                    {...field}
                    type="text"
                    value={formData.phone}
                    onChange={(e) => setFormData({ ...formData, phone: e.target.value })}
                    className="input"
                  />
                )}
              </FormField>

              <div className="border-t pt-4">
                <h4 className="font-medium mb-2">Billing Address</h4>
                <div className="space-y-2">
                  <FormField label="Address Line 1">
                    {(field) => (
                      <input
                        {...field}
                        type="text"
                        value={formData.address_line1}
                        onChange={(e) => setFormData({ ...formData, address_line1: e.target.value })}
                        className="input"
                        placeholder="Address Line 1"
                      />
                    )}
                  </FormField>
                  <FormField label="Address Line 2">
                    {(field) => (
                      <input
                        {...field}
                        type="text"
                        value={formData.address_line2}
                        onChange={(e) => setFormData({ ...formData, address_line2: e.target.value })}
                        className="input"
                        placeholder="Address Line 2"
                      />
                    )}
                  </FormField>
                  <div className="grid grid-cols-3 gap-2">
                    <FormField label="City">
                      {(field) => (
                        <input
                          {...field}
                          type="text"
                          value={formData.city}
                          onChange={(e) => setFormData({ ...formData, city: e.target.value })}
                          className="input"
                          placeholder="City"
                        />
                      )}
                    </FormField>
                    <FormField label="State">
                      {(field) => (
                        <input
                          {...field}
                          type="text"
                          value={formData.state}
                          onChange={(e) => setFormData({ ...formData, state: e.target.value })}
                          className="input"
                          placeholder="State"
                        />
                      )}
                    </FormField>
                    <FormField label="ZIP">
                      {(field) => (
                        <input
                          {...field}
                          type="text"
                          value={formData.zip_code}
                          onChange={(e) => setFormData({ ...formData, zip_code: e.target.value })}
                          className="input"
                          placeholder="ZIP"
                        />
                      )}
                    </FormField>
                  </div>
                </div>
              </div>

              <div className="border-t pt-4">
                <h4 className="font-medium mb-2">Terms & Requirements</h4>
                <div className="grid grid-cols-2 gap-4">
                  <FormField label="Payment Terms">
                    {(field) => (
                      <select
                        {...field}
                        value={formData.payment_terms}
                        onChange={(e) => setFormData({ ...formData, payment_terms: e.target.value })}
                        className="input"
                      >
                        <option value="Net 30">Net 30</option>
                        <option value="Net 15">Net 15</option>
                        <option value="Net 45">Net 45</option>
                        <option value="Net 60">Net 60</option>
                        <option value="Due on Receipt">Due on Receipt</option>
                        <option value="COD">COD</option>
                      </select>
                    )}
                  </FormField>
                </div>
                <div className="flex gap-6 mt-3">
                  <label className="flex items-center">
                    <input
                      type="checkbox"
                      checked={formData.requires_coc}
                      onChange={(e) => setFormData({ ...formData, requires_coc: e.target.checked })}
                      className="mr-2 rounded border-slate-600"
                    />
                    <span className="text-sm">Requires COC</span>
                  </label>
                  <label className="flex items-center">
                    <input
                      type="checkbox"
                      checked={formData.requires_fai}
                      onChange={(e) => setFormData({ ...formData, requires_fai: e.target.checked })}
                      className="mr-2 rounded border-slate-600"
                    />
                    <span className="text-sm">Requires FAI</span>
                  </label>
                </div>
              </div>

              <FormField label="Special Requirements">
                {(field) => (
                  <textarea
                    {...field}
                    value={formData.special_requirements}
                    onChange={(e) => setFormData({ ...formData, special_requirements: e.target.value })}
                    className="input"
                    rows={2}
                  />
                )}
              </FormField>

              <FormField label="Notes">
                {(field) => (
                  <textarea
                    {...field}
                    value={formData.notes}
                    onChange={(e) => setFormData({ ...formData, notes: e.target.value })}
                    className="input"
                    rows={2}
                  />
                )}
              </FormField>

              <div className="flex justify-end gap-3 mt-6 pt-4 border-t">
                <Button variant="secondary" onClick={requestCloseModal} disabled={saving}>
                  Cancel
                </Button>
                <LoadingButton type="submit" loading={saving} loadingText="Saving...">
                  {editingCustomer ? 'Update' : 'Create'}
                </LoadingButton>
              </div>
            </form>
      </Modal>

      {/* Customer Detail Modal */}
      <Modal
        open={!!selectedCustomer}
        onClose={closeDetails}
        size="3xl"
        closeOnBackdrop={false}
        scroll={false}
        padded={false}
      >
        {selectedCustomer && (
          <>
            {/* Header */}
            <div className="px-6 py-4 border-b flex items-center justify-between bg-slate-800/50">
              <div className="flex items-center gap-3">
                <button onClick={closeDetails} className="text-slate-400 hover:text-slate-300" aria-label="Back to customers">
                  <ArrowLeftIcon className="h-5 w-5" aria-hidden="true" />
                </button>
                <div>
                  <h2 className="text-xl font-semibold">{selectedCustomer.name}</h2>
                  <p className="text-sm text-slate-400">Code: {selectedCustomer.code}</p>
                </div>
              </div>
              <button onClick={closeDetails} className="text-slate-500 hover:text-slate-400" aria-label="Close details">
                <XMarkIcon className="h-6 w-6" aria-hidden="true" />
              </button>
            </div>

            {/* Content */}
            <div className="p-6 overflow-y-auto flex-1 min-h-0">
              {loadingStats ? (
                <div className="flex items-center justify-center h-32">
                  <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-werco-primary"></div>
                </div>
              ) : customerStats ? (
                <div className="space-y-6">
                  {/* Stats Cards */}
                  <div className="grid grid-cols-3 gap-4">
                    <div className="bg-blue-500/10 rounded-lg p-4">
                      <div className="text-3xl font-bold text-blue-600">{customerStats.part_count}</div>
                      <div className="text-sm text-blue-300">Parts</div>
                    </div>
                    <div className="bg-green-500/10 rounded-lg p-4">
                      <div className="text-3xl font-bold text-green-600">{customerStats.work_order_counts.total}</div>
                      <div className="text-sm text-green-300">Total Work Orders</div>
                    </div>
                    <div className="bg-yellow-500/10 rounded-lg p-4">
                      <div className="text-3xl font-bold text-yellow-600">
                        {(customerStats.work_order_counts.by_status['in_progress'] || 0) + 
                         (customerStats.work_order_counts.by_status['released'] || 0)}
                      </div>
                      <div className="text-sm text-yellow-300">Active WOs</div>
                    </div>
                  </div>

                  {/* Work Order Status Breakdown */}
                  {Object.keys(customerStats.work_order_counts.by_status).length > 0 && (
                    <div>
                      <h3 className="text-sm font-medium text-slate-300 mb-2">Work Orders by Status</h3>
                      <div className="flex flex-wrap gap-2">
                        {Object.entries(customerStats.work_order_counts.by_status).map(([status, count]) => (
                          <span
                            key={status}
                            className={`px-3 py-1 rounded-full text-sm font-medium ${statusColor(status)}`}
                          >
                            {status.replace('_', ' ')}: {count}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Contact Info */}
                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <h3 className="text-sm font-medium text-slate-300 mb-2">Contact</h3>
                      <div className="text-sm space-y-1">
                        <p>{selectedCustomer.contact_name || '-'}</p>
                        <p className="text-slate-400">{selectedCustomer.email || '-'}</p>
                        <p className="text-slate-400">{selectedCustomer.phone || '-'}</p>
                      </div>
                    </div>
                    <div>
                      <h3 className="text-sm font-medium text-slate-300 mb-2">Address</h3>
                      <div className="text-sm text-slate-400">
                        {selectedCustomer.address_line1 && <p>{selectedCustomer.address_line1}</p>}
                        {selectedCustomer.city && selectedCustomer.state && (
                          <p>{selectedCustomer.city}, {selectedCustomer.state} {selectedCustomer.zip_code}</p>
                        )}
                      </div>
                    </div>
                  </div>

                  {/* Customer Menu */}
                  <div>
                    <h3 className="text-sm font-medium text-slate-300 mb-2">
                      Customer Menu: Parts, Assemblies, Current and Past Work Orders
                    </h3>
                    <div className="grid grid-cols-2 gap-3">
                      <div className="border rounded-lg bg-fd-panel">
                        <div className="px-3 py-2 border-b text-xs font-semibold text-slate-300 flex items-center justify-between">
                          <span>Assemblies</span>
                          <span>{customerStats.assemblies.length}</span>
                        </div>
                        <div className="max-h-40 overflow-y-auto divide-y divide-slate-700/30">
                          {customerStats.assemblies.length > 0 ? customerStats.assemblies.map(item => (
                            <div key={item.id} className="px-3 py-2 text-xs">
                              <div className="font-mono text-white">{item.part_number}</div>
                              <div className="text-slate-400 truncate">{item.name}</div>
                            </div>
                          )) : (
                            <div className="px-3 py-2 text-xs text-slate-400">No assemblies found</div>
                          )}
                        </div>
                      </div>

                      <div className="border rounded-lg bg-fd-panel">
                        <div className="px-3 py-2 border-b text-xs font-semibold text-slate-300 flex items-center justify-between">
                          <span>Parts</span>
                          <span>{customerStats.parts.length}</span>
                        </div>
                        <div className="max-h-40 overflow-y-auto divide-y divide-slate-700/30">
                          {customerStats.parts.length > 0 ? customerStats.parts.map(item => (
                            <div key={item.id} className="px-3 py-2 text-xs">
                              <div className="font-mono text-white">{item.part_number}</div>
                              <div className="text-slate-400 truncate">{item.name}</div>
                            </div>
                          )) : (
                            <div className="px-3 py-2 text-xs text-slate-400">No parts found</div>
                          )}
                        </div>
                      </div>

                      <div className="border rounded-lg bg-fd-panel">
                        <div className="px-3 py-2 border-b text-xs font-semibold text-slate-300 flex items-center justify-between">
                          <span>Current Work Orders</span>
                          <span>{customerStats.current_work_orders.length}</span>
                        </div>
                        <div className="max-h-44 overflow-y-auto divide-y divide-slate-700/30">
                          {customerStats.current_work_orders.length > 0 ? customerStats.current_work_orders.map(wo => (
                            <button
                              type="button"
                              key={wo.id}
                              className="w-full text-left px-3 py-2 text-xs hover:bg-slate-800/50"
                              onClick={() => {
                                closeDetails();
                                navigate(`/work-orders/${wo.id}`);
                              }}
                            >
                              <div className="flex items-center justify-between gap-2">
                                <span className="font-mono text-white">{wo.work_order_number}</span>
                                <span className={`px-2 py-0.5 rounded ${statusColor(wo.status)}`}>
                                  {wo.status.replace('_', ' ')}
                                </span>
                              </div>
                              <div className="text-slate-400 truncate">
                                {wo.part_number || 'No part'} • Qty {wo.quantity_ordered}
                              </div>
                            </button>
                          )) : (
                            <div className="px-3 py-2 text-xs text-slate-400">No current work orders</div>
                          )}
                        </div>
                      </div>

                      <div className="border rounded-lg bg-fd-panel">
                        <div className="px-3 py-2 border-b text-xs font-semibold text-slate-300 flex items-center justify-between">
                          <span>Past Work Orders</span>
                          <span>{customerStats.past_work_orders.length}</span>
                        </div>
                        <div className="max-h-44 overflow-y-auto divide-y divide-slate-700/30">
                          {customerStats.past_work_orders.length > 0 ? customerStats.past_work_orders.map(wo => (
                            <button
                              type="button"
                              key={wo.id}
                              className="w-full text-left px-3 py-2 text-xs hover:bg-slate-800/50"
                              onClick={() => {
                                closeDetails();
                                navigate(`/work-orders/${wo.id}`);
                              }}
                            >
                              <div className="flex items-center justify-between gap-2">
                                <span className="font-mono text-white">{wo.work_order_number}</span>
                                <span className={`px-2 py-0.5 rounded ${statusColor(wo.status)}`}>
                                  {wo.status.replace('_', ' ')}
                                </span>
                              </div>
                              <div className="text-slate-400 truncate">
                                {wo.part_number || 'No part'} • Qty {wo.quantity_ordered}
                              </div>
                            </button>
                          )) : (
                            <div className="px-3 py-2 text-xs text-slate-400">No past work orders</div>
                          )}
                        </div>
                      </div>
                    </div>
                  </div>

                  {customerStats.work_order_counts.total === 0 && customerStats.part_count === 0 && (
                    <EmptyState
                      title="Nothing on file yet"
                      description="No parts, assemblies, or work orders found for this customer."
                    />
                  )}
                </div>
              ) : statsError ? (
                <ErrorState
                  message="Could not load customer statistics."
                  onRetry={() => viewCustomerDetails(selectedCustomer)}
                />
              ) : null}
            </div>

            {/* Footer */}
            <div className="px-6 py-4 border-t bg-slate-800/50 flex justify-end gap-3">
              <Button variant="secondary" onClick={closeDetails}>Close</Button>
              <Button
                onClick={() => { closeDetails(); handleEdit(selectedCustomer); }}
              >
                Edit Customer
              </Button>
            </div>
          </>
        )}
      </Modal>
    </div>
  );
}
