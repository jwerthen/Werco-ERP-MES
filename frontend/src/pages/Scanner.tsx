import React, { useState, useRef, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../services/api';
import {
  QrCodeIcon,
  MagnifyingGlassIcon,
  ClipboardDocumentListIcon,
  CubeIcon,
  PlayIcon,
  StopIcon,
  PlusIcon,
  LinkIcon,
} from '@heroicons/react/24/outline';

interface ScanResult {
  found: boolean;
  match_type?: string;
  part_id?: number;
  part_number?: string;
  part_name?: string;
  part_description?: string;
  part_type?: string;
  unit_of_measure?: string;
  supplier_part_number?: string;
  vendor_name?: string;
  supplier_description?: string;
  work_order_id?: number;
  work_order_number?: string;
  work_order_status?: string;
  quantity_ordered?: number;
  customer_name?: string;
  scanned_code: string;
}

interface Part {
  id: number;
  part_number: string;
  name: string;
  part_type: string;
}

interface Vendor {
  id: number;
  name: string;
}

export default function Scanner() {
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const [scanInput, setScanInput] = useState('');
  const [scanning, setScanning] = useState(true);
  const [result, setResult] = useState<ScanResult | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  
  // Manual mapping state
  const [showMappingModal, setShowMappingModal] = useState(false);
  const [parts, setParts] = useState<Part[]>([]);
  const [vendors, setVendors] = useState<Vendor[]>([]);
  const [mappingForm, setMappingForm] = useState({
    supplier_part_number: '',
    part_id: 0,
    vendor_id: 0,
    supplier_description: '',
    supplier_uom: '',
    notes: ''
  });

  // Auto-focus input for barcode scanner
  useEffect(() => {
    if (scanning && inputRef.current) {
      inputRef.current.focus();
    }
  }, [scanning]);

  // Re-focus on click anywhere
  useEffect(() => {
    const handleClick = () => {
      if (scanning && inputRef.current && !showMappingModal) {
        inputRef.current.focus();
      }
    };
    document.addEventListener('click', handleClick);
    return () => document.removeEventListener('click', handleClick);
  }, [scanning, showMappingModal]);

  const loadPartsAndVendors = async () => {
    try {
      const [partsRes, vendorsRes] = await Promise.all([
        api.getParts({ active_only: true }),
        api.getVendors()
      ]);
      setParts(partsRes);
      setVendors(vendorsRes);
    } catch (err) {
      console.error('Failed to load data:', err);
    }
  };

  const handleScan = async (value: string) => {
    if (!value.trim()) return;
    
    setLoading(true);
    setError('');
    setResult(null);
    
    try {
      const response = await api.scannerLookup(value.trim());
      setResult(response);
      
      if (!response.found) {
        // Prepare for manual mapping
        setMappingForm({
          ...mappingForm,
          supplier_part_number: value.trim()
        });
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Lookup failed');
    } finally {
      setLoading(false);
      setScanInput('');
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      handleScan(scanInput);
    }
  };

  const openMappingModal = async () => {
    await loadPartsAndVendors();
    setShowMappingModal(true);
  };

  const handleCreateMapping = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!mappingForm.part_id) {
      alert('Please select a part');
      return;
    }
    
    try {
      await api.createSupplierMapping({
        supplier_part_number: mappingForm.supplier_part_number,
        part_id: mappingForm.part_id,
        vendor_id: mappingForm.vendor_id || null,
        supplier_description: mappingForm.supplier_description || null,
        supplier_uom: mappingForm.supplier_uom || null,
        notes: mappingForm.notes || null
      });
      
      setShowMappingModal(false);
      // Re-scan to show the result
      handleScan(mappingForm.supplier_part_number);
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to create mapping');
    }
  };

  const goToWorkOrder = (woId: number) => {
    navigate(`/work-orders/${woId}`);
  };

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-gray-900">Barcode Scanner</h1>
        <div className="flex gap-2">
          <button
            onClick={() => navigate('/scanner/mappings')}
            className="btn-secondary flex items-center"
          >
            <LinkIcon className="h-5 w-5 mr-2" />
            Manage Mappings
          </button>
          <button
            onClick={() => setScanning(!scanning)}
            className={`flex items-center px-4 py-2 rounded-lg font-medium ${
              scanning ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-800'
            }`}
          >
            {scanning ? (
              <>
                <PlayIcon className="h-5 w-5 mr-2" />
                Scanning Active
              </>
            ) : (
              <>
                <StopIcon className="h-5 w-5 mr-2" />
                Scanning Paused
              </>
            )}
          </button>
        </div>
      </div>

      {/* Scanner Input */}
      <div className="card">
        <div className="flex items-center justify-center py-8">
          <div className="text-center">
            <QrCodeIcon className="h-16 w-16 mx-auto text-werco-primary mb-4" />
            <p className="text-lg font-medium text-gray-900 mb-2">
              {scanning ? 'Ready to Scan' : 'Scanning Paused'}
            </p>
            <p className="text-sm text-gray-500 mb-4">
              Scan a supplier barcode, part number, or work order
            </p>
            <div className="relative max-w-md mx-auto">
              <MagnifyingGlassIcon className="h-5 w-5 absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
              <input
                ref={inputRef}
                type="text"
                value={scanInput}
                onChange={(e) => setScanInput(e.target.value)}
                onKeyDown={handleKeyDown}
                className="input pl-10 text-center text-lg"
                placeholder="Scan or type code..."
                autoFocus
              />
            </div>
            <button
              onClick={() => handleScan(scanInput)}
              disabled={!scanInput.trim() || loading}
              className="btn-primary mt-4"
            >
              {loading ? 'Looking up...' : 'Look Up'}
            </button>
          </div>
        </div>
      </div>

      {/* Error */}
      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg">
          {error}
        </div>
      )}

      {/* Result - Found */}
      {result && result.found && (
        <div className="card">
          {(result.match_type === 'supplier_mapping' || result.match_type === 'part_number') && (
            <div>
              <div className="flex items-center mb-4">
                <CubeIcon className="h-8 w-8 text-werco-primary mr-3" />
                <div>
                  <h2 className="text-xl font-bold">{result.part_number}</h2>
                  <p className="text-gray-500">
                    {result.match_type === 'supplier_mapping' ? 'Matched via Supplier Code' : 'Part Number'}
                  </p>
                </div>
                <span className={`ml-auto px-3 py-1 rounded-full text-sm font-medium ${
                  result.part_type === 'manufactured' ? 'bg-blue-100 text-blue-800' :
                  result.part_type === 'assembly' ? 'bg-purple-100 text-purple-800' :
                  result.part_type === 'raw_material' ? 'bg-yellow-100 text-yellow-800' :
                  'bg-green-100 text-green-800'
                }`}>
                  {result.part_type?.replace('_', ' ')}
                </span>
              </div>
              
              <div className="grid grid-cols-2 gap-4 mb-4">
                <div>
                  <label className="text-sm text-gray-500">Name</label>
                  <p className="font-medium">{result.part_name}</p>
                </div>
                {result.part_description && (
                  <div>
                    <label className="text-sm text-gray-500">Description</label>
                    <p className="font-medium">{result.part_description}</p>
                  </div>
                )}
                {result.unit_of_measure && (
                  <div>
                    <label className="text-sm text-gray-500">Unit of Measure</label>
                    <p className="font-medium">{result.unit_of_measure}</p>
                  </div>
                )}
                {result.supplier_part_number && (
                  <div>
                    <label className="text-sm text-gray-500">Supplier Part #</label>
                    <p className="font-mono">{result.supplier_part_number}</p>
                  </div>
                )}
                {result.vendor_name && (
                  <div>
                    <label className="text-sm text-gray-500">Vendor</label>
                    <p className="font-medium">{result.vendor_name}</p>
                  </div>
                )}
                {result.supplier_description && (
                  <div className="col-span-2">
                    <label className="text-sm text-gray-500">Supplier Description</label>
                    <p className="font-medium">{result.supplier_description}</p>
                  </div>
                )}
              </div>

              <div className="flex gap-3 pt-4 border-t">
                <button
                  onClick={() => navigate('/parts')}
                  className="btn-primary"
                >
                  Go to Parts
                </button>
                <button
                  onClick={() => navigate('/inventory')}
                  className="btn-secondary"
                >
                  Check Inventory
                </button>
                <button
                  onClick={() => navigate('/purchasing')}
                  className="btn-secondary"
                >
                  Create PO
                </button>
              </div>
            </div>
          )}

          {result.match_type === 'work_order' && (
            <div>
              <div className="flex items-center mb-4">
                <ClipboardDocumentListIcon className="h-8 w-8 text-werco-primary mr-3" />
                <div>
                  <h2 className="text-xl font-bold">{result.work_order_number}</h2>
                  <p className="text-gray-500">Work Order</p>
                </div>
                <span className={`ml-auto px-3 py-1 rounded-full text-sm font-medium ${
                  result.work_order_status === 'in_progress' ? 'bg-green-100 text-green-800' :
                  result.work_order_status === 'released' ? 'bg-blue-100 text-blue-800' :
                  'bg-gray-100 text-gray-800'
                }`}>
                  {result.work_order_status?.replace('_', ' ')}
                </span>
              </div>
              
              <div className="grid grid-cols-2 gap-4 mb-4">
                <div>
                  <label className="text-sm text-gray-500">Part</label>
                  <p className="font-medium">{result.part_number}</p>
                  <p className="text-sm text-gray-600">{result.part_name}</p>
                </div>
                <div>
                  <label className="text-sm text-gray-500">Quantity</label>
                  <p className="font-medium">{result.quantity_ordered}</p>
                </div>
                {result.customer_name && (
                  <div>
                    <label className="text-sm text-gray-500">Customer</label>
                    <p className="font-medium">{result.customer_name}</p>
                  </div>
                )}
              </div>

              <div className="flex gap-3 pt-4 border-t">
                <button
                  onClick={() => goToWorkOrder(result.work_order_id!)}
                  className="btn-primary"
                >
                  View Work Order
                </button>
                <button
                  onClick={() => navigate('/shop-floor')}
                  className="btn-secondary"
                >
                  Go to Shop Floor
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Result - Not Found */}
      {result && !result.found && (
        <div className="card">
          <div className="text-center py-4">
            <div className="text-yellow-500 mb-4">
              <MagnifyingGlassIcon className="h-12 w-12 mx-auto" />
            </div>
            <h3 className="text-lg font-medium text-gray-900 mb-2">No Match Found</h3>
            <p className="text-gray-500 mb-4">
              Scanned code: <span className="font-mono font-bold">{result.scanned_code}</span>
            </p>
            <p className="text-sm text-gray-500 mb-6">
              This code is not mapped to any part in the system. Would you like to create a mapping?
            </p>
            <button
              onClick={openMappingModal}
              className="btn-primary flex items-center mx-auto"
            >
              <PlusIcon className="h-5 w-5 mr-2" />
              Create Mapping
            </button>
          </div>
        </div>
      )}

      {/* Info Box */}
      <div className="card bg-blue-50 border-blue-200">
        <h3 className="font-semibold mb-2 text-blue-900">How Scanning Works</h3>
        <ul className="text-sm text-blue-800 space-y-1">
          <li>1. Scan a supplier barcode, internal part number, or work order</li>
          <li>2. System checks supplier mappings first, then internal part numbers</li>
          <li>3. If no match found, you can create a mapping to link the supplier code to your part</li>
          <li>4. Future scans of that code will automatically show your part info</li>
        </ul>
      </div>

      {/* Create Mapping Modal */}
      {showMappingModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-lg w-full mx-4">
            <h3 className="text-lg font-semibold mb-4">Create Supplier Mapping</h3>
            <form onSubmit={handleCreateMapping} className="space-y-4">
              <div>
                <label className="label">Supplier Barcode/Part Number</label>
                <input
                  type="text"
                  value={mappingForm.supplier_part_number}
                  onChange={(e) => setMappingForm({ ...mappingForm, supplier_part_number: e.target.value })}
                  className="input font-mono"
                  required
                />
              </div>

              <div>
                <label className="label">Map to Internal Part *</label>
                <select
                  value={mappingForm.part_id}
                  onChange={(e) => setMappingForm({ ...mappingForm, part_id: parseInt(e.target.value) })}
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
                  value={mappingForm.vendor_id}
                  onChange={(e) => setMappingForm({ ...mappingForm, vendor_id: parseInt(e.target.value) })}
                  className="input"
                >
                  <option value={0}>Any vendor</option>
                  {vendors.map(v => (
                    <option key={v.id} value={v.id}>{v.name}</option>
                  ))}
                </select>
              </div>

              <div>
                <label className="label">Supplier Description</label>
                <input
                  type="text"
                  value={mappingForm.supplier_description}
                  onChange={(e) => setMappingForm({ ...mappingForm, supplier_description: e.target.value })}
                  className="input"
                  placeholder="e.g., 60x120 1/8in Carbon Steel Sheet"
                />
              </div>

              <div>
                <label className="label">Supplier Unit of Measure</label>
                <input
                  type="text"
                  value={mappingForm.supplier_uom}
                  onChange={(e) => setMappingForm({ ...mappingForm, supplier_uom: e.target.value })}
                  className="input"
                  placeholder="e.g., Sheet, Each, Lb"
                />
              </div>

              <div>
                <label className="label">Notes</label>
                <textarea
                  value={mappingForm.notes}
                  onChange={(e) => setMappingForm({ ...mappingForm, notes: e.target.value })}
                  className="input"
                  rows={2}
                />
              </div>

              <div className="flex justify-end gap-3 pt-4 border-t">
                <button type="button" onClick={() => setShowMappingModal(false)} className="btn-secondary">
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
