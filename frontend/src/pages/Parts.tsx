import React, { useEffect, useState, useMemo, useCallback } from 'react';
import api from '../services/api';
import { Part, PartType } from '../types';
import { useNavigate } from 'react-router-dom';
import { PlusIcon, PencilIcon, MagnifyingGlassIcon, ChevronDownIcon, ChevronRightIcon, TrashIcon, WrenchScrewdriverIcon, DocumentDuplicateIcon, XMarkIcon } from '@heroicons/react/24/outline';
import { SkeletonTable } from '../components/ui/Skeleton';

const typeColors: Record<PartType, string> = {
  manufactured: 'bg-blue-100 text-blue-800',
  purchased: 'bg-green-100 text-green-800',
  assembly: 'bg-purple-100 text-purple-800',
  raw_material: 'bg-yellow-100 text-yellow-800',
  hardware: 'bg-amber-100 text-amber-800',
  consumable: 'bg-orange-100 text-orange-800',
};

interface BOMItem {
  id: number;
  component_part_id: number;
  quantity: number;
  item_number: number;
  component_part?: Part;
}

interface ImportAssembly {
  part_number?: string;
  name?: string;
  revision?: string;
  description?: string;
  drawing_number?: string;
  part_type?: string;
}

interface ImportItem {
  line_number?: number;
  part_number?: string;
  description?: string;
  quantity?: number;
  unit_of_measure?: string;
  item_type?: string;
  line_type?: 'component' | 'hardware' | 'consumable' | 'reference';
  reference_designator?: string;
  find_number?: string;
  notes?: string;
}

interface ImportPreview {
  document_type: 'bom' | 'part';
  assembly: ImportAssembly;
  items: ImportItem[];
  extraction_confidence?: string;
  warnings?: string[];
  raw_columns?: string[];
  raw_rows?: string[][];
  suggested_mapping?: Record<string, number | null>;
  source_format?: string;
}

export default function Parts() {
  const navigate = useNavigate();
  const [parts, setParts] = useState<Part[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [typeFilter, setTypeFilter] = useState<string>('');
  const [showModal, setShowModal] = useState(false);
  const [editingPart, setEditingPart] = useState<Part | null>(null);
  const [formData, setFormData] = useState({
    part_number: '',
    name: '',
    part_type: 'manufactured' as PartType,
    description: '',
    revision: 'A',
    standard_cost: 0,
    is_critical: false,
    requires_inspection: true,
    customer_name: '',
    customer_part_number: '',
    drawing_number: '',
    version: 0
  });
  const [customerSearch, setCustomerSearch] = useState('');
  const [showCustomerDropdown, setShowCustomerDropdown] = useState(false);
  const [expandedParts, setExpandedParts] = useState<Set<number>>(new Set());
  const [bomData, setBomData] = useState<Record<number, BOMItem[]>>({});
  const [loadingBom, setLoadingBom] = useState<number | null>(null);
  const [showComponentsOnly, setShowComponentsOnly] = useState(false);
  const [showImportModal, setShowImportModal] = useState(false);
  const [showImportPreviewModal, setShowImportPreviewModal] = useState(false);
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importCreateMissingParts, setImportCreateMissingParts] = useState(true);
  const [importPreview, setImportPreview] = useState<ImportPreview | null>(null);
  const [importWarnings, setImportWarnings] = useState<string[]>([]);
  const [importLoading, setImportLoading] = useState(false);
  const [importColumnMap, setImportColumnMap] = useState<Record<string, number | null>>({});
  const [importDerivedItems, setImportDerivedItems] = useState<ImportItem[]>([]);

  // Get unique customers from existing parts
  const existingCustomers = useMemo(() => {
    const customers = parts
      .map(p => p.customer_name)
      .filter((c): c is string => !!c && c.trim() !== '');
    return Array.from(new Set(customers)).sort();
  }, [parts]);

  // Filter customers based on search
  const filteredCustomers = useMemo(() => {
    if (!customerSearch) return existingCustomers;
    const search = customerSearch.toLowerCase();
    return existingCustomers.filter(c => c.toLowerCase().includes(search));
  }, [existingCustomers, customerSearch]);

  const [allComponentIds, setAllComponentIds] = useState<Set<number>>(new Set());

  const loadParts = useCallback(async () => {
    try {
      const params: any = {};
      if (typeFilter) params.part_type = typeFilter;
      
      // Load parts first - this is the critical call
      const partsResponse = await api.getParts(params);
      setParts(partsResponse);
      
      // Try to load BOMs separately - don't fail if this fails
      try {
        const bomsResponse = await api.getBOMs({ active_only: true });
        
        // Build set of all component part IDs from all BOMs
        const componentIds = new Set<number>();
        const bomDataMap: Record<number, BOMItem[]> = {};
        
        for (const bom of bomsResponse) {
          if (bom.items && bom.items.length > 0) {
            bomDataMap[bom.part_id] = bom.items;
            bom.items.forEach((item: BOMItem) => {
              componentIds.add(item.component_part_id);
            });
          }
        }
        
        setAllComponentIds(componentIds);
        setBomData(bomDataMap);
      } catch (bomErr) {
        console.error('Failed to load BOMs (parts still loaded):', bomErr);
        // Clear component IDs so all parts show
        setAllComponentIds(new Set());
        setBomData({});
      }
    } catch (err) {
      console.error('Failed to load parts:', err);
    } finally {
      setLoading(false);
    }
  }, [typeFilter]);

  useEffect(() => {
    loadParts();
  }, [loadParts]);

  // Filter parts - only show manufactured/assembly parts (materials go to Materials & Hardware)
  const filteredParts = useMemo(() => {
    // First filter to only manufactured and assembly parts
    // Raw materials and purchased items belong in Materials & Hardware inventory
    let filtered = parts.filter(part => 
      part.part_type === 'manufactured' || part.part_type === 'assembly'
    );
    
    // Apply search filter
    if (search) {
      const searchLower = search.toLowerCase();
      filtered = filtered.filter(part =>
        part.part_number.toLowerCase().includes(searchLower) ||
        part.name.toLowerCase().includes(searchLower) ||
        part.description?.toLowerCase().includes(searchLower) ||
        part.customer_part_number?.toLowerCase().includes(searchLower)
      );
    }
    
    // Hide parts that are components of assemblies (unless searching or toggled)
    if (!search && !showComponentsOnly && allComponentIds.size > 0) {
      filtered = filtered.filter(part => !allComponentIds.has(part.id));
    }
    
    return filtered;
  }, [parts, search, showComponentsOnly, allComponentIds]);

  // Toggle assembly expansion
  const toggleExpand = async (partId: number) => {
    const newExpanded = new Set(expandedParts);
    if (newExpanded.has(partId)) {
      newExpanded.delete(partId);
    } else {
      newExpanded.add(partId);
      // Load BOM data if not already loaded
      if (!bomData[partId]) {
        setLoadingBom(partId);
        try {
          const bom = await api.getBOMByPart(partId);
          if (bom && bom.items) {
            setBomData(prev => ({ ...prev, [partId]: bom.items }));
          } else {
            setBomData(prev => ({ ...prev, [partId]: [] }));
          }
        } catch (err) {
          console.error('Failed to load BOM:', err);
          setBomData(prev => ({ ...prev, [partId]: [] }));
        } finally {
          setLoadingBom(null);
        }
      }
    }
    setExpandedParts(newExpanded);
  };

  // Get part by ID for BOM display
  const getPartById = (partId: number) => parts.find(p => p.id === partId);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      if (editingPart) {
        await api.updatePart(editingPart.id, formData);
      } else {
        await api.createPart(formData);
      }
      setShowModal(false);
      resetForm();
      loadParts();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to save part');
    }
  };

  const handleEdit = (part: Part) => {
    setEditingPart(part);
    setFormData({
      part_number: part.part_number,
      name: part.name,
      part_type: part.part_type,
      description: part.description || '',
      revision: part.revision,
      standard_cost: part.standard_cost,
      is_critical: part.is_critical,
      requires_inspection: part.requires_inspection,
      customer_name: part.customer_name || '',
      customer_part_number: part.customer_part_number || '',
      drawing_number: part.drawing_number || '',
      version: part.version || 0
    });
    setCustomerSearch(part.customer_name || '');
    setShowModal(true);
  };

  const handleDelete = async (part: Part) => {
    if (!window.confirm(`Delete part ${part.part_number}? This will mark it as obsolete.`)) return;
    try {
      await api.deletePart(part.id);
      loadParts();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to delete part');
    }
  };

  const handleCreateRouting = async (part: Part) => {
    try {
      // Check if routing already exists
      const existingRoutings = await api.getRoutings({ part_id: part.id });
      if (existingRoutings.length > 0) {
        navigate('/routing');
        return;
      }
      // Create new routing and navigate
      await api.createRouting({ part_id: part.id, revision: 'A', description: `Routing for ${part.part_number}` });
      navigate('/routing');
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to create routing');
    }
  };

  const resetForm = () => {
    setEditingPart(null);
    setFormData({
      part_number: '',
      name: '',
      part_type: 'manufactured',
      description: '',
      revision: 'A',
      standard_cost: 0,
      is_critical: false,
      requires_inspection: true,
      customer_name: '',
      customer_part_number: '',
      drawing_number: '',
      version: 0
    });
    setCustomerSearch('');
    setShowCustomerDropdown(false);
  };

  const handleImportPreview = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!importFile) {
      alert('Please select a file to import.');
      return;
    }
    setImportLoading(true);
    setImportWarnings([]);
    try {
      const formData = new FormData();
      formData.append('file', importFile);
      const preview = await api.previewBOMImport(formData);
      setImportPreview(preview);
      setImportWarnings(preview.warnings || []);
      if (preview.raw_columns && preview.raw_columns.length > 0 && preview.raw_rows) {
        const mapping = preview.suggested_mapping || {
          line_number: null,
          part_number: null,
          description: null,
          quantity: null,
          unit_of_measure: null,
          item_type: null,
          line_type: null,
        };
        setImportColumnMap(mapping);
        setImportDerivedItems(buildItemsFromRaw(preview.raw_columns, preview.raw_rows, mapping));
      } else {
        setImportDerivedItems(preview.items || []);
      }
      setShowImportModal(false);
      setShowImportPreviewModal(true);
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to generate preview');
    } finally {
      setImportLoading(false);
    }
  };

  const updatePreviewAssembly = (field: keyof ImportAssembly, value: string) => {
    if (!importPreview) return;
    setImportPreview({
      ...importPreview,
      assembly: {
        ...importPreview.assembly,
        [field]: value
      }
    });
  };

  const updatePreviewItem = (index: number, field: keyof ImportItem, value: string | number) => {
    if (!importPreview) return;
    const items = [...importPreview.items];
    items[index] = { ...items[index], [field]: value };
    setImportPreview({ ...importPreview, items });
  };

  const buildItemsFromRaw = (rawColumns: string[], rawRows: string[][], mapping: Record<string, number | null>) => {
    const items: ImportItem[] = [];
    let nextLine = 10;
    rawRows.forEach((row) => {
      const hasData = row.some((cell) => (cell || '').toString().trim() !== '');
      if (!hasData) return;

      const getVal = (field: string) => {
        const idx = mapping[field];
        if (idx === null || idx === undefined || idx >= row.length) return '';
        return (row[idx] || '').toString().trim();
      };

      const lineVal = getVal('line_number');
      const lineNumber = lineVal ? parseInt(lineVal) : nextLine;
      nextLine = (isNaN(lineNumber) ? nextLine : lineNumber) + 10;

      const quantityVal = getVal('quantity');
      const quantity = quantityVal ? parseFloat(quantityVal) : 1;

      items.push({
        line_number: isNaN(lineNumber) ? nextLine : lineNumber,
        part_number: getVal('part_number') || undefined,
        description: getVal('description') || undefined,
        quantity: isNaN(quantity) ? 1 : quantity,
        unit_of_measure: getVal('unit_of_measure') || undefined,
        item_type: getVal('item_type') || undefined,
        line_type: (getVal('line_type') as any) || undefined,
      });
    });
    return items;
  };

  const handleCommitImport = async () => {
    if (!importPreview) return;
    setImportLoading(true);
    try {
      const commitItems = (importPreview.raw_columns && importPreview.raw_columns.length > 0)
        ? importDerivedItems
        : importPreview.items;
      const result = await api.commitBOMImport({
        document_type: importPreview.document_type,
        assembly: importPreview.assembly,
        items: commitItems,
        create_missing_parts: importCreateMissingParts
      });
      await loadParts();
      if (result.bom_id) {
        navigate(`/bom?id=${result.bom_id}`);
      } else {
        alert(`Part created: ${result.assembly_part_number}`);
      }
      if (result.warnings?.length) {
        alert(`Import completed with warnings:\n- ${result.warnings.join('\n- ')}`);
      }
      setShowImportPreviewModal(false);
      setImportPreview(null);
      setImportFile(null);
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to create from preview');
    } finally {
      setImportLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-werco-primary"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6" data-tour="eng-parts">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-gray-900">Parts</h1>
        <div className="flex gap-2">
          <button
            onClick={() => setShowImportModal(true)}
            className="btn-secondary flex items-center"
          >
            <DocumentDuplicateIcon className="h-5 w-5 mr-2" />
            Import
          </button>
          <button
            onClick={() => { resetForm(); setShowModal(true); }}
            className="btn-primary flex items-center"
          >
            <PlusIcon className="h-5 w-5 mr-2" />
            Add Part
          </button>
        </div>
      </div>

      {/* Filters */}
      <div className="flex flex-col sm:flex-row gap-4">
        <div className="relative flex-1">
          <MagnifyingGlassIcon className="h-5 w-5 absolute left-3 top-1/2 transform -translate-y-1/2 text-gray-400" />
          <input
            type="text"
            placeholder="Search parts..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="input pl-10"
          />
        </div>
        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          className="input w-full sm:w-48"
        >
          <option value="">All Types</option>
          <option value="manufactured">Manufactured</option>
          <option value="assembly">Assembly</option>
          <option value="purchased">Purchased</option>
          <option value="hardware">Hardware</option>
          <option value="consumable">Consumable</option>
          <option value="raw_material">Raw Material</option>
        </select>
        <label className="flex items-center gap-2 text-sm text-gray-600 whitespace-nowrap">
          <input
            type="checkbox"
            checked={showComponentsOnly}
            onChange={(e) => setShowComponentsOnly(e.target.checked)}
            className="rounded border-gray-300 text-cyan-600 focus:ring-cyan-500"
          />
          Show BOM components
        </label>
      </div>
      
      {/* Filter status message */}
      {!search && !showComponentsOnly && allComponentIds.size > 0 && (
        <div className="text-sm text-amber-600 bg-amber-50 px-3 py-2 rounded-lg">
          {allComponentIds.size} component part{allComponentIds.size !== 1 ? 's' : ''} hidden (used in BOMs). 
          Check "Show BOM components" to see all parts.
        </div>
      )}

      {/* Parts Table */}
      <div className="card overflow-hidden">
        {loading ? (
          <SkeletonTable rows={8} columns={10} />
        ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase w-8"></th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Part #</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Name</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Customer</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Type</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Rev</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Cost</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Critical</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Actions</th>
              </tr>
            </thead>
            <tbody className="bg-white divide-y divide-gray-200">
              {filteredParts.map((part) => (
                <React.Fragment key={part.id}>
                  {/* Main part row */}
                  <tr className="hover:bg-gray-50">
                    <td className="px-4 py-4">
                      {part.part_type === 'assembly' && (
                        <button
                          onClick={() => toggleExpand(part.id)}
                          className="text-gray-400 hover:text-gray-600"
                          title={expandedParts.has(part.id) ? 'Collapse BOM' : 'Expand BOM'}
                        >
                          {loadingBom === part.id ? (
                            <div className="h-5 w-5 animate-spin rounded-full border-2 border-gray-300 border-t-werco-primary"></div>
                          ) : expandedParts.has(part.id) ? (
                            <ChevronDownIcon className="h-5 w-5" />
                          ) : (
                            <ChevronRightIcon className="h-5 w-5" />
                          )}
                        </button>
                      )}
                    </td>
                    <td className="px-4 py-4">
                      <span className="font-medium text-werco-primary">{part.part_number}</span>
                    </td>
                    <td className="px-4 py-4">
                      <div>
                        <div className="font-medium">{part.name}</div>
                        {part.customer_part_number && (
                          <div className="text-sm text-gray-500">Cust P/N: {part.customer_part_number}</div>
                        )}
                      </div>
                    </td>
                    <td className="px-4 py-4 text-sm">{part.customer_name || '-'}</td>
                    <td className="px-4 py-4">
                      <span className={`inline-flex px-2 py-1 rounded text-xs font-medium ${typeColors[part.part_type]}`}>
                        {part.part_type.replace('_', ' ')}
                      </span>
                    </td>
                    <td className="px-4 py-4 font-medium">{part.revision}</td>
                    <td className="px-4 py-4">${Number(part.standard_cost || 0).toFixed(2)}</td>
                    <td className="px-4 py-4">
                      {part.is_critical && (
                        <span className="inline-flex px-2 py-1 rounded bg-red-100 text-red-800 text-xs font-medium">
                          Critical
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-4">
                      <span className={`inline-flex px-2 py-1 rounded text-xs font-medium ${
                        part.status === 'active' ? 'bg-green-100 text-green-800' :
                        part.status === 'obsolete' ? 'bg-gray-100 text-gray-800' :
                        'bg-yellow-100 text-yellow-800'
                      }`}>
                        {part.status}
                      </span>
                    </td>
                    <td className="px-4 py-4">
                      <div className="flex gap-2">
                        <button
                          onClick={() => handleEdit(part)}
                          className="text-gray-400 hover:text-gray-600"
                          title="Edit"
                        >
                          <PencilIcon className="h-5 w-5" />
                        </button>
                        {(part.part_type === 'manufactured' || part.part_type === 'assembly') && (
                          <button
                            onClick={() => handleCreateRouting(part)}
                            className="text-gray-400 hover:text-blue-600"
                            title="Add/View Routing"
                          >
                            <WrenchScrewdriverIcon className="h-5 w-5" />
                          </button>
                        )}
                        <button
                          onClick={() => handleDelete(part)}
                          className="text-gray-400 hover:text-red-600"
                          title="Delete"
                        >
                          <TrashIcon className="h-5 w-5" />
                        </button>
                      </div>
                    </td>
                  </tr>
                  
                  {/* BOM items (expanded) */}
                  {expandedParts.has(part.id) && bomData[part.id] && bomData[part.id].length > 0 && (
                    bomData[part.id].map((item) => {
                      const componentPart = item.component_part || getPartById(item.component_part_id);
                      return (
                        <tr key={`bom-${part.id}-${item.id}`} className="bg-gray-50 hover:bg-gray-100">
                          <td className="px-4 py-2"></td>
                          <td className="px-4 py-2 pl-8">
                            <div className="flex items-center gap-2">
                              <span className="text-gray-400">└</span>
                              <span className="text-sm text-werco-primary">
                                {componentPart?.part_number || `Part #${item.component_part_id}`}
                              </span>
                              <span className="text-xs bg-gray-200 text-gray-600 px-1.5 py-0.5 rounded">
                                x{item.quantity}
                              </span>
                            </div>
                          </td>
                          <td className="px-4 py-2 text-sm text-gray-600">
                            {componentPart?.name || '-'}
                          </td>
                          <td className="px-4 py-2 text-sm text-gray-500">-</td>
                          <td className="px-4 py-2">
                            {componentPart && (
                              <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${typeColors[componentPart.part_type]}`}>
                                {componentPart.part_type.replace('_', ' ')}
                              </span>
                            )}
                          </td>
                          <td className="px-4 py-2 text-sm text-gray-600">{componentPart?.revision || '-'}</td>
                          <td className="px-4 py-2 text-sm text-gray-600">
                            ${Number(componentPart?.standard_cost || 0).toFixed(2)}
                          </td>
                          <td className="px-4 py-2"></td>
                          <td className="px-4 py-2"></td>
                          <td className="px-4 py-2">
                            {componentPart && (
                              <div className="flex gap-2">
                                <button
                                  onClick={() => handleEdit(componentPart)}
                                  className="text-gray-400 hover:text-gray-600"
                                  title="Edit Part"
                                >
                                  <PencilIcon className="h-4 w-4" />
                                </button>
                                {(componentPart.part_type === 'manufactured' || componentPart.part_type === 'assembly') && (
                                  <button
                                    onClick={() => handleCreateRouting(componentPart)}
                                    className="text-gray-400 hover:text-blue-600"
                                    title="Add/View Routing"
                                  >
                                    <WrenchScrewdriverIcon className="h-4 w-4" />
                                  </button>
                                )}
                              </div>
                            )}
                          </td>
                        </tr>
                      );
                    })
                  )}
                  
                  {/* No BOM items message */}
                  {expandedParts.has(part.id) && bomData[part.id] && bomData[part.id].length === 0 && (
                    <tr className="bg-gray-50">
                      <td className="px-4 py-2"></td>
                      <td colSpan={9} className="px-4 py-2 pl-8 text-sm text-gray-500 italic">
                        No BOM defined for this assembly
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              ))}
            </tbody>
          </table>
        </div>
        )}
        
        {!loading && filteredParts.length === 0 && (
          <div className="text-center py-8 text-gray-500">
            No parts found
          </div>
        )}
      </div>

      {/* Import Modal */}
      {showImportModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-lg w-full mx-4">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold">Import Part or BOM</h3>
              <button onClick={() => setShowImportModal(false)}>
                <XMarkIcon className="h-5 w-5 text-gray-500" />
              </button>
            </div>
            <form onSubmit={handleImportPreview} className="space-y-4">
              <div>
                <label className="label">PDF, Word, or Excel Document</label>
                <input
                  type="file"
                  accept=".pdf,.doc,.docx,.xlsx,.xls"
                  onChange={(e) => setImportFile(e.target.files?.[0] || null)}
                  className="input"
                  required
                />
              </div>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={importCreateMissingParts}
                  onChange={(e) => setImportCreateMissingParts(e.target.checked)}
                  className="rounded border-gray-300"
                />
                <span className="text-sm">Create missing parts automatically</span>
              </label>
              <div className="flex justify-end gap-3 pt-4 border-t">
                <button type="button" onClick={() => setShowImportModal(false)} className="btn-secondary">
                  Cancel
                </button>
                <button type="submit" className="btn-primary" disabled={importLoading}>
                  {importLoading ? 'Importing...' : 'Preview'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Import Preview Modal */}
      {showImportPreviewModal && importPreview && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-6xl w-full mx-4 max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between mb-4">
              <div>
                <h3 className="text-lg font-semibold">Review Import</h3>
                <p className="text-sm text-gray-500">
                  {importPreview.document_type === 'bom' ? 'Assembly BOM' : 'Single Part'} • Confidence: {importPreview.extraction_confidence || 'low'}
                </p>
              </div>
              <button onClick={() => setShowImportPreviewModal(false)}>
                <XMarkIcon className="h-5 w-5 text-gray-500" />
              </button>
            </div>

            {importWarnings.length > 0 && (
              <div className="bg-amber-50 border border-amber-200 rounded-md p-3 text-sm text-amber-800 mb-4">
                {importWarnings.map((w, idx) => (
                  <div key={idx}>{w}</div>
                ))}
              </div>
            )}

            <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-6">
              <div>
                <label className="label">Part Number</label>
                <input
                  className="input"
                  value={importPreview.assembly.part_number || ''}
                  onChange={(e) => updatePreviewAssembly('part_number', e.target.value)}
                />
              </div>
              <div>
                <label className="label">Revision</label>
                <input
                  className="input"
                  value={importPreview.assembly.revision || ''}
                  onChange={(e) => updatePreviewAssembly('revision', e.target.value)}
                />
              </div>
              <div>
                <label className="label">Part Type</label>
                <select
                  className="input"
                  value={importPreview.assembly.part_type || (importPreview.document_type === 'bom' ? 'assembly' : 'manufactured')}
                  onChange={(e) => updatePreviewAssembly('part_type', e.target.value)}
                >
                  <option value="manufactured">Manufactured</option>
                  <option value="assembly">Assembly</option>
                  <option value="purchased">Purchased</option>
                  <option value="raw_material">Raw Material</option>
                  <option value="hardware">Hardware</option>
                  <option value="consumable">Consumable</option>
                </select>
              </div>
              <div className="lg:col-span-2">
                <label className="label">Name</label>
                <input
                  className="input"
                  value={importPreview.assembly.name || ''}
                  onChange={(e) => updatePreviewAssembly('name', e.target.value)}
                />
              </div>
              <div>
                <label className="label">Drawing #</label>
                <input
                  className="input"
                  value={importPreview.assembly.drawing_number || ''}
                  onChange={(e) => updatePreviewAssembly('drawing_number', e.target.value)}
                />
              </div>
              <div className="lg:col-span-3">
                <label className="label">Description</label>
                <textarea
                  className="input"
                  rows={2}
                  value={importPreview.assembly.description || ''}
                  onChange={(e) => updatePreviewAssembly('description', e.target.value)}
                />
              </div>
            </div>

            {importPreview.document_type === 'bom' && (
              <div className="mb-4">
                {importPreview.raw_columns && importPreview.raw_columns.length > 0 && (
                  <div className="mb-4">
                    <p className="text-sm text-gray-600 mb-2">Map your Excel columns:</p>
                    <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
                      {[
                        { key: 'line_number', label: 'Line #' },
                        { key: 'part_number', label: 'Part #' },
                        { key: 'description', label: 'Description' },
                        { key: 'quantity', label: 'Qty' },
                        { key: 'unit_of_measure', label: 'UOM' },
                        { key: 'item_type', label: 'Item Type' },
                        { key: 'line_type', label: 'Line Type' },
                      ].map((field) => (
                        <div key={field.key}>
                          <label className="label">{field.label}</label>
                          <select
                            className="input"
                            value={importColumnMap[field.key] ?? ''}
                            onChange={(e) => {
                              const idx = e.target.value === '' ? null : parseInt(e.target.value);
                              const nextMap = { ...importColumnMap, [field.key]: idx };
                              setImportColumnMap(nextMap);
                              if (importPreview.raw_columns && importPreview.raw_rows) {
                                setImportDerivedItems(buildItemsFromRaw(importPreview.raw_columns, importPreview.raw_rows, nextMap));
                              }
                            }}
                          >
                            <option value="">Not mapped</option>
                            {(importPreview.raw_columns || []).map((col, idx) => (
                              <option key={col + idx} value={idx}>{col || `Column ${idx + 1}`}</option>
                            ))}
                          </select>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}

            {importPreview.document_type === 'bom' && (
              <div className="overflow-x-auto mb-6">
                <table className="min-w-full divide-y divide-gray-200">
                  <thead className="bg-gray-50">
                    <tr>
                      <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Line</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Part #</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Description</th>
                      <th className="px-3 py-2 text-right text-xs font-medium text-gray-500 uppercase">Qty</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">UOM</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Item Type</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Line Type</th>
                    </tr>
                  </thead>
                  <tbody className="bg-white divide-y divide-gray-200">
                    {(importPreview.raw_columns && importPreview.raw_columns.length > 0 ? importDerivedItems : importPreview.items).map((item, index) => (
                      <tr key={index}>
                        <td className="px-3 py-2 text-sm">
                          <input
                            className="input w-20"
                            type="number"
                            value={item.line_number || (index + 1) * 10}
                            onChange={(e) => {
                              const value = parseInt(e.target.value);
                              if (importPreview.raw_columns && importPreview.raw_columns.length > 0) {
                                const next = [...importDerivedItems];
                                next[index] = { ...next[index], line_number: value };
                                setImportDerivedItems(next);
                              } else {
                                updatePreviewItem(index, 'line_number', value);
                              }
                            }}
                          />
                        </td>
                        <td className="px-3 py-2 text-sm">
                          <textarea
                            className="input min-h-[44px] h-auto leading-snug"
                            rows={2}
                            value={item.part_number || ''}
                            onChange={(e) => {
                              const value = e.target.value;
                              if (importPreview.raw_columns && importPreview.raw_columns.length > 0) {
                                const next = [...importDerivedItems];
                                next[index] = { ...next[index], part_number: value };
                                setImportDerivedItems(next);
                              } else {
                                updatePreviewItem(index, 'part_number', value);
                              }
                            }}
                          />
                        </td>
                        <td className="px-3 py-2 text-sm">
                          <textarea
                            className="input min-h-[44px] h-auto leading-snug"
                            rows={2}
                            value={item.description || ''}
                            onChange={(e) => {
                              const value = e.target.value;
                              if (importPreview.raw_columns && importPreview.raw_columns.length > 0) {
                                const next = [...importDerivedItems];
                                next[index] = { ...next[index], description: value };
                                setImportDerivedItems(next);
                              } else {
                                updatePreviewItem(index, 'description', value);
                              }
                            }}
                          />
                        </td>
                        <td className="px-3 py-2 text-sm">
                          <input
                            className="input w-24 text-right"
                            type="number"
                            step="0.001"
                            value={item.quantity ?? 1}
                            onChange={(e) => {
                              const value = parseFloat(e.target.value);
                              if (importPreview.raw_columns && importPreview.raw_columns.length > 0) {
                                const next = [...importDerivedItems];
                                next[index] = { ...next[index], quantity: value };
                                setImportDerivedItems(next);
                              } else {
                                updatePreviewItem(index, 'quantity', value);
                              }
                            }}
                          />
                        </td>
                        <td className="px-3 py-2 text-sm">
                          <input
                            className="input w-24"
                            value={item.unit_of_measure || ''}
                            onChange={(e) => {
                              const value = e.target.value;
                              if (importPreview.raw_columns && importPreview.raw_columns.length > 0) {
                                const next = [...importDerivedItems];
                                next[index] = { ...next[index], unit_of_measure: value };
                                setImportDerivedItems(next);
                              } else {
                                updatePreviewItem(index, 'unit_of_measure', value);
                              }
                            }}
                          />
                        </td>
                        <td className="px-3 py-2 text-sm">
                          <select
                            className="input"
                            value={item.item_type || 'buy'}
                            onChange={(e) => {
                              const value = e.target.value;
                              if (importPreview.raw_columns && importPreview.raw_columns.length > 0) {
                                const next = [...importDerivedItems];
                                next[index] = { ...next[index], item_type: value };
                                setImportDerivedItems(next);
                              } else {
                                updatePreviewItem(index, 'item_type', value);
                              }
                            }}
                          >
                            <option value="make">Make</option>
                            <option value="buy">Buy</option>
                            <option value="phantom">Phantom</option>
                          </select>
                        </td>
                        <td className="px-3 py-2 text-sm">
                          <select
                            className="input"
                            value={item.line_type || 'component'}
                            onChange={(e) => {
                              const value = e.target.value;
                              if (importPreview.raw_columns && importPreview.raw_columns.length > 0) {
                                const next = [...importDerivedItems];
                                next[index] = { ...next[index], line_type: value as any };
                                setImportDerivedItems(next);
                              } else {
                                updatePreviewItem(index, 'line_type', value);
                              }
                            }}
                          >
                            <option value="component">Component</option>
                            <option value="hardware">Hardware</option>
                            <option value="consumable">Consumable</option>
                            <option value="reference">Reference</option>
                          </select>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {importPreview.items.length === 0 && (
                  <p className="text-sm text-gray-500 py-3">No BOM items detected.</p>
                )}
              </div>
            )}

            <div className="flex justify-end gap-3 pt-4 border-t">
              <button type="button" onClick={() => setShowImportPreviewModal(false)} className="btn-secondary">
                Cancel
              </button>
              <button type="button" onClick={handleCommitImport} className="btn-primary" disabled={importLoading}>
                {importLoading ? 'Creating...' : 'Create'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Add/Edit Modal */}
      {showModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-lg w-full mx-4 max-h-[90vh] overflow-y-auto">
            <h3 className="text-lg font-semibold mb-4">
              {editingPart ? 'Edit Part' : 'Add Part'}
            </h3>
            
            <form onSubmit={handleSubmit} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Part Number</label>
                  <input
                    type="text"
                    value={formData.part_number}
                    onChange={(e) => setFormData({ ...formData, part_number: e.target.value })}
                    className="input"
                    required
                    disabled={!!editingPart}
                  />
                </div>
                <div>
                  <label className="label">Revision</label>
                  <input
                    type="text"
                    value={formData.revision}
                    onChange={(e) => setFormData({ ...formData, revision: e.target.value })}
                    className="input"
                    required
                  />
                </div>
              </div>
              
              <div>
                <label className="label">Name</label>
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
                  <label className="label">Type</label>
                  <select
                    value={formData.part_type}
                    onChange={(e) => setFormData({ ...formData, part_type: e.target.value as PartType })}
                    className="input"
                    required
                  >
                    <option value="manufactured">Manufactured (Make)</option>
                    <option value="assembly">Assembly</option>
                    <option value="purchased">Purchased (Buy)</option>
                    <option value="hardware">Hardware (Fasteners, etc.)</option>
                    <option value="consumable">Consumable (Adhesives, etc.)</option>
                    <option value="raw_material">Raw Material</option>
                  </select>
                </div>
                <div>
                  <label className="label">Standard Cost ($)</label>
                  <input
                    type="number"
                    min="0"
                    step="0.01"
                    value={formData.standard_cost}
                    onChange={(e) => setFormData({ ...formData, standard_cost: parseFloat(e.target.value) || 0 })}
                    className="input"
                  />
                </div>
              </div>
              
              <div>
                <label className="label">Description</label>
                <textarea
                  value={formData.description}
                  onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                  className="input"
                  rows={2}
                />
              </div>
              
              <div className="relative">
                <label className="label">Customer</label>
                <div className="relative">
                  <input
                    type="text"
                    value={customerSearch}
                    onChange={(e) => {
                      setCustomerSearch(e.target.value);
                      setFormData({ ...formData, customer_name: e.target.value });
                      setShowCustomerDropdown(true);
                    }}
                    onFocus={() => setShowCustomerDropdown(true)}
                    onBlur={() => setTimeout(() => setShowCustomerDropdown(false), 200)}
                    className="input pr-8"
                    placeholder="Select or type new customer"
                  />
                  <ChevronDownIcon 
                    className="h-5 w-5 absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 cursor-pointer"
                    onClick={() => setShowCustomerDropdown(!showCustomerDropdown)}
                  />
                </div>
                {showCustomerDropdown && (
                  <div className="absolute z-10 w-full mt-1 bg-white border border-gray-200 rounded-md shadow-lg max-h-48 overflow-y-auto">
                    {filteredCustomers.length > 0 ? (
                      <>
                        {filteredCustomers.map(customer => (
                          <div
                            key={customer}
                            className="px-3 py-2 hover:bg-gray-100 cursor-pointer text-sm"
                            onMouseDown={() => {
                              setCustomerSearch(customer);
                              setFormData({ ...formData, customer_name: customer });
                              setShowCustomerDropdown(false);
                            }}
                          >
                            {customer}
                          </div>
                        ))}
                      </>
                    ) : customerSearch ? (
                      <div
                        className="px-3 py-2 hover:bg-blue-50 cursor-pointer text-sm text-blue-600"
                        onMouseDown={() => {
                          setFormData({ ...formData, customer_name: customerSearch });
                          setShowCustomerDropdown(false);
                        }}
                      >
                        <PlusIcon className="h-4 w-4 inline mr-1" />
                        Create "{customerSearch}"
                      </div>
                    ) : (
                      <div className="px-3 py-2 text-sm text-gray-500">
                        Type to search or add new customer
                      </div>
                    )}
                    {filteredCustomers.length > 0 && customerSearch && !filteredCustomers.includes(customerSearch) && (
                      <div
                        className="px-3 py-2 hover:bg-blue-50 cursor-pointer text-sm text-blue-600 border-t"
                        onMouseDown={() => {
                          setFormData({ ...formData, customer_name: customerSearch });
                          setShowCustomerDropdown(false);
                        }}
                      >
                        <PlusIcon className="h-4 w-4 inline mr-1" />
                        Create "{customerSearch}"
                      </div>
                    )}
                  </div>
                )}
              </div>
              
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Customer Part #</label>
                  <input
                    type="text"
                    value={formData.customer_part_number}
                    onChange={(e) => setFormData({ ...formData, customer_part_number: e.target.value })}
                    className="input"
                  />
                </div>
                <div>
                  <label className="label">Drawing #</label>
                  <input
                    type="text"
                    value={formData.drawing_number}
                    onChange={(e) => setFormData({ ...formData, drawing_number: e.target.value })}
                    className="input"
                  />
                </div>
              </div>
              
              <div className="flex gap-6">
                <label className="flex items-center">
                  <input
                    type="checkbox"
                    checked={formData.is_critical}
                    onChange={(e) => setFormData({ ...formData, is_critical: e.target.checked })}
                    className="mr-2"
                  />
                  <span className="text-sm">Critical Characteristic</span>
                </label>
                <label className="flex items-center">
                  <input
                    type="checkbox"
                    checked={formData.requires_inspection}
                    onChange={(e) => setFormData({ ...formData, requires_inspection: e.target.checked })}
                    className="mr-2"
                  />
                  <span className="text-sm">Requires Inspection</span>
                </label>
              </div>
              
              <div className="flex justify-end gap-3 mt-6">
                <button
                  type="button"
                  onClick={() => { setShowModal(false); resetForm(); }}
                  className="btn-secondary"
                >
                  Cancel
                </button>
                <button type="submit" className="btn-primary">
                  {editingPart ? 'Update' : 'Create'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
