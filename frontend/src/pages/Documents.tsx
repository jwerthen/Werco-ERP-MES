import React, { useEffect, useState, useCallback, useMemo } from 'react';
import api from '../services/api';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import { Modal } from '../components/ui/Modal';
import { FormField } from '../components/ui/FormField';
import {
  useToast,
  DataTable,
  DataTableColumn,
  MobileDataCard,
} from '../components/ui';
import { formatCentralDate } from '../utils/centralTime';
import {
  ArrowUpTrayIcon,
  ArrowDownTrayIcon,
  TrashIcon,
  MagnifyingGlassIcon,
  DocumentTextIcon,
} from '@heroicons/react/24/outline';

interface Document {
  id: number;
  document_number: string;
  revision: string;
  title: string;
  document_type: string;
  description?: string;
  part_id?: number;
  work_order_id?: number;
  vendor_id?: number;
  file_name?: string;
  file_size?: number;
  mime_type?: string;
  status: string;
  created_at: string;
}

interface Part {
  id: number;
  part_number: string;
  name: string;
}

interface DocumentType {
  value: string;
  label: string;
}

const typeIcons: Record<string, string> = {
  drawing: '📐',
  specification: '📋',
  work_instruction: '📝',
  inspection_plan: '🔍',
  certificate: '📜',
  material_cert: '🏭',
  procedure: '📖',
  quality_record: '✅',
  ncr: '⚠️',
  car: '🔧',
  fai: '📊',
  other: '📄',
};

const formatFileSize = (bytes?: number) => {
  if (!bytes) return '-';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
};

export default function Documents() {
  const { showToast } = useToast();
  const [documents, setDocuments] = useState<Document[]>([]);
  const [parts, setParts] = useState<Part[]>([]);
  const [documentTypes, setDocumentTypes] = useState<DocumentType[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [search, setSearch] = useState('');
  const debouncedSearch = useDebouncedValue(search, 250);
  const [filterType, setFilterType] = useState('');
  const [showUploadModal, setShowUploadModal] = useState(false);

  const [uploadForm, setUploadForm] = useState({
    title: '',
    document_type: 'drawing',
    description: '',
    part_id: 0,
    revision: 'A',
    file: null as File | null
  });
  const partsById = useMemo(() => new Map(parts.map((part) => [part.id, part])), [parts]);

  const loadData = useCallback(async () => {
    setLoading(true);
    setLoadError(false);
    try {
      const [docsRes, partsRes, typesRes] = await Promise.all([
        api.getDocuments({ document_type: filterType || undefined }),
        api.getParts({ active_only: true, item_group: 'all' }),
        api.getDocumentTypes()
      ]);
      setDocuments(docsRes);
      setParts(partsRes);
      setDocumentTypes(typesRes);
    } catch (err) {
      console.error('Failed to load documents:', err);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, [filterType]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const handleUpload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!uploadForm.file) {
      showToast('error', 'Please select a file');
      return;
    }

    const formData = new FormData();
    formData.append('file', uploadForm.file);
    formData.append('title', uploadForm.title);
    formData.append('document_type', uploadForm.document_type);
    formData.append('description', uploadForm.description);
    formData.append('revision', uploadForm.revision);
    if (uploadForm.part_id > 0) {
      formData.append('part_id', uploadForm.part_id.toString());
    }

    try {
      await api.uploadDocument(formData);
      setShowUploadModal(false);
      setUploadForm({ title: '', document_type: 'drawing', description: '', part_id: 0, revision: 'A', file: null });
      showToast('success', 'Document uploaded');
      loadData();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to upload document');
    }
  };

  const handleDownload = useCallback(async (doc: Document) => {
    try {
      const response = await api.downloadDocument(doc.id);
      const url = window.URL.createObjectURL(new Blob([response]));
      const link = document.createElement('a');
      link.href = url;
      link.setAttribute('download', doc.file_name || 'document');
      document.body.appendChild(link);
      link.click();
      link.remove();
    } catch {
      showToast('error', 'Failed to download document');
    }
  }, [showToast]);

  const handleDelete = useCallback(async (docId: number) => {
    if (!window.confirm('Delete this document?')) return;
    try {
      await api.deleteDocument(docId);
      showToast('success', 'Document deleted');
      loadData();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to delete');
    }
  }, [showToast, loadData]);

  const filteredDocs = useMemo(() => {
    if (!debouncedSearch) return documents;
    const searchLower = debouncedSearch.toLowerCase();
    return documents.filter((doc) => (
      doc.document_number.toLowerCase().includes(searchLower) ||
      doc.title.toLowerCase().includes(searchLower) ||
      doc.file_name?.toLowerCase().includes(searchLower)
    ));
  }, [documents, debouncedSearch]);
  const filteredCount = filteredDocs.length;

  const partNumberFor = useCallback(
    (doc: Document) => (doc.part_id ? partsById.get(doc.part_id)?.part_number || '-' : '-'),
    [partsById]
  );

  const columns = useMemo<Array<DataTableColumn<Document>>>(() => [
    {
      key: 'document',
      header: 'Document',
      sortable: true,
      accessor: (doc) => doc.title,
      csv: (doc) => `${doc.title} (${doc.document_number} Rev ${doc.revision})`,
      render: (doc) => (
        <div className="flex items-center">
          <span className="text-2xl mr-3">{typeIcons[doc.document_type] || '📄'}</span>
          <div>
            <div className="font-medium">{doc.title}</div>
            <div className="text-sm text-slate-400">{doc.document_number} Rev {doc.revision}</div>
          </div>
        </div>
      ),
    },
    {
      key: 'type',
      header: 'Type',
      sortable: true,
      accessor: (doc) => doc.document_type,
      csv: (doc) => doc.document_type.replace(/_/g, ' '),
      render: (doc) => <span className="capitalize">{doc.document_type.replace('_', ' ')}</span>,
    },
    {
      key: 'file',
      header: 'File',
      sortable: true,
      accessor: (doc) => doc.file_name ?? '',
      csv: (doc) => doc.file_name ?? '',
      render: (doc) => (
        <>
          <div className="text-sm">{doc.file_name || '-'}</div>
          <div className="text-xs text-slate-400">{formatFileSize(doc.file_size)}</div>
        </>
      ),
    },
    {
      key: 'part',
      header: 'Part',
      sortable: true,
      accessor: (doc) => partNumberFor(doc),
      render: (doc) => <span className="text-sm">{partNumberFor(doc)}</span>,
    },
    {
      key: 'uploaded',
      header: 'Uploaded',
      sortable: true,
      accessor: (doc) => doc.created_at,
      csv: (doc) => formatCentralDate(doc.created_at),
      render: (doc) => <span className="text-sm">{formatCentralDate(doc.created_at)}</span>,
    },
    {
      key: 'actions',
      header: 'Actions',
      align: 'center',
      render: (doc) => (
        <div className="flex justify-center gap-2">
          <button
            onClick={(e) => { e.stopPropagation(); handleDownload(doc); }}
            className="text-werco-primary hover:text-blue-400"
            title="Download"
            aria-label="Download document"
          >
            <ArrowDownTrayIcon className="h-5 w-5" aria-hidden="true" />
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); handleDelete(doc.id); }}
            className="text-red-500 hover:text-red-400"
            title="Delete"
            aria-label="Delete document"
          >
            <TrashIcon className="h-5 w-5" aria-hidden="true" />
          </button>
        </div>
      ),
    },
  ], [partNumberFor, handleDownload, handleDelete]);

  const renderMobileCard = useCallback((doc: Document) => (
    <MobileDataCard
      title={doc.title}
      subtitle={`${doc.document_number} Rev ${doc.revision}`}
      badge={<span className="text-xl">{typeIcons[doc.document_type] || '📄'}</span>}
      fields={[
        { label: 'Type', value: <span className="capitalize">{doc.document_type.replace('_', ' ')}</span> },
        { label: 'Part', value: partNumberFor(doc) },
        { label: 'File', value: doc.file_name || '-', fullWidth: true },
        { label: 'Size', value: formatFileSize(doc.file_size) },
        { label: 'Uploaded', value: formatCentralDate(doc.created_at) },
      ]}
      actions={
        <>
          <button
            onClick={() => handleDownload(doc)}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-werco-primary border border-fd-line rounded-sm hover:bg-slate-700/40"
          >
            <ArrowDownTrayIcon className="h-4 w-4" /> Download
          </button>
          <button
            onClick={() => handleDelete(doc.id)}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-red-500 border border-fd-line rounded-sm hover:bg-slate-700/40"
          >
            <TrashIcon className="h-4 w-4" /> Delete
          </button>
        </>
      }
    />
  ), [partNumberFor, handleDownload, handleDelete]);

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-white">Documents</h1>
        <button onClick={() => setShowUploadModal(true)} className="btn-primary flex items-center">
          <ArrowUpTrayIcon className="h-5 w-5 mr-2" />
          Upload Document
        </button>
      </div>

      {/* Filters */}
      <div className="flex flex-col sm:flex-row gap-4">
        <div className="relative flex-1">
          <MagnifyingGlassIcon className="h-5 w-5 absolute left-3 top-1/2 transform -translate-y-1/2 text-slate-400" />
          <input
            type="text"
            placeholder="Search documents..."
            aria-label="Search documents"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="input pl-10"
          />
        </div>
        <select
          value={filterType}
          onChange={(e) => setFilterType(e.target.value)}
          className="input w-full sm:w-48"
        >
          <option value="">All Types</option>
          {documentTypes.map(t => (
            <option key={t.value} value={t.value}>{t.label}</option>
          ))}
        </select>
      </div>
      <div className="flex items-center gap-2 text-sm text-slate-400">
        <span>Showing</span>
        <span className="px-2 py-1 rounded-full bg-slate-800/50 text-slate-300 font-medium">{filteredCount}</span>
        <span>of</span>
        <span className="px-2 py-1 rounded-full bg-slate-800/50 text-slate-300 font-medium">{documents.length}</span>
        <span>documents</span>
      </div>

      {/* Documents Table */}
      <DataTable
        columns={columns}
        data={filteredDocs}
        rowKey={(doc) => doc.id}
        defaultSort={{ key: 'uploaded', dir: 'desc' }}
        pageSize={25}
        loading={loading}
        error={loadError}
        onRetry={loadData}
        csvExport={{ filename: 'documents' }}
        mobileCards={renderMobileCard}
        empty={{
          icon: DocumentTextIcon,
          title: search || filterType ? 'No matching documents' : 'No documents',
          description:
            search || filterType
              ? 'Try adjusting your search or type filter.'
              : 'Upload a document to get started.',
          action:
            search || filterType
              ? undefined
              : { label: 'Upload Document', onClick: () => setShowUploadModal(true) },
        }}
      />

      {/* Upload Modal */}
      <Modal open={showUploadModal} onClose={() => setShowUploadModal(false)} size="md" closeOnBackdrop={false}>
            <h3 className="text-lg font-semibold mb-4">Upload Document</h3>
            <form onSubmit={handleUpload} className="space-y-4">
              <FormField label="File" required>
                {(field) => (
                  <input
                    {...field}
                    type="file"
                    onChange={(e) => setUploadForm({ ...uploadForm, file: e.target.files?.[0] || null })}
                    className="input"
                    required
                  />
                )}
              </FormField>
              <FormField label="Title" required>
                {(field) => (
                  <input
                    {...field}
                    type="text"
                    value={uploadForm.title}
                    onChange={(e) => setUploadForm({ ...uploadForm, title: e.target.value })}
                    className="input"
                    placeholder="Document title"
                    required
                  />
                )}
              </FormField>
              <div className="grid grid-cols-2 gap-4">
                <FormField label="Type">
                  {(field) => (
                    <select
                      {...field}
                      value={uploadForm.document_type}
                      onChange={(e) => setUploadForm({ ...uploadForm, document_type: e.target.value })}
                      className="input"
                    >
                      {documentTypes.map(t => (
                        <option key={t.value} value={t.value}>{t.label}</option>
                      ))}
                    </select>
                  )}
                </FormField>
                <FormField label="Revision">
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={uploadForm.revision}
                      onChange={(e) => setUploadForm({ ...uploadForm, revision: e.target.value })}
                      className="input"
                    />
                  )}
                </FormField>
              </div>
              <FormField label="Associated Part">
                {(field) => (
                  <select
                    {...field}
                    value={uploadForm.part_id}
                    onChange={(e) => setUploadForm({ ...uploadForm, part_id: parseInt(e.target.value) })}
                    className="input"
                  >
                    <option value={0}>None</option>
                    {parts.map(p => (
                      <option key={p.id} value={p.id}>{p.part_number} - {p.name}</option>
                    ))}
                  </select>
                )}
              </FormField>
              <FormField label="Description">
                {(field) => (
                  <textarea
                    {...field}
                    value={uploadForm.description}
                    onChange={(e) => setUploadForm({ ...uploadForm, description: e.target.value })}
                    className="input"
                    rows={2}
                  />
                )}
              </FormField>
              <div className="flex justify-end gap-3 pt-4 border-t">
                <button type="button" onClick={() => setShowUploadModal(false)} className="btn-secondary">
                  Cancel
                </button>
                <button type="submit" className="btn-primary">Upload</button>
              </div>
            </form>
      </Modal>
    </div>
  );
}
