import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../services/api';
import {
  PlusIcon,
  TrashIcon,
  MagnifyingGlassIcon,
  ArrowLeftIcon,
} from '@heroicons/react/24/outline';

interface SupplierMapping {
  id: number;
  supplier_part_number: string;
  part_id: number;
  part_number: string;
  part_name: string;
  part_description?: string;
  vendor_id?: number;
  vendor_name?: string;
  supplier_description?: string;
  supplier_uom?: string;
  conversion_factor: number;
  is_active: boolean;
}

interface Part {
  id: number;
  part_number: string;
  name: string;
}

interface Vendor {
  id: number;
  name: string;
}

export default function ScannerMappings() {
  const navigate = useNavigate();
  const [mappings, setMappings] = useState<SupplierMapping[]>([]);
  const [parts, setParts] = useState<Part[]>([]);
  const [vendors, setVendors] = useState<Vendor[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [showModal, setShowModal] = useState(false);

  const [formData, setFormData] = useState({
    supplier_part_number: '',
    part_id: 0,
    vendor_id: 0,
    supplier_description: '',
    supplier_uom: '',
    notes: ''
  });

  useEffect(() => {
    loadData();
  }, []);

  const loadData = async () => {
    try {
      const [mappingsRes, partsRes, vendorsRes] = await Promise.all([
        api.getSupplierMappings(),
        api.getParts({ active_only: true }),
        api.getVendors()
      ]);
      setMappings(mappingsRes);
      setParts(partsRes);
      setVendors(vendorsRes);
    } catch (err) {
      console.error('Failed to load data:', err);
    } finally {
      setLoading(false);
    }
  };

  const handleSearch = async () => {
    try {
      const response = await api.getSupplierMappings(search || undefined);
      setMappings(response);
    } catch (err) {
      console.error('Failed to search:', err);
    }
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!formData.part_id) {
      alert('Please select a part');
      return;
    }

    try {
      await api.createSupplierMapping({
        supplier_part_number: formData.supplier_part_number,
        part_id: formData.part_id,
        vendor_id: formData.vendor_id || null,
        supplier_description: formData.supplier_description || null,
        supplier_uom: formData.supplier_uom || null,
        notes: formData.notes || null
      });
      setShowModal(false);
      resetForm();
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to create mapping');
    }
  };

  const handleDelete = async (id: number) => {
    if (!window.confirm('Delete this mapping?')) return;
    try {
      await api.deleteSupplierMapping(id);
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to delete');
    }
  };

  const resetForm = () => {
    setFormData({
      supplier_part_number: '',
      part_id: 0,
      vendor_id: 0,
      supplier_description: '',
      supplier_uom: '',
      notes: ''
    });
  };

  const filteredMappings = mappings.filter(m => {
    if (!search) return true;
    const s = search.toLowerCase();
    return (
      m.supplier_part_number.toLowerCase().includes(s) ||
      m.part_number.toLowerCase().includes(s) ||
      m.part_name.toLowerCase().includes(s) ||
      m.supplier_description?.toLowerCase().includes(s) ||
      m.vendor_name?.toLowerCase().includes(s)
    );
  });

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
        <div className="flex items-center">
          <button onClick={() => navigate('/scanner')} className="mr-4 text-gray-500 hover:text-gray-700">
            <ArrowLeftIcon className="h-6 w-6" />
          </button>
          <h1 className="text-2xl font-bold text-gray-900">Supplier Part Mappings</h1>
        </div>
        <button
          onClick={() => { resetForm(); setShowModal(true); }}
          className="btn-primary flex items-center"
        >
          <PlusIcon className="h-5 w-5 mr-2" />
          Add Mapping
        </button>
      </div>

      <p className="text-gray-600">
        Map supplier barcodes and part numbers to your internal parts. When you scan a supplier code, 
        the system will look up the mapping and show your part information.
      </p>

      {/* Search */}
      <div className="relative max-w-md">
        <MagnifyingGlassIcon className="h-5 w-5 absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
          className="input pl-10"
          placeholder="Search mappings..."
        />
      </div>

      {/* Mappings Table */}
      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Supplier Code</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Internal Part</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Vendor</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Supplier Description</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">UOM</th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200">
              {filteredMappings.map((m) => (
                <tr key={m.id} className="hover:bg-gray-50">
                  <td className="px-4 py-4">
                    <span className="font-mono font-medium">{m.supplier_part_number}</span>
                  </td>
                  <td className="px-4 py-4">
                    <div className="font-medium text-werco-primary">{m.part_number}</div>
                    <div className="text-sm text-gray-500">{m.part_name}</div>
                  </td>
                  <td className="px-4 py-4 text-sm">{m.vendor_name || '-'}</td>
                  <td className="px-4 py-4 text-sm">{m.supplier_description || '-'}</td>
                  <td className="px-4 py-4 text-sm">{m.supplier_uom || '-'}</td>
                  <td className="px-4 py-4 text-center">
                    <button
                      onClick={() => handleDelete(m.id)}
                      className="text-red-500 hover:text-red-700"
                      title="Delete"
                    >
                      <TrashIcon className="h-5 w-5" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {filteredMappings.length === 0 && (
          <div className="text-center py-8 text-gray-500">
            {search ? 'No mappings found matching your search' : 'No supplier mappings yet. Add one to get started.'}
          </div>
        )}
      </div>

      {/* Add Mapping Modal */}
      {showModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-lg w-full mx-4">
            <h3 className="text-lg font-semibold mb-4">Add Supplier Mapping</h3>
            <form onSubmit={handleCreate} className="space-y-4">
              <div>
                <label className="label">Supplier Barcode/Part Number *</label>
                <input
                  type="text"
                  value={formData.supplier_part_number}
                  onChange={(e) => setFormData({ ...formData, supplier_part_number: e.target.value })}
                  className="input font-mono"
                  required
                  placeholder="Scan or type the supplier's code"
                />
              </div>

              <div>
                <label className="label">Map to Internal Part *</label>
                <select
                  value={formData.part_id}
                  onChange={(e) => setFormData({ ...formData, part_id: parseInt(e.target.value) })}
                  className="input"
                  required
                >
                  <option value={0}>Select a part...</option>
                  {parts.map(p => (
                    <option key={p.id} value={p.id}>
                      {p.part_number} - {p.name}
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label className="label">Vendor (Optional)</label>
                <select
                  value={formData.vendor_id}
                  onChange={(e) => setFormData({ ...formData, vendor_id: parseInt(e.target.value) })}
                  className="input"
                >
                  <option value={0}>Any vendor</option>
                  {vendors.map(v => (
                    <option key={v.id} value={v.id}>{v.name}</option>
                  ))}
                </select>
                <p className="text-xs text-gray-500 mt-1">
                  If specified, this mapping only applies to this vendor
                </p>
              </div>

              <div>
                <label className="label">Supplier Description</label>
                <input
                  type="text"
                  value={formData.supplier_description}
                  onChange={(e) => setFormData({ ...formData, supplier_description: e.target.value })}
                  className="input"
                  placeholder="e.g., 60x120 1/8in Carbon Steel Sheet"
                />
              </div>

              <div>
                <label className="label">Supplier Unit of Measure</label>
                <input
                  type="text"
                  value={formData.supplier_uom}
                  onChange={(e) => setFormData({ ...formData, supplier_uom: e.target.value })}
                  className="input"
                  placeholder="e.g., Sheet, Each, Lb"
                />
              </div>

              <div className="flex justify-end gap-3 pt-4 border-t">
                <button type="button" onClick={() => setShowModal(false)} className="btn-secondary">
                  Cancel
                </button>
                <button type="submit" className="btn-primary">
                  Create Mapping
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
