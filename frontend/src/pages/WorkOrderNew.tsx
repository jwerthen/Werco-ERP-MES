import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../services/api';

interface Part {
  id: number;
  part_number: string;
  name: string;
  part_type: string;
}

export default function WorkOrderNew() {
  const navigate = useNavigate();
  const [parts, setParts] = useState<Part[]>([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);

  const [form, setForm] = useState({
    part_id: 0,
    quantity_ordered: 1,
    priority: 5,
    customer_name: '',
    customer_po: '',
    due_date: '',
    lot_number: '',
    notes: ''
  });

  useEffect(() => {
    loadParts();
  }, []);

  const loadParts = async () => {
    try {
      const partsRes = await api.getParts({ active_only: true });
      setParts(partsRes);
    } catch (err) {
      console.error('Failed to load parts:', err);
    } finally {
      setLoading(false);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.part_id) {
      alert('Please select a part');
      return;
    }

    setSubmitting(true);
    try {
      const payload = {
        ...form,
        due_date: form.due_date || null,
        operations: []  // Let backend auto-generate from routing
      };
      const result = await api.createWorkOrder(payload);
      navigate(`/work-orders/${result.id}`);
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to create work order');
    } finally {
      setSubmitting(false);
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
    <div className="max-w-2xl mx-auto">
      <h1 className="text-2xl font-bold text-gray-900 mb-6">New Work Order</h1>

      <form onSubmit={handleSubmit} className="card space-y-6">
        <div>
          <label className="label">Part *</label>
          <select
            value={form.part_id}
            onChange={(e) => setForm({ ...form, part_id: parseInt(e.target.value) })}
            className="input"
            required
          >
            <option value={0}>Select a part...</option>
            {parts
              .filter(p => ['assembly', 'manufactured'].includes(p.part_type))
              .map(part => (
                <option key={part.id} value={part.id}>
                  {part.part_number} - {part.name}
                </option>
              ))}
          </select>
          <p className="text-sm text-gray-500 mt-1">
            If this part has a released routing, operations will be auto-generated.
          </p>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="label">Quantity *</label>
            <input
              type="number"
              value={form.quantity_ordered}
              onChange={(e) => setForm({ ...form, quantity_ordered: parseInt(e.target.value) })}
              className="input"
              min={1}
              required
            />
          </div>
          <div>
            <label className="label">Priority (1=Highest)</label>
            <select
              value={form.priority}
              onChange={(e) => setForm({ ...form, priority: parseInt(e.target.value) })}
              className="input"
            >
              <option value={1}>1 - Critical</option>
              <option value={2}>2 - Urgent</option>
              <option value={3}>3 - High</option>
              <option value={5}>5 - Normal</option>
              <option value={7}>7 - Low</option>
              <option value={10}>10 - Lowest</option>
            </select>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="label">Customer Name</label>
            <input
              type="text"
              value={form.customer_name}
              onChange={(e) => setForm({ ...form, customer_name: e.target.value })}
              className="input"
            />
          </div>
          <div>
            <label className="label">Customer PO #</label>
            <input
              type="text"
              value={form.customer_po}
              onChange={(e) => setForm({ ...form, customer_po: e.target.value })}
              className="input"
            />
          </div>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="label">Due Date</label>
            <input
              type="date"
              value={form.due_date}
              onChange={(e) => setForm({ ...form, due_date: e.target.value })}
              className="input"
            />
          </div>
          <div>
            <label className="label">Lot Number</label>
            <input
              type="text"
              value={form.lot_number}
              onChange={(e) => setForm({ ...form, lot_number: e.target.value })}
              className="input"
              placeholder="Auto-generated if blank"
            />
          </div>
        </div>

        <div>
          <label className="label">Notes</label>
          <textarea
            value={form.notes}
            onChange={(e) => setForm({ ...form, notes: e.target.value })}
            className="input"
            rows={3}
          />
        </div>

        <div className="flex justify-end gap-3 pt-4 border-t">
          <button
            type="button"
            onClick={() => navigate('/work-orders')}
            className="btn-secondary"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={submitting}
            className="btn-primary"
          >
            {submitting ? 'Creating...' : 'Create Work Order'}
          </button>
        </div>
      </form>
    </div>
  );
}
