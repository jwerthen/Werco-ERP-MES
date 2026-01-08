import React, { useState, useCallback, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../services/api';
import {
  CloudArrowUpIcon,
  DocumentIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  XMarkIcon,
  ArrowPathIcon,
  DocumentCheckIcon,
} from '@heroicons/react/24/outline';

interface VendorMatch {
  matched: boolean;
  match_id: number | null;
  match_name: string;
  confidence: number;
  suggestions: Array<{ id: number; name: string; code: string; score: number }>;
}

interface PartMatch {
  matched: boolean;
  match_id: number | null;
  match_name: string;
  confidence: number;
  suggestions: Array<{ id: number; part_number: string; name: string; score: number }>;
}

interface LineItem {
  line_number: number;
  part_number: string;
  description: string;
  qty_ordered: number;
  unit_of_measure: string;
  unit_price: number;
  line_total: number;
  confidence: string;
  part_match: PartMatch | null;
  matched_part_id: number | null;
  // Form state
  selected_part_id: number | null;
  create_new_part: boolean;
  new_part_type: 'purchased' | 'raw_material';
}

interface ExtractionResult {
  po_number: string;
  vendor: { name: string; address: string };
  vendor_match: VendorMatch | null;
  matched_vendor_id: number | null;
  order_date: string | null;
  expected_delivery_date: string | null;
  required_date: string | null;
  payment_terms: string | null;
  shipping_method: string | null;
  ship_to: string | null;
  line_items: LineItem[];
  subtotal: number | null;
  tax: number | null;
  shipping_cost: number | null;
  total_amount: number | null;
  notes: string | null;
  extraction_confidence: string;
  pdf_was_ocr: boolean;
  pdf_page_count: number;
  pdf_path: string;
  validation_issues: Array<{ field: string; severity: string; message: string }>;
  po_number_exists: boolean;
}

type Step = 'upload' | 'processing' | 'review' | 'success';

export default function POUpload() {
  const navigate = useNavigate();
  const [step, setStep] = useState<Step>('upload');
  const [file, setFile] = useState<File | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [error, setError] = useState('');
  const [extractionResult, setExtractionResult] = useState<ExtractionResult | null>(null);
  
  // Form state for review
  const [formData, setFormData] = useState({
    po_number: '',
    vendor_id: null as number | null,
    create_vendor: false,
    new_vendor_name: '',
    new_vendor_code: '',
    new_vendor_address: '',
    order_date: '',
    required_date: '',
    expected_date: '',
    shipping_method: '',
    ship_to: '',
    notes: '',
  });
  const [lineItems, setLineItems] = useState<LineItem[]>([]);
  const [vendorSearch, setVendorSearch] = useState('');
  const [vendorResults, setVendorResults] = useState<any[]>([]);
  const [partSearches, setPartSearches] = useState<{ [key: number]: string }>({});
  const [partResults, setPartResults] = useState<{ [key: number]: any[] }>({});
  const [createdPO, setCreatedPO] = useState<{ id: number; number: string } | null>(null);

  // Handle file drop
  const handleDrag = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.type === 'dragenter' || e.type === 'dragover') {
      setDragActive(true);
    } else if (e.type === 'dragleave') {
      setDragActive(false);
    }
  }, []);

  const ALLOWED_EXTENSIONS = ['.pdf', '.doc', '.docx'];
  const ALLOWED_MIME_TYPES = [
    'application/pdf',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
  ];

  const isValidFile = (file: File) => {
    const ext = '.' + file.name.toLowerCase().split('.').pop();
    return ALLOWED_EXTENSIONS.includes(ext) || ALLOWED_MIME_TYPES.includes(file.type);
  };

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);
    
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      const droppedFile = e.dataTransfer.files[0];
      if (isValidFile(droppedFile)) {
        setFile(droppedFile);
        setError('');
      } else {
        setError('Only PDF and Word documents (.pdf, .doc, .docx) are supported');
      }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      const selectedFile = e.target.files[0];
      if (isValidFile(selectedFile)) {
        setFile(selectedFile);
        setError('');
      } else {
        setError('Only PDF and Word documents (.pdf, .doc, .docx) are supported');
      }
    }
  };

  const handleUpload = async () => {
    if (!file) return;
    
    setStep('processing');
    setError('');
    
    try {
      const result = await api.uploadPOPdf(file);
      setExtractionResult(result);
      
      // Initialize form data from extraction
      setFormData({
        po_number: result.po_number || '',
        vendor_id: result.matched_vendor_id,
        create_vendor: false,
        new_vendor_name: result.vendor?.name || '',
        new_vendor_code: '',
        new_vendor_address: result.vendor?.address || '',
        order_date: result.order_date || '',
        required_date: result.required_date || '',
        expected_date: result.expected_delivery_date || '',
        shipping_method: result.shipping_method || '',
        ship_to: result.ship_to || '',
        notes: result.notes || '',
      });
      
      // Initialize line items with match info
      setLineItems(result.line_items.map((item: any) => ({
        ...item,
        selected_part_id: item.matched_part_id,
        create_new_part: false,
        new_part_type: 'purchased' as const,
      })));
      
      setStep('review');
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to process PDF');
      setStep('upload');
    }
  };

  // Search vendors
  useEffect(() => {
    if (vendorSearch.length >= 2) {
      const timeout = setTimeout(async () => {
        try {
          const results = await api.searchVendorsForPO(vendorSearch);
          setVendorResults(results);
        } catch (err) {
          console.error('Vendor search failed:', err);
        }
      }, 300);
      return () => clearTimeout(timeout);
    } else {
      setVendorResults([]);
    }
  }, [vendorSearch]);

  // Search parts for a specific line
  const searchParts = async (lineIndex: number, query: string) => {
    setPartSearches(prev => ({ ...prev, [lineIndex]: query }));
    
    if (query.length >= 2) {
      try {
        const results = await api.searchPartsForPO(query);
        setPartResults(prev => ({ ...prev, [lineIndex]: results }));
      } catch (err) {
        console.error('Part search failed:', err);
      }
    } else {
      setPartResults(prev => ({ ...prev, [lineIndex]: [] }));
    }
  };

  const selectPartForLine = (lineIndex: number, partId: number, partNumber: string) => {
    setLineItems(prev => prev.map((item, idx) => 
      idx === lineIndex 
        ? { ...item, selected_part_id: partId, part_number: partNumber, create_new_part: false }
        : item
    ));
    setPartResults(prev => ({ ...prev, [lineIndex]: [] }));
    setPartSearches(prev => ({ ...prev, [lineIndex]: '' }));
  };

  const toggleCreatePart = (lineIndex: number) => {
    setLineItems(prev => prev.map((item, idx) => 
      idx === lineIndex 
        ? { ...item, create_new_part: !item.create_new_part, selected_part_id: null, new_part_type: 'purchased' }
        : item
    ));
  };

  const setPartType = (lineIndex: number, partType: 'purchased' | 'raw_material') => {
    setLineItems(prev => prev.map((item, idx) => 
      idx === lineIndex 
        ? { ...item, new_part_type: partType }
        : item
    ));
  };

  const handleCreatePO = async () => {
    setError('');
    
    // Validate
    if (!formData.po_number) {
      setError('PO number is required');
      return;
    }
    
    if (!formData.vendor_id && !formData.create_vendor) {
      setError('Please select a vendor or create a new one');
      return;
    }
    
    // Check all line items have parts
    const unmatchedLines = lineItems.filter(item => !item.selected_part_id && !item.create_new_part);
    if (unmatchedLines.length > 0) {
      setError(`${unmatchedLines.length} line(s) need part assignment`);
      return;
    }
    
    try {
      const partsToCreate = lineItems
        .filter(item => item.create_new_part)
        .map(item => ({
          part_number: item.part_number,
          description: item.description,
          part_type: item.new_part_type
        }));
      
      const result = await api.createPOFromUpload({
        po_number: formData.po_number,
        vendor_id: formData.vendor_id || 0,
        create_vendor: formData.create_vendor,
        new_vendor_name: formData.new_vendor_name,
        new_vendor_code: formData.new_vendor_code,
        new_vendor_address: formData.new_vendor_address,
        order_date: formData.order_date || undefined,
        required_date: formData.required_date || undefined,
        expected_date: formData.expected_date || undefined,
        shipping_method: formData.shipping_method || undefined,
        ship_to: formData.ship_to || undefined,
        notes: formData.notes || undefined,
        line_items: lineItems.map(item => ({
          part_id: item.selected_part_id || 0,
          part_number: item.part_number,
          description: item.description,
          quantity_ordered: item.qty_ordered,
          unit_price: item.unit_price,
          line_total: item.line_total,
        })),
        create_parts: partsToCreate,
        pdf_path: extractionResult?.pdf_path || '',
      });
      
      if (result.success) {
        setCreatedPO({ id: result.po_id, number: result.po_number });
        setStep('success');
      } else {
        setError(result.message || 'Failed to create PO');
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to create PO');
    }
  };

  const getConfidenceBadge = (confidence: string) => {
    const colors = {
      high: 'bg-green-100 text-green-800',
      medium: 'bg-amber-100 text-amber-800',
      low: 'bg-red-100 text-red-800',
    };
    return colors[confidence as keyof typeof colors] || colors.medium;
  };

  // UPLOAD STEP
  if (step === 'upload') {
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900">Upload Purchase Order</h1>
          <p className="text-gray-500 mt-1">Upload a PDF or Word document to automatically extract data</p>
        </div>

        {error && (
          <div className="bg-red-50 border border-red-200 rounded-xl p-4 flex items-center gap-3">
            <ExclamationTriangleIcon className="h-5 w-5 text-red-600" />
            <span className="text-red-800">{error}</span>
          </div>
        )}

        <div className="card">
          <div
            className={`border-2 border-dashed rounded-xl p-12 text-center transition-colors ${
              dragActive 
                ? 'border-werco-primary bg-werco-50' 
                : file 
                  ? 'border-green-400 bg-green-50' 
                  : 'border-gray-300 hover:border-gray-400'
            }`}
            onDragEnter={handleDrag}
            onDragLeave={handleDrag}
            onDragOver={handleDrag}
            onDrop={handleDrop}
          >
            {file ? (
              <div className="flex flex-col items-center">
                <DocumentIcon className="h-16 w-16 text-green-500 mb-4" />
                <p className="text-lg font-medium text-gray-900">{file.name}</p>
                <p className="text-sm text-gray-500 mt-1">
                  {(file.size / 1024 / 1024).toFixed(2)} MB
                </p>
                <button
                  onClick={() => setFile(null)}
                  className="mt-4 text-sm text-red-600 hover:text-red-800"
                >
                  Remove file
                </button>
              </div>
            ) : (
              <>
                <CloudArrowUpIcon className="h-16 w-16 text-gray-400 mx-auto mb-4" />
                <p className="text-lg font-medium text-gray-900">
                  Drag and drop your PO document here
                </p>
                <p className="text-sm text-gray-500 mt-1">or</p>
                <label className="mt-4 inline-block">
                  <span className="btn-primary cursor-pointer">Browse Files</span>
                  <input
                    type="file"
                    accept=".pdf,.doc,.docx,application/pdf,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    onChange={handleFileSelect}
                    className="hidden"
                  />
                </label>
                <p className="text-xs text-gray-400 mt-4">Supported: PDF, DOC, DOCX (max 10MB)</p>
              </>
            )}
          </div>

          {file && (
            <div className="mt-6 flex justify-end">
              <button onClick={handleUpload} className="btn-primary px-8">
                Extract Data
              </button>
            </div>
          )}
        </div>

        <div className="card bg-blue-50 border-blue-200">
          <h3 className="font-semibold text-blue-800 mb-2">How it works</h3>
          <ol className="list-decimal list-inside space-y-1 text-sm text-blue-700">
            <li>Upload your purchase order (PDF or Word document)</li>
            <li>AI extracts vendor, line items, and order details</li>
            <li>Review and verify the extracted data</li>
            <li>Match parts to your inventory or create new ones</li>
            <li>Confirm to create the PO in your system</li>
          </ol>
        </div>
      </div>
    );
  }

  // PROCESSING STEP
  if (step === 'processing') {
    return (
      <div className="flex flex-col items-center justify-center min-h-[400px]">
        <ArrowPathIcon className="h-16 w-16 text-werco-primary animate-spin mb-6" />
        <h2 className="text-xl font-semibold text-gray-900">Processing PDF...</h2>
        <p className="text-gray-500 mt-2">Extracting data with AI. This may take a moment.</p>
      </div>
    );
  }

  // SUCCESS STEP
  if (step === 'success' && createdPO) {
    return (
      <div className="flex flex-col items-center justify-center min-h-[400px]">
        <CheckCircleIcon className="h-20 w-20 text-green-500 mb-6" />
        <h2 className="text-2xl font-bold text-gray-900">Purchase Order Created!</h2>
        <p className="text-gray-600 mt-2">PO {createdPO.number} has been created successfully</p>
        <div className="flex gap-4 mt-8">
          <button
            onClick={() => navigate(`/purchasing`)}
            className="btn-secondary px-6"
          >
            Go to Purchasing
          </button>
          <button
            onClick={() => navigate(`/receiving`)}
            className="btn-primary px-6"
          >
            Go to Receiving
          </button>
        </div>
      </div>
    );
  }

  // REVIEW STEP
  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-3xl font-bold text-gray-900">Review Extracted Data</h1>
          <p className="text-gray-500 mt-1">Verify and edit the extracted information before creating the PO</p>
        </div>
        <div className="flex items-center gap-3">
          {extractionResult?.pdf_was_ocr && (
            <span className="px-3 py-1 rounded-full text-sm bg-amber-100 text-amber-800">
              OCR Processed
            </span>
          )}
          <span className={`px-3 py-1 rounded-full text-sm ${getConfidenceBadge(extractionResult?.extraction_confidence || 'medium')}`}>
            {extractionResult?.extraction_confidence} confidence
          </span>
        </div>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-xl p-4 flex items-center gap-3">
          <ExclamationTriangleIcon className="h-5 w-5 text-red-600" />
          <span className="text-red-800">{error}</span>
          <button onClick={() => setError('')} className="ml-auto">
            <XMarkIcon className="h-5 w-5 text-red-600" />
          </button>
        </div>
      )}

      {/* Validation Issues */}
      {extractionResult?.validation_issues && extractionResult.validation_issues.length > 0 && (
        <div className="bg-amber-50 border border-amber-200 rounded-xl p-4">
          <h3 className="font-semibold text-amber-800 mb-2">Issues to Review</h3>
          <ul className="space-y-1">
            {extractionResult.validation_issues.map((issue, idx) => (
              <li key={idx} className={`text-sm ${issue.severity === 'error' ? 'text-red-700' : 'text-amber-700'}`}>
                • {issue.message}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="grid grid-cols-3 gap-6">
        {/* Left: PDF Preview placeholder */}
        <div className="col-span-1">
          <div className="card sticky top-4">
            <h3 className="font-semibold mb-4">Source Document</h3>
            <div className="bg-gray-100 rounded-lg p-4 text-center">
              <DocumentIcon className="h-12 w-12 text-gray-400 mx-auto mb-2" />
              <p className="text-sm text-gray-600">{file?.name}</p>
              <p className="text-xs text-gray-400 mt-1">
                {extractionResult?.pdf_page_count} page(s)
              </p>
              {extractionResult?.pdf_path && (
                <a
                  href={api.getPOPdfUrl(extractionResult.pdf_path.replace('uploads/purchase_orders/', ''))}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="btn-secondary text-sm mt-4 inline-block"
                >
                  Open PDF
                </a>
              )}
            </div>
          </div>
        </div>

        {/* Right: Form */}
        <div className="col-span-2 space-y-6">
          {/* Header Info */}
          <div className="card">
            <h3 className="font-semibold mb-4">PO Information</h3>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  PO Number <span className="text-red-500">*</span>
                </label>
                <input
                  type="text"
                  value={formData.po_number}
                  onChange={(e) => setFormData({ ...formData, po_number: e.target.value })}
                  className={`input w-full ${extractionResult?.po_number_exists ? 'border-red-300' : ''}`}
                />
                {extractionResult?.po_number_exists && (
                  <p className="text-xs text-red-600 mt-1">This PO number already exists</p>
                )}
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Order Date</label>
                <input
                  type="date"
                  value={formData.order_date}
                  onChange={(e) => setFormData({ ...formData, order_date: e.target.value })}
                  className="input w-full"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Required Date</label>
                <input
                  type="date"
                  value={formData.required_date}
                  onChange={(e) => setFormData({ ...formData, required_date: e.target.value })}
                  className="input w-full"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Expected Delivery</label>
                <input
                  type="date"
                  value={formData.expected_date}
                  onChange={(e) => setFormData({ ...formData, expected_date: e.target.value })}
                  className="input w-full"
                />
              </div>
            </div>
          </div>

          {/* Vendor Section */}
          <div className="card">
            <h3 className="font-semibold mb-4">Vendor</h3>
            
            {extractionResult?.vendor_match?.matched && !formData.create_vendor ? (
              <div className="bg-green-50 border border-green-200 rounded-lg p-4 mb-4">
                <div className="flex justify-between items-center">
                  <div>
                    <p className="font-medium text-green-800">
                      Matched: {extractionResult.vendor_match.match_name}
                    </p>
                    <p className="text-sm text-green-600">
                      Confidence: {extractionResult.vendor_match.confidence}%
                    </p>
                  </div>
                  <button
                    onClick={() => setFormData({ ...formData, vendor_id: null })}
                    className="text-sm text-green-700 hover:underline"
                  >
                    Change
                  </button>
                </div>
              </div>
            ) : !formData.create_vendor ? (
              <div className="space-y-3">
                <p className="text-sm text-amber-600">
                  Extracted vendor: "{extractionResult?.vendor?.name}" - No exact match found
                </p>
                
                {/* Suggestions */}
                {extractionResult?.vendor_match?.suggestions && extractionResult.vendor_match.suggestions.length > 0 && (
                  <div className="space-y-2">
                    <p className="text-sm font-medium">Suggestions:</p>
                    {extractionResult.vendor_match.suggestions.map((sug) => (
                      <button
                        key={sug.id}
                        onClick={() => setFormData({ ...formData, vendor_id: sug.id })}
                        className="block w-full text-left px-3 py-2 rounded-lg border hover:bg-gray-50"
                      >
                        <span className="font-medium">{sug.name}</span>
                        <span className="text-gray-500 text-sm ml-2">({sug.code})</span>
                        <span className="text-xs text-gray-400 ml-2">{sug.score}% match</span>
                      </button>
                    ))}
                  </div>
                )}
                
                {/* Search */}
                <div className="relative">
                  <input
                    type="text"
                    value={vendorSearch}
                    onChange={(e) => setVendorSearch(e.target.value)}
                    placeholder="Search vendors..."
                    className="input w-full"
                  />
                  {vendorResults.length > 0 && (
                    <div className="absolute z-10 w-full mt-1 bg-white border rounded-lg shadow-lg max-h-48 overflow-y-auto">
                      {vendorResults.map((v) => (
                        <button
                          key={v.id}
                          onClick={() => {
                            setFormData({ ...formData, vendor_id: v.id });
                            setVendorSearch('');
                            setVendorResults([]);
                          }}
                          className="block w-full text-left px-4 py-2 hover:bg-gray-50"
                        >
                          {v.name} ({v.code})
                        </button>
                      ))}
                    </div>
                  )}
                </div>
                
                <button
                  onClick={() => setFormData({ ...formData, create_vendor: true })}
                  className="text-sm text-werco-primary hover:underline"
                >
                  + Create new vendor
                </button>
              </div>
            ) : (
              <div className="space-y-3 bg-blue-50 border border-blue-200 rounded-lg p-4">
                <div className="flex justify-between items-center">
                  <p className="font-medium text-blue-800">Create New Vendor</p>
                  <button
                    onClick={() => setFormData({ ...formData, create_vendor: false })}
                    className="text-sm text-blue-700 hover:underline"
                  >
                    Cancel
                  </button>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">Name *</label>
                    <input
                      type="text"
                      value={formData.new_vendor_name}
                      onChange={(e) => setFormData({ ...formData, new_vendor_name: e.target.value })}
                      className="input w-full"
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">Code</label>
                    <input
                      type="text"
                      value={formData.new_vendor_code}
                      onChange={(e) => setFormData({ ...formData, new_vendor_code: e.target.value })}
                      className="input w-full"
                      placeholder="Auto-generated if blank"
                    />
                  </div>
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Address</label>
                  <input
                    type="text"
                    value={formData.new_vendor_address}
                    onChange={(e) => setFormData({ ...formData, new_vendor_address: e.target.value })}
                    className="input w-full"
                  />
                </div>
              </div>
            )}
            
            {formData.vendor_id && !formData.create_vendor && (
              <div className="mt-3 text-sm text-green-600">
                ✓ Vendor selected (ID: {formData.vendor_id})
              </div>
            )}
          </div>

          {/* Line Items */}
          <div className="card">
            <h3 className="font-semibold mb-4">Line Items ({lineItems.length})</h3>
            <div className="space-y-4">
              {lineItems.map((item, idx) => (
                <div 
                  key={idx} 
                  className={`border rounded-xl p-4 ${
                    item.confidence === 'low' ? 'border-amber-300 bg-amber-50' : 'border-gray-200'
                  }`}
                >
                  <div className="flex justify-between items-start mb-3">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium text-gray-500">Line {item.line_number}</span>
                      <span className={`px-2 py-0.5 rounded text-xs ${getConfidenceBadge(item.confidence)}`}>
                        {item.confidence}
                      </span>
                    </div>
                    <div className="text-right">
                      <p className="font-semibold">${item.line_total?.toFixed(2) || '0.00'}</p>
                      <p className="text-xs text-gray-500">
                        {item.qty_ordered} × ${item.unit_price?.toFixed(2) || '0.00'}
                      </p>
                    </div>
                  </div>
                  
                  <div className="grid grid-cols-3 gap-4 mb-3">
                    <div>
                      <label className="block text-xs text-gray-500 mb-1">Part Number</label>
                      <input
                        type="text"
                        value={item.part_number}
                        onChange={(e) => setLineItems(prev => prev.map((it, i) => 
                          i === idx ? { ...it, part_number: e.target.value } : it
                        ))}
                        className="input w-full text-sm"
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-gray-500 mb-1">Quantity</label>
                      <input
                        type="number"
                        value={item.qty_ordered}
                        onChange={(e) => setLineItems(prev => prev.map((it, i) => 
                          i === idx ? { ...it, qty_ordered: parseFloat(e.target.value) || 0 } : it
                        ))}
                        className="input w-full text-sm"
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-gray-500 mb-1">Unit Price</label>
                      <input
                        type="number"
                        step="0.01"
                        value={item.unit_price}
                        onChange={(e) => setLineItems(prev => prev.map((it, i) => 
                          i === idx ? { ...it, unit_price: parseFloat(e.target.value) || 0 } : it
                        ))}
                        className="input w-full text-sm"
                      />
                    </div>
                  </div>
                  
                  <div className="mb-3">
                    <label className="block text-xs text-gray-500 mb-1">Description</label>
                    <input
                      type="text"
                      value={item.description}
                      onChange={(e) => setLineItems(prev => prev.map((it, i) => 
                        i === idx ? { ...it, description: e.target.value } : it
                      ))}
                      className="input w-full text-sm"
                    />
                  </div>

                  {/* Part Matching */}
                  <div className="bg-gray-50 rounded-lg p-3">
                    {item.selected_part_id ? (
                      <div className="flex justify-between items-center">
                        <span className="text-sm text-green-600">
                          ✓ Matched to part ID: {item.selected_part_id}
                        </span>
                        <button
                          onClick={() => setLineItems(prev => prev.map((it, i) => 
                            i === idx ? { ...it, selected_part_id: null } : it
                          ))}
                          className="text-xs text-gray-500 hover:underline"
                        >
                          Change
                        </button>
                      </div>
                    ) : item.create_new_part ? (
                      <div className="space-y-2">
                        <div className="flex justify-between items-center">
                          <span className="text-sm text-blue-600">
                            Will create new part: {item.part_number}
                          </span>
                          <button
                            onClick={() => toggleCreatePart(idx)}
                            className="text-xs text-gray-500 hover:underline"
                          >
                            Cancel
                          </button>
                        </div>
                        <div className="flex items-center gap-4">
                          <span className="text-xs text-gray-500">Part Type:</span>
                          <label className="flex items-center gap-1.5 cursor-pointer">
                            <input
                              type="radio"
                              name={`part-type-${idx}`}
                              checked={item.new_part_type === 'purchased'}
                              onChange={() => setPartType(idx, 'purchased')}
                              className="text-rose-600"
                            />
                            <span className="text-sm">Purchased Part</span>
                          </label>
                          <label className="flex items-center gap-1.5 cursor-pointer">
                            <input
                              type="radio"
                              name={`part-type-${idx}`}
                              checked={item.new_part_type === 'raw_material'}
                              onChange={() => setPartType(idx, 'raw_material')}
                              className="text-rose-600"
                            />
                            <span className="text-sm">Raw Material</span>
                          </label>
                        </div>
                      </div>
                    ) : (
                      <div className="space-y-2">
                        <div className="flex items-center gap-2">
                          <ExclamationTriangleIcon className="h-4 w-4 text-amber-500" />
                          <span className="text-sm text-amber-600">Part not matched</span>
                        </div>
                        
                        {/* Suggestions */}
                        {item.part_match?.suggestions && item.part_match.suggestions.length > 0 && (
                          <div className="flex flex-wrap gap-2">
                            {item.part_match.suggestions.slice(0, 3).map((sug) => (
                              <button
                                key={sug.id}
                                onClick={() => selectPartForLine(idx, sug.id, sug.part_number)}
                                className="text-xs px-2 py-1 rounded bg-white border hover:bg-gray-50"
                              >
                                {sug.part_number} ({sug.score}%)
                              </button>
                            ))}
                          </div>
                        )}
                        
                        {/* Search */}
                        <div className="relative">
                          <input
                            type="text"
                            value={partSearches[idx] || ''}
                            onChange={(e) => searchParts(idx, e.target.value)}
                            placeholder="Search parts..."
                            className="input w-full text-sm"
                          />
                          {partResults[idx]?.length > 0 && (
                            <div className="absolute z-10 w-full mt-1 bg-white border rounded-lg shadow-lg max-h-32 overflow-y-auto">
                              {partResults[idx].map((p: any) => (
                                <button
                                  key={p.id}
                                  onClick={() => selectPartForLine(idx, p.id, p.part_number)}
                                  className="block w-full text-left px-3 py-2 text-sm hover:bg-gray-50"
                                >
                                  {p.part_number} - {p.name}
                                </button>
                              ))}
                            </div>
                          )}
                        </div>
                        
                        <button
                          onClick={() => toggleCreatePart(idx)}
                          className="text-xs text-werco-primary hover:underline"
                        >
                          + Create as new part
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
            
            {/* Totals */}
            <div className="mt-6 pt-4 border-t">
              <div className="flex justify-end">
                <div className="w-64 space-y-2">
                  <div className="flex justify-between text-sm">
                    <span>Subtotal:</span>
                    <span>${lineItems.reduce((sum, item) => sum + (item.line_total || 0), 0).toFixed(2)}</span>
                  </div>
                  {extractionResult?.tax && (
                    <div className="flex justify-between text-sm">
                      <span>Tax:</span>
                      <span>${extractionResult.tax.toFixed(2)}</span>
                    </div>
                  )}
                  <div className="flex justify-between font-semibold">
                    <span>Total:</span>
                    <span>${extractionResult?.total_amount?.toFixed(2) || '0.00'}</span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* Notes */}
          <div className="card">
            <h3 className="font-semibold mb-4">Notes</h3>
            <textarea
              value={formData.notes}
              onChange={(e) => setFormData({ ...formData, notes: e.target.value })}
              className="input w-full"
              rows={3}
              placeholder="Additional notes..."
            />
          </div>

          {/* Actions */}
          <div className="flex justify-between items-center">
            <button
              onClick={() => {
                setStep('upload');
                setFile(null);
                setExtractionResult(null);
              }}
              className="btn-secondary"
            >
              Start Over
            </button>
            <button
              onClick={handleCreatePO}
              className="btn-primary px-8 flex items-center gap-2"
            >
              <DocumentCheckIcon className="h-5 w-5" />
              Create Purchase Order
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
