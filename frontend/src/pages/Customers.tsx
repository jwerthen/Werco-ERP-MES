import React, { useEffect, useState, useCallback } from 'react';
import api from '../services/api';
import { PlusIcon, PencilIcon, MagnifyingGlassIcon, XMarkIcon, ArrowLeftIcon } from '@heroicons/react/24/outline';

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
  recent_work_orders: Array<{
    id: number;
    work_order_number: string;
    status: string;
    due_date?: string;
    quantity_ordered: number;
    created_at: string;
  }>;
}

const statusColors: Record<string, string> = {
  draft: 'bg-gray-100 text-gray-800',
  released: 'bg-blue-100 text-blue-800',
  in_progress: 'bg-yellow-100 text-yellow-800',
  complete: 'bg-green-100 text-green-800',
  on_hold: 'bg-orange-100 text-orange-800',
  cancelled: 'bg-red-100 text-red-800',
  closed: 'bg-purple-100 text-purple-800',
};

export default function Customers() {
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [showInactive, setShowInactive] = useState(false);
  const [showModal, setShowModal] = useState(false);
  const [editingCustomer, setEditingCustomer] = useState<Customer | null>(null);
  const [selectedCustomer, setSelectedCustomer] = useState<Customer | null>(null);
  const [customerStats, setCustomerStats] = useState<CustomerStats | null>(null);
  const [loadingStats, setLoadingStats] = useState(false);

  const [formData, setFormData] = useState({
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
    notes: ''
  });

  const loadCustomers = useCallback(async () => {
    try {
      const response = await api.getCustomers(!showInactive);
      setCustomers(response);
    } catch (err) {
      console.error('Failed to load customers:', err);
    } finally {
      setLoading(false);
    }
  }, [showInactive]);

  useEffect(() => {
    loadCustomers();
  }, [loadCustomers]);

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
    try {
      const stats = await api.getCustomerStats(customer.id);
      setCustomerStats(stats);
    } catch (err) {
      console.error('Failed to load customer stats:', err);
      setCustomerStats(null);
    } finally {
      setLoadingStats(false);
    }
  };

  const closeDetails = () => {
    setSelectedCustomer(null);
    setCustomerStats(null);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      if (editingCustomer) {
        await api.updateCustomer(editingCustomer.id, formData);
      } else {
        await api.createCustomer(formData);
      }
      setShowModal(false);
      resetForm();
      loadCustomers();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to save customer');
    }
  };

  const handleEdit = (customer: Customer) => {
    setEditingCustomer(customer);
    setFormData({
      name: customer.name,
      contact_name: customer.contact_name || '',
      email: customer.email || '',
      phone: customer.phone || '',
      address_line1: customer.address_line1 || '',
      address_line2: '',
      city: customer.city || '',
      state: customer.state || '',
      zip_code: customer.zip_code || '',
      country: 'USA',
      ship_to_name: '',
      ship_address_line1: '',
      ship_city: '',
      ship_state: '',
      ship_zip_code: '',
      payment_terms: customer.payment_terms || 'Net 30',
      requires_coc: customer.requires_coc,
      requires_fai: customer.requires_fai,
      special_requirements: '',
      notes: ''
    });
    setShowModal(true);
  };

  const resetForm = () => {
    setEditingCustomer(null);
    setFormData({
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
      notes: ''
    });
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-werco-primary"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-gray-900">Customers</h1>
        <button
          onClick={() => { resetForm(); setShowModal(true); }}
          className="btn-primary flex items-center"
        >
          <PlusIcon className="h-5 w-5 mr-2" />
          Add Customer
        </button>
      </div>

      {/* Filters */}
      <div className="flex flex-col sm:flex-row gap-4">
        <div className="relative flex-1">
          <MagnifyingGlassIcon className="h-5 w-5 absolute left-3 top-1/2 transform -translate-y-1/2 text-gray-400" />
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
            className="mr-2 rounded border-gray-300"
          />
          <span className="text-sm text-gray-700">Show inactive</span>
        </label>
      </div>

      {/* Customers Table */}
      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Code</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Name</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Contact</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Location</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Terms</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Requirements</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Actions</th>
              </tr>
            </thead>
            <tbody className="bg-white divide-y divide-gray-200">
              {filteredCustomers.map((customer) => (
                <tr 
                  key={customer.id} 
                  className={`hover:bg-gray-50 cursor-pointer ${!customer.is_active ? 'opacity-60' : ''}`}
                  onClick={() => viewCustomerDetails(customer)}
                >
                  <td className="px-4 py-4 font-mono text-sm">{customer.code}</td>
                  <td className="px-4 py-4 font-medium text-werco-primary">{customer.name}</td>
                  <td className="px-4 py-4">
                    <div>
                      <div className="text-sm">{customer.contact_name || '-'}</div>
                      {customer.email && (
                        <div className="text-xs text-gray-500">{customer.email}</div>
                      )}
                    </div>
                  </td>
                  <td className="px-4 py-4 text-sm">
                    {customer.city && customer.state ? `${customer.city}, ${customer.state}` : '-'}
                  </td>
                  <td className="px-4 py-4 text-sm">{customer.payment_terms || '-'}</td>
                  <td className="px-4 py-4">
                    <div className="flex gap-1">
                      {customer.requires_coc && (
                        <span className="px-2 py-0.5 bg-blue-100 text-blue-800 text-xs rounded">COC</span>
                      )}
                      {customer.requires_fai && (
                        <span className="px-2 py-0.5 bg-purple-100 text-purple-800 text-xs rounded">FAI</span>
                      )}
                    </div>
                  </td>
                  <td className="px-4 py-4">
                    <span className={`inline-flex px-2 py-1 rounded text-xs font-medium ${
                      customer.is_active ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-600'
                    }`}>
                      {customer.is_active ? 'Active' : 'Inactive'}
                    </span>
                  </td>
                  <td className="px-4 py-4 text-center">
                    <button
                      onClick={(e) => { e.stopPropagation(); handleEdit(customer); }}
                      className="text-gray-400 hover:text-gray-600"
                    >
                      <PencilIcon className="h-5 w-5" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {filteredCustomers.length === 0 && (
          <div className="text-center py-8 text-gray-500">No customers found</div>
        )}
      </div>

      {/* Add/Edit Modal */}
      {showModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-2xl w-full mx-4 max-h-[90vh] overflow-y-auto">
            <h3 className="text-lg font-semibold mb-4">
              {editingCustomer ? 'Edit Customer' : 'Add Customer'}
            </h3>
            <form onSubmit={handleSubmit} className="space-y-4">
              <div>
                <label className="label">Customer Name *</label>
                <input
                  type="text"
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  className="input"
                  required
                />
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Contact Name</label>
                  <input
                    type="text"
                    value={formData.contact_name}
                    onChange={(e) => setFormData({ ...formData, contact_name: e.target.value })}
                    className="input"
                  />
                </div>
                <div>
                  <label className="label">Email</label>
                  <input
                    type="email"
                    value={formData.email}
                    onChange={(e) => setFormData({ ...formData, email: e.target.value })}
                    className="input"
                  />
                </div>
              </div>

              <div>
                <label className="label">Phone</label>
                <input
                  type="text"
                  value={formData.phone}
                  onChange={(e) => setFormData({ ...formData, phone: e.target.value })}
                  className="input"
                />
              </div>

              <div className="border-t pt-4">
                <h4 className="font-medium mb-2">Billing Address</h4>
                <div className="space-y-2">
                  <input
                    type="text"
                    value={formData.address_line1}
                    onChange={(e) => setFormData({ ...formData, address_line1: e.target.value })}
                    className="input"
                    placeholder="Address Line 1"
                  />
                  <input
                    type="text"
                    value={formData.address_line2}
                    onChange={(e) => setFormData({ ...formData, address_line2: e.target.value })}
                    className="input"
                    placeholder="Address Line 2"
                  />
                  <div className="grid grid-cols-3 gap-2">
                    <input
                      type="text"
                      value={formData.city}
                      onChange={(e) => setFormData({ ...formData, city: e.target.value })}
                      className="input"
                      placeholder="City"
                    />
                    <input
                      type="text"
                      value={formData.state}
                      onChange={(e) => setFormData({ ...formData, state: e.target.value })}
                      className="input"
                      placeholder="State"
                    />
                    <input
                      type="text"
                      value={formData.zip_code}
                      onChange={(e) => setFormData({ ...formData, zip_code: e.target.value })}
                      className="input"
                      placeholder="ZIP"
                    />
                  </div>
                </div>
              </div>

              <div className="border-t pt-4">
                <h4 className="font-medium mb-2">Terms & Requirements</h4>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="label">Payment Terms</label>
                    <select
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
                  </div>
                </div>
                <div className="flex gap-6 mt-3">
                  <label className="flex items-center">
                    <input
                      type="checkbox"
                      checked={formData.requires_coc}
                      onChange={(e) => setFormData({ ...formData, requires_coc: e.target.checked })}
                      className="mr-2 rounded border-gray-300"
                    />
                    <span className="text-sm">Requires COC</span>
                  </label>
                  <label className="flex items-center">
                    <input
                      type="checkbox"
                      checked={formData.requires_fai}
                      onChange={(e) => setFormData({ ...formData, requires_fai: e.target.checked })}
                      className="mr-2 rounded border-gray-300"
                    />
                    <span className="text-sm">Requires FAI</span>
                  </label>
                </div>
              </div>

              <div>
                <label className="label">Special Requirements</label>
                <textarea
                  value={formData.special_requirements}
                  onChange={(e) => setFormData({ ...formData, special_requirements: e.target.value })}
                  className="input"
                  rows={2}
                />
              </div>

              <div>
                <label className="label">Notes</label>
                <textarea
                  value={formData.notes}
                  onChange={(e) => setFormData({ ...formData, notes: e.target.value })}
                  className="input"
                  rows={2}
                />
              </div>

              <div className="flex justify-end gap-3 mt-6 pt-4 border-t">
                <button type="button" onClick={() => { setShowModal(false); resetForm(); }} className="btn-secondary">
                  Cancel
                </button>
                <button type="submit" className="btn-primary">
                  {editingCustomer ? 'Update' : 'Create'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Customer Detail Modal */}
      {selectedCustomer && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg max-w-3xl w-full mx-4 max-h-[90vh] overflow-hidden flex flex-col">
            {/* Header */}
            <div className="px-6 py-4 border-b flex items-center justify-between bg-gray-50">
              <div className="flex items-center gap-3">
                <button onClick={closeDetails} className="text-gray-500 hover:text-gray-700">
                  <ArrowLeftIcon className="h-5 w-5" />
                </button>
                <div>
                  <h2 className="text-xl font-semibold">{selectedCustomer.name}</h2>
                  <p className="text-sm text-gray-500">Code: {selectedCustomer.code}</p>
                </div>
              </div>
              <button onClick={closeDetails} className="text-gray-400 hover:text-gray-600">
                <XMarkIcon className="h-6 w-6" />
              </button>
            </div>

            {/* Content */}
            <div className="p-6 overflow-y-auto flex-1">
              {loadingStats ? (
                <div className="flex items-center justify-center h-32">
                  <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-werco-primary"></div>
                </div>
              ) : customerStats ? (
                <div className="space-y-6">
                  {/* Stats Cards */}
                  <div className="grid grid-cols-3 gap-4">
                    <div className="bg-blue-50 rounded-lg p-4">
                      <div className="text-3xl font-bold text-blue-600">{customerStats.part_count}</div>
                      <div className="text-sm text-blue-800">Parts</div>
                    </div>
                    <div className="bg-green-50 rounded-lg p-4">
                      <div className="text-3xl font-bold text-green-600">{customerStats.work_order_counts.total}</div>
                      <div className="text-sm text-green-800">Total Work Orders</div>
                    </div>
                    <div className="bg-yellow-50 rounded-lg p-4">
                      <div className="text-3xl font-bold text-yellow-600">
                        {(customerStats.work_order_counts.by_status['in_progress'] || 0) + 
                         (customerStats.work_order_counts.by_status['released'] || 0)}
                      </div>
                      <div className="text-sm text-yellow-800">Active WOs</div>
                    </div>
                  </div>

                  {/* Work Order Status Breakdown */}
                  {Object.keys(customerStats.work_order_counts.by_status).length > 0 && (
                    <div>
                      <h3 className="text-sm font-medium text-gray-700 mb-2">Work Orders by Status</h3>
                      <div className="flex flex-wrap gap-2">
                        {Object.entries(customerStats.work_order_counts.by_status).map(([status, count]) => (
                          <span 
                            key={status} 
                            className={`px-3 py-1 rounded-full text-sm font-medium ${statusColors[status] || 'bg-gray-100 text-gray-800'}`}
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
                      <h3 className="text-sm font-medium text-gray-700 mb-2">Contact</h3>
                      <div className="text-sm space-y-1">
                        <p>{selectedCustomer.contact_name || '-'}</p>
                        <p className="text-gray-500">{selectedCustomer.email || '-'}</p>
                        <p className="text-gray-500">{selectedCustomer.phone || '-'}</p>
                      </div>
                    </div>
                    <div>
                      <h3 className="text-sm font-medium text-gray-700 mb-2">Address</h3>
                      <div className="text-sm text-gray-600">
                        {selectedCustomer.address_line1 && <p>{selectedCustomer.address_line1}</p>}
                        {selectedCustomer.city && selectedCustomer.state && (
                          <p>{selectedCustomer.city}, {selectedCustomer.state} {selectedCustomer.zip_code}</p>
                        )}
                      </div>
                    </div>
                  </div>

                  {/* Recent Work Orders */}
                  {customerStats.recent_work_orders.length > 0 && (
                    <div>
                      <h3 className="text-sm font-medium text-gray-700 mb-2">Recent Work Orders</h3>
                      <div className="border rounded-lg overflow-hidden">
                        <table className="min-w-full divide-y divide-gray-200">
                          <thead className="bg-gray-50">
                            <tr>
                              <th className="px-4 py-2 text-left text-xs font-medium text-gray-500">WO #</th>
                              <th className="px-4 py-2 text-left text-xs font-medium text-gray-500">Status</th>
                              <th className="px-4 py-2 text-left text-xs font-medium text-gray-500">Qty</th>
                              <th className="px-4 py-2 text-left text-xs font-medium text-gray-500">Due Date</th>
                            </tr>
                          </thead>
                          <tbody className="divide-y divide-gray-200">
                            {customerStats.recent_work_orders.map(wo => (
                              <tr key={wo.id} className="hover:bg-gray-50">
                                <td className="px-4 py-2 text-sm font-medium text-werco-primary">{wo.work_order_number}</td>
                                <td className="px-4 py-2">
                                  <span className={`px-2 py-0.5 rounded text-xs font-medium ${statusColors[wo.status] || 'bg-gray-100'}`}>
                                    {wo.status.replace('_', ' ')}
                                  </span>
                                </td>
                                <td className="px-4 py-2 text-sm">{wo.quantity_ordered}</td>
                                <td className="px-4 py-2 text-sm text-gray-500">
                                  {wo.due_date ? new Date(wo.due_date).toLocaleDateString() : '-'}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  )}

                  {customerStats.recent_work_orders.length === 0 && customerStats.work_order_counts.total === 0 && (
                    <div className="text-center text-gray-500 py-8">
                      No work orders found for this customer
                    </div>
                  )}
                </div>
              ) : (
                <div className="text-center text-gray-500 py-8">
                  Failed to load customer statistics
                </div>
              )}
            </div>

            {/* Footer */}
            <div className="px-6 py-4 border-t bg-gray-50 flex justify-end gap-3">
              <button onClick={closeDetails} className="btn-secondary">Close</button>
              <button 
                onClick={() => { closeDetails(); handleEdit(selectedCustomer); }}
                className="btn-primary"
              >
                Edit Customer
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
