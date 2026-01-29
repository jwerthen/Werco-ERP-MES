import React, { useEffect, useState, useCallback, useMemo } from 'react';
import api from '../services/api';
import { format } from 'date-fns';
import {
  ArrowUpTrayIcon,
  ArrowDownTrayIcon,
  TrashIcon,
  MagnifyingGlassIcon,
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

export default function Documents() {
  const [documents, setDocuments] = useState<Document[]>([]);
  const [parts, setParts] = useState<Part[]>([]);
  const [documentTypes, setDocumentTypes] = useState<DocumentType[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
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
    try {
      const [docsRes, partsRes, typesRes] = await Promise.all([
        api.getDocuments({ document_type: filterType || undefined }),
        api.getParts({ active_only: true }),
        api.getDocumentTypes()
      ]);
      setDocuments(docsRes);
      setParts(partsRes);
      setDocumentTypes(typesRes);
    } catch (err) {
      console.error('Failed to load documents:', err);
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
      alert('Please select a file');
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
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to upload document');
    }
  };

  const handleDownload = async (doc: Document) => {
    try {
      const response = await api.downloadDocument(doc.id);
      const url = window.URL.createObjectURL(new Blob([response]));
      const link = document.createElement('a');
      link.href = url;
      link.setAttribute('download', doc.file_name || 'document');
      document.body.appendChild(link);
      link.click();
      link.remove();
    } catch (err) {
      alert('Failed to download document');
    }
  };

  const handleDelete = async (docId: number) => {
    if (!window.confirm('Delete this document?')) return;
    try {
      await api.deleteDocument(docId);
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to delete');
    }
  };

  const formatFileSize = (bytes?: number) => {
    if (!bytes) return '-';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const filteredDocs = useMemo(() => {
    if (!search) return documents;
    const searchLower = search.toLowerCase();
    return documents.filter((doc) => (
      doc.document_number.toLowerCase().includes(searchLower) ||
      doc.title.toLowerCase().includes(searchLower) ||
      doc.file_name?.toLowerCase().includes(searchLower)
    ));
  }, [documents, search]);
  const filteredCount = filteredDocs.length;

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
        <h1 className="text-2xl font-bold text-gray-900">Documents</h1>
        <button onClick={() => setShowUploadModal(true)} className="btn-primary flex items-center">
          <ArrowUpTrayIcon className="h-5 w-5 mr-2" />
          Upload Document
        </button>
      </div>

      {/* Filters */}
      <div className="flex flex-col sm:flex-row gap-4">
        <div className="relative flex-1">
          <MagnifyingGlassIcon className="h-5 w-5 absolute left-3 top-1/2 transform -translate-y-1/2 text-gray-400" />
          <input
            type="text"
            placeholder="Search documents..."
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
      <div className="flex items-center gap-2 text-sm text-gray-600">
        <span>Showing</span>
        <span className="px-2 py-1 rounded-full bg-gray-100 text-gray-700 font-medium">{filteredCount}</span>
        <span>of</span>
        <span className="px-2 py-1 rounded-full bg-gray-100 text-gray-700 font-medium">{documents.length}</span>
        <span>documents</span>
      </div>

      {/* Documents Table */}
      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Document</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Type</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">File</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Part</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Uploaded</th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Actions</th>
              </tr>
            </thead>
            <tbody className="bg-white divide-y divide-gray-200">
              {filteredDocs.map((doc) => (
                <tr key={doc.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3">
                    <div className="flex items-center">
                      <span className="text-2xl mr-3">{typeIcons[doc.document_type] || '📄'}</span>
                      <div>
                        <div className="font-medium">{doc.title}</div>
                        <div className="text-sm text-gray-500">{doc.document_number} Rev {doc.revision}</div>
                      </div>
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <span className="capitalize">{doc.document_type.replace('_', ' ')}</span>
                  </td>
                  <td className="px-4 py-3">
                    <div className="text-sm">{doc.file_name || '-'}</div>
                    <div className="text-xs text-gray-500">{formatFileSize(doc.file_size)}</div>
                  </td>
                  <td className="px-4 py-3 text-sm">
                    {doc.part_id ? partsById.get(doc.part_id)?.part_number || '-' : '-'}
                  </td>
                  <td className="px-4 py-3 text-sm">
                    {format(new Date(doc.created_at), 'MMM d, yyyy')}
                  </td>
                  <td className="px-4 py-3 text-center">
                    <div className="flex justify-center gap-2">
                      <button
                        onClick={() => handleDownload(doc)}
                        className="text-werco-primary hover:text-blue-700"
                        title="Download"
                      >
                        <ArrowDownTrayIcon className="h-5 w-5" />
                      </button>
                      <button
                        onClick={() => handleDelete(doc.id)}
                        className="text-red-500 hover:text-red-700"
                        title="Delete"
                      >
                        <TrashIcon className="h-5 w-5" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {filteredDocs.length === 0 && (
            <p className="text-center text-gray-500 py-8">No documents found</p>
          )}
        </div>
      </div>

      {/* Upload Modal */}
      {showUploadModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-md w-full mx-4">
            <h3 className="text-lg font-semibold mb-4">Upload Document</h3>
            <form onSubmit={handleUpload} className="space-y-4">
              <div>
                <label className="label">File *</label>
                <input
                  type="file"
                  onChange={(e) => setUploadForm({ ...uploadForm, file: e.target.files?.[0] || null })}
                  className="input"
                  required
                />
              </div>
              <div>
                <label className="label">Title *</label>
                <input
                  type="text"
                  value={uploadForm.title}
                  onChange={(e) => setUploadForm({ ...uploadForm, title: e.target.value })}
                  className="input"
                  placeholder="Document title"
                  required
                />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Type</label>
                  <select
                    value={uploadForm.document_type}
                    onChange={(e) => setUploadForm({ ...uploadForm, document_type: e.target.value })}
                    className="input"
                  >
                    {documentTypes.map(t => (
                      <option key={t.value} value={t.value}>{t.label}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="label">Revision</label>
                  <input
                    type="text"
                    value={uploadForm.revision}
                    onChange={(e) => setUploadForm({ ...uploadForm, revision: e.target.value })}
                    className="input"
                  />
                </div>
              </div>
              <div>
                <label className="label">Associated Part</label>
                <select
                  value={uploadForm.part_id}
                  onChange={(e) => setUploadForm({ ...uploadForm, part_id: parseInt(e.target.value) })}
                  className="input"
                >
                  <option value={0}>None</option>
                  {parts.map(p => (
                    <option key={p.id} value={p.id}>{p.part_number} - {p.name}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="label">Description</label>
                <textarea
                  value={uploadForm.description}
                  onChange={(e) => setUploadForm({ ...uploadForm, description: e.target.value })}
                  className="input"
                  rows={2}
                />
              </div>
              <div className="flex justify-end gap-3 pt-4 border-t">
                <button type="button" onClick={() => setShowUploadModal(false)} className="btn-secondary">
                  Cancel
                </button>
                <button type="submit" className="btn-primary">Upload</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
