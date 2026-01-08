import React, { useEffect, useState, useCallback } from 'react';
import api from '../services/api';
import {
  PlusIcon,
  PencilIcon,
  TrashIcon,
  CheckIcon,
  XMarkIcon
} from '@heroicons/react/24/outline';

type EntityType = 'part' | 'work_order' | 'work_center' | 'customer' | 'supplier' | 'inventory' | 'bom';
type FieldType = 'text' | 'number' | 'decimal' | 'date' | 'datetime' | 'boolean' | 'select' | 'multiselect' | 'url' | 'email' | 'textarea';

interface FieldDefinition {
  id: number;
  field_key: string;
  display_name: string;
  description?: string;
  entity_type: EntityType;
  field_type: FieldType;
  is_required: boolean;
  is_active: boolean;
  sort_order: number;
  options?: string[];
  placeholder?: string;
  help_text?: string;
  show_in_list: boolean;
  show_in_filter: boolean;
  field_group?: string;
  created_at: string;
}

const entityTypes: { value: EntityType; label: string }[] = [
  { value: 'part', label: 'Parts' },
  { value: 'work_order', label: 'Work Orders' },
  { value: 'work_center', label: 'Work Centers' },
  { value: 'customer', label: 'Customers' },
  { value: 'supplier', label: 'Suppliers' },
  { value: 'inventory', label: 'Inventory' },
  { value: 'bom', label: 'Bill of Materials' },
];

const fieldTypes: { value: FieldType; label: string }[] = [
  { value: 'text', label: 'Text' },
  { value: 'textarea', label: 'Text Area' },
  { value: 'number', label: 'Number (Integer)' },
  { value: 'decimal', label: 'Decimal' },
  { value: 'date', label: 'Date' },
  { value: 'datetime', label: 'Date & Time' },
  { value: 'boolean', label: 'Yes/No' },
  { value: 'select', label: 'Dropdown (Single)' },
  { value: 'multiselect', label: 'Dropdown (Multiple)' },
  { value: 'url', label: 'URL' },
  { value: 'email', label: 'Email' },
];

export default function CustomFieldsPage() {
  const [fields, setFields] = useState<FieldDefinition[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedEntityType, setSelectedEntityType] = useState<EntityType | ''>('');
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [editingField, setEditingField] = useState<FieldDefinition | null>(null);

  // Form state
  const [formData, setFormData] = useState({
    field_key: '',
    display_name: '',
    description: '',
    entity_type: 'part' as EntityType,
    field_type: 'text' as FieldType,
    is_required: false,
    sort_order: 0,
    options: '',
    placeholder: '',
    help_text: '',
    show_in_list: false,
    show_in_filter: false,
    field_group: ''
  });

  const loadFields = useCallback(async () => {
    try {
      const data = await api.getCustomFieldDefinitions(selectedEntityType || undefined);
      setFields(data);
    } catch (err) {
      console.error('Failed to load fields:', err);
    } finally {
      setLoading(false);
    }
  }, [selectedEntityType]);

  useEffect(() => {
    loadFields();
  }, [loadFields]);

  const resetForm = () => {
    setFormData({
      field_key: '',
      display_name: '',
      description: '',
      entity_type: selectedEntityType || 'part',
      field_type: 'text',
      is_required: false,
      sort_order: 0,
      options: '',
      placeholder: '',
      help_text: '',
      show_in_list: false,
      show_in_filter: false,
      field_group: ''
    });
  };

  const openCreateModal = () => {
    resetForm();
    setEditingField(null);
    setShowCreateModal(true);
  };

  const openEditModal = (field: FieldDefinition) => {
    setEditingField(field);
    setFormData({
      field_key: field.field_key,
      display_name: field.display_name,
      description: field.description || '',
      entity_type: field.entity_type,
      field_type: field.field_type,
      is_required: field.is_required,
      sort_order: field.sort_order,
      options: field.options?.join('\n') || '',
      placeholder: field.placeholder || '',
      help_text: field.help_text || '',
      show_in_list: field.show_in_list,
      show_in_filter: field.show_in_filter,
      field_group: field.field_group || ''
    });
    setShowCreateModal(true);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    
    const payload = {
      ...formData,
      options: formData.options ? formData.options.split('\n').filter(o => o.trim()) : null
    };

    try {
      if (editingField) {
        await api.updateCustomFieldDefinition(editingField.id, payload);
      } else {
        await api.createCustomFieldDefinition(payload);
      }
      setShowCreateModal(false);
      loadFields();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to save field');
    }
  };

  const handleDelete = async (field: FieldDefinition) => {
    if (!window.confirm(`Deactivate field "${field.display_name}"?`)) return;
    
    try {
      await api.deleteCustomFieldDefinition(field.id);
      loadFields();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to delete field');
    }
  };

  const groupedFields = fields.reduce((acc, field) => {
    const group = field.field_group || 'General';
    if (!acc[group]) acc[group] = [];
    acc[group].push(field);
    return acc;
  }, {} as Record<string, FieldDefinition[]>);

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
        <h1 className="text-2xl font-bold text-gray-900">Custom Fields</h1>
        <button onClick={openCreateModal} className="btn-primary flex items-center">
          <PlusIcon className="h-5 w-5 mr-2" />
          New Field
        </button>
      </div>

      {/* Entity Type Filter */}
      <div className="card">
        <div className="flex flex-wrap gap-2">
          <button
            onClick={() => setSelectedEntityType('')}
            className={`px-3 py-1.5 rounded-full text-sm font-medium transition-colors ${
              selectedEntityType === ''
                ? 'bg-werco-primary text-white'
                : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
            }`}
          >
            All Entities
          </button>
          {entityTypes.map((et) => (
            <button
              key={et.value}
              onClick={() => setSelectedEntityType(et.value)}
              className={`px-3 py-1.5 rounded-full text-sm font-medium transition-colors ${
                selectedEntityType === et.value
                  ? 'bg-werco-primary text-white'
                  : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
              }`}
            >
              {et.label}
            </button>
          ))}
        </div>
      </div>

      {/* Fields List */}
      {Object.entries(groupedFields).length > 0 ? (
        Object.entries(groupedFields).map(([group, groupFields]) => (
          <div key={group} className="card">
            <h2 className="text-lg font-semibold mb-4">{group}</h2>
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-gray-200">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Field</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Entity</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Type</th>
                    <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Required</th>
                    <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">List</th>
                    <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Filter</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Actions</th>
                  </tr>
                </thead>
                <tbody className="bg-white divide-y divide-gray-200">
                  {groupFields.map((field) => (
                    <tr key={field.id} className={!field.is_active ? 'opacity-50' : ''}>
                      <td className="px-4 py-3">
                        <div className="font-medium">{field.display_name}</div>
                        <div className="text-sm text-gray-500">{field.field_key}</div>
                        {field.description && (
                          <div className="text-xs text-gray-400 mt-1">{field.description}</div>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <span className="inline-flex px-2 py-1 rounded bg-gray-100 text-gray-700 text-xs">
                          {entityTypes.find(e => e.value === field.entity_type)?.label}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-sm">
                        {fieldTypes.find(f => f.value === field.field_type)?.label}
                        {field.options && field.options.length > 0 && (
                          <div className="text-xs text-gray-400">{field.options.length} options</div>
                        )}
                      </td>
                      <td className="px-4 py-3 text-center">
                        {field.is_required && <CheckIcon className="h-5 w-5 text-green-500 mx-auto" />}
                      </td>
                      <td className="px-4 py-3 text-center">
                        {field.show_in_list && <CheckIcon className="h-5 w-5 text-green-500 mx-auto" />}
                      </td>
                      <td className="px-4 py-3 text-center">
                        {field.show_in_filter && <CheckIcon className="h-5 w-5 text-green-500 mx-auto" />}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <button
                          onClick={() => openEditModal(field)}
                          className="text-gray-400 hover:text-werco-primary mr-2"
                        >
                          <PencilIcon className="h-5 w-5" />
                        </button>
                        <button
                          onClick={() => handleDelete(field)}
                          className="text-gray-400 hover:text-red-500"
                        >
                          <TrashIcon className="h-5 w-5" />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ))
      ) : (
        <div className="card text-center py-12">
          <p className="text-gray-500 mb-4">No custom fields defined yet</p>
          <button onClick={openCreateModal} className="btn-primary">
            Create Your First Custom Field
          </button>
        </div>
      )}

      {/* Create/Edit Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-2xl max-h-[90vh] overflow-y-auto">
            <div className="p-6">
              <div className="flex justify-between items-center mb-6">
                <h2 className="text-xl font-semibold">
                  {editingField ? 'Edit Field' : 'New Custom Field'}
                </h2>
                <button onClick={() => setShowCreateModal(false)} className="text-gray-400 hover:text-gray-600">
                  <XMarkIcon className="h-6 w-6" />
                </button>
              </div>

              <form onSubmit={handleSubmit} className="space-y-4">
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="label">Field Key (unique identifier)</label>
                    <input
                      type="text"
                      value={formData.field_key}
                      onChange={(e) => setFormData({ ...formData, field_key: e.target.value.toLowerCase().replace(/\s+/g, '_') })}
                      className="input"
                      placeholder="customer_po_number"
                      required
                      disabled={!!editingField}
                    />
                  </div>
                  <div>
                    <label className="label">Display Name</label>
                    <input
                      type="text"
                      value={formData.display_name}
                      onChange={(e) => setFormData({ ...formData, display_name: e.target.value })}
                      className="input"
                      placeholder="Customer PO Number"
                      required
                    />
                  </div>
                </div>

                <div>
                  <label className="label">Description</label>
                  <input
                    type="text"
                    value={formData.description}
                    onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                    className="input"
                    placeholder="Optional description"
                  />
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="label">Entity Type</label>
                    <select
                      value={formData.entity_type}
                      onChange={(e) => setFormData({ ...formData, entity_type: e.target.value as EntityType })}
                      className="input"
                      disabled={!!editingField}
                    >
                      {entityTypes.map((et) => (
                        <option key={et.value} value={et.value}>{et.label}</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="label">Field Type</label>
                    <select
                      value={formData.field_type}
                      onChange={(e) => setFormData({ ...formData, field_type: e.target.value as FieldType })}
                      className="input"
                    >
                      {fieldTypes.map((ft) => (
                        <option key={ft.value} value={ft.value}>{ft.label}</option>
                      ))}
                    </select>
                  </div>
                </div>

                {(formData.field_type === 'select' || formData.field_type === 'multiselect') && (
                  <div>
                    <label className="label">Options (one per line)</label>
                    <textarea
                      value={formData.options}
                      onChange={(e) => setFormData({ ...formData, options: e.target.value })}
                      className="input"
                      rows={4}
                      placeholder="Option 1&#10;Option 2&#10;Option 3"
                    />
                  </div>
                )}

                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="label">Placeholder</label>
                    <input
                      type="text"
                      value={formData.placeholder}
                      onChange={(e) => setFormData({ ...formData, placeholder: e.target.value })}
                      className="input"
                      placeholder="Enter value..."
                    />
                  </div>
                  <div>
                    <label className="label">Field Group</label>
                    <input
                      type="text"
                      value={formData.field_group}
                      onChange={(e) => setFormData({ ...formData, field_group: e.target.value })}
                      className="input"
                      placeholder="General"
                    />
                  </div>
                </div>

                <div>
                  <label className="label">Help Text</label>
                  <input
                    type="text"
                    value={formData.help_text}
                    onChange={(e) => setFormData({ ...formData, help_text: e.target.value })}
                    className="input"
                    placeholder="Additional instructions for users"
                  />
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="label">Sort Order</label>
                    <input
                      type="number"
                      value={formData.sort_order}
                      onChange={(e) => setFormData({ ...formData, sort_order: parseInt(e.target.value) })}
                      className="input"
                    />
                  </div>
                </div>

                <div className="flex flex-wrap gap-6">
                  <label className="flex items-center">
                    <input
                      type="checkbox"
                      checked={formData.is_required}
                      onChange={(e) => setFormData({ ...formData, is_required: e.target.checked })}
                      className="mr-2"
                    />
                    <span className="text-sm">Required</span>
                  </label>
                  <label className="flex items-center">
                    <input
                      type="checkbox"
                      checked={formData.show_in_list}
                      onChange={(e) => setFormData({ ...formData, show_in_list: e.target.checked })}
                      className="mr-2"
                    />
                    <span className="text-sm">Show in List View</span>
                  </label>
                  <label className="flex items-center">
                    <input
                      type="checkbox"
                      checked={formData.show_in_filter}
                      onChange={(e) => setFormData({ ...formData, show_in_filter: e.target.checked })}
                      className="mr-2"
                    />
                    <span className="text-sm">Enable Filtering</span>
                  </label>
                </div>

                <div className="flex justify-end gap-3 pt-4">
                  <button type="button" onClick={() => setShowCreateModal(false)} className="btn-secondary">
                    Cancel
                  </button>
                  <button type="submit" className="btn-primary">
                    {editingField ? 'Update Field' : 'Create Field'}
                  </button>
                </div>
              </form>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
