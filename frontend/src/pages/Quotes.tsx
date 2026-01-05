import React, { useEffect, useState } from 'react';
import api from '../services/api';
import { format } from 'date-fns';
import {
  PlusIcon,
  PaperAirplaneIcon,
  ArrowRightIcon,
} from '@heroicons/react/24/outline';

interface QuoteLine {
  id: number;
  line_number: number;
  part_id?: number;
  part_number?: string;
  description: string;
  quantity: number;
  unit_price: number;
  line_total: number;
}

interface Quote {
  id: number;
  quote_number: string;
  revision: string;
  customer_name: string;
  customer_contact?: string;
  customer_email?: string;
  status: string;
  quote_date: string;
  valid_until?: string;
  subtotal: number;
  total: number;
  lead_time_days?: number;
  lines: QuoteLine[];
  work_order_id?: number;
}

interface Part {
  id: number;
  part_number: string;
  name: string;
  part_type: string;
  standard_cost: number;
}

const statusColors: Record<string, string> = {
  draft: 'bg-gray-100 text-gray-800',
  pending: 'bg-yellow-100 text-yellow-800',
  sent: 'bg-blue-100 text-blue-800',
  accepted: 'bg-green-100 text-green-800',
  rejected: 'bg-red-100 text-red-800',
  expired: 'bg-gray-100 text-gray-600',
  converted: 'bg-emerald-100 text-emerald-800',
};

export default function Quotes() {
  const [quotes, setQuotes] = useState<Quote[]>([]);
  const [parts, setParts] = useState<Part[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [selectedQuote, setSelectedQuote] = useState<Quote | null>(null);

  const [newQuote, setNewQuote] = useState({
    customer_name: '',
    customer_contact: '',
    customer_email: '',
    customer_phone: '',
    valid_days: 30,
    lead_time_days: 14,
    payment_terms: 'Net 30',
    notes: '',
    lines: [] as Array<{ part_id: number; description: string; quantity: number; unit_price: number; labor_hours: number }>
  });

  useEffect(() => {
    loadData();
  }, []);

  const loadData = async () => {
    try {
      const [quotesRes, partsRes] = await Promise.all([
        api.getQuotes(),
        api.getParts({ active_only: true })
      ]);
      setQuotes(quotesRes);
      setParts(partsRes);
    } catch (err) {
      console.error('Failed to load quotes:', err);
    } finally {
      setLoading(false);
    }
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (newQuote.lines.length === 0) {
      alert('Please add at least one line item');
      return;
    }
    try {
      await api.createQuote(newQuote);
      setShowCreateModal(false);
      setNewQuote({
        customer_name: '', customer_contact: '', customer_email: '', customer_phone: '',
        valid_days: 30, lead_time_days: 14, payment_terms: 'Net 30', notes: '', lines: []
      });
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to create quote');
    }
  };

  const handleSend = async (quoteId: number) => {
    try {
      await api.sendQuote(quoteId);
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to send quote');
    }
  };

  const handleConvert = async (quoteId: number) => {
    if (!window.confirm('Convert this quote to a work order?')) return;
    try {
      const result = await api.convertQuote(quoteId);
      alert(`Work Order ${result.work_order_number} created!`);
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to convert quote');
    }
  };

  const addLine = () => {
    setNewQuote({
      ...newQuote,
      lines: [...newQuote.lines, { part_id: 0, description: '', quantity: 1, unit_price: 0, labor_hours: 0 }]
    });
  };

  const updateLine = (index: number, field: string, value: any) => {
    const lines = [...newQuote.lines];
    lines[index] = { ...lines[index], [field]: value };
    
    // Auto-fill description and price from part
    if (field === 'part_id' && value > 0) {
      const part = parts.find(p => p.id === value);
      if (part) {
        lines[index].description = `${part.part_number} - ${part.name}`;
        lines[index].unit_price = part.standard_cost * 1.5; // 50% markup default
      }
    }
    
    setNewQuote({ ...newQuote, lines });
  };

  const removeLine = (index: number) => {
    setNewQuote({ ...newQuote, lines: newQuote.lines.filter((_, i) => i !== index) });
  };

  const calculateTotal = () => {
    return newQuote.lines.reduce((sum, line) => sum + (line.quantity * line.unit_price), 0);
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
        <h1 className="text-2xl font-bold text-gray-900">Quotes & Estimates</h1>
        <button onClick={() => setShowCreateModal(true)} className="btn-primary flex items-center">
          <PlusIcon className="h-5 w-5 mr-2" />
          New Quote
        </button>
      </div>

      {/* Quotes Table */}
      <div className="card">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Quote #</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Customer</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Date</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Valid Until</th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Total</th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Lines</th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200">
              {quotes.map((q) => (
                <tr key={q.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3">
                    <span className="font-medium text-werco-primary">{q.quote_number}</span>
                    <span className="text-gray-500 text-sm ml-1">Rev {q.revision}</span>
                  </td>
                  <td className="px-4 py-3">
                    <div className="font-medium">{q.customer_name}</div>
                    {q.customer_contact && (
                      <div className="text-sm text-gray-500">{q.customer_contact}</div>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-1 rounded-full text-xs font-medium ${statusColors[q.status]}`}>
                      {q.status}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-sm">
                    {format(new Date(q.quote_date), 'MMM d, yyyy')}
                  </td>
                  <td className="px-4 py-3 text-sm">
                    {q.valid_until ? format(new Date(q.valid_until), 'MMM d, yyyy') : '-'}
                  </td>
                  <td className="px-4 py-3 text-right font-medium">
                    ${q.total.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                  </td>
                  <td className="px-4 py-3 text-center">{q.lines.length}</td>
                  <td className="px-4 py-3 text-center">
                    <div className="flex justify-center gap-2">
                      {q.status === 'draft' && (
                        <button
                          onClick={() => handleSend(q.id)}
                          className="text-blue-600 hover:text-blue-800"
                          title="Send to Customer"
                        >
                          <PaperAirplaneIcon className="h-5 w-5" />
                        </button>
                      )}
                      {(q.status === 'sent' || q.status === 'accepted') && !q.work_order_id && (
                        <button
                          onClick={() => handleConvert(q.id)}
                          className="text-green-600 hover:text-green-800"
                          title="Convert to Work Order"
                        >
                          <ArrowRightIcon className="h-5 w-5" />
                        </button>
                      )}
                      {q.work_order_id && (
                        <span className="text-xs text-gray-500">WO Created</span>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {quotes.length === 0 && (
            <p className="text-center text-gray-500 py-8">No quotes yet</p>
          )}
        </div>
      </div>

      {/* Create Quote Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-3xl w-full mx-4 max-h-[90vh] overflow-y-auto">
            <h3 className="text-lg font-semibold mb-4">Create Quote</h3>
            <form onSubmit={handleCreate} className="space-y-4">
              {/* Customer Info */}
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Customer Name *</label>
                  <input
                    type="text"
                    value={newQuote.customer_name}
                    onChange={(e) => setNewQuote({ ...newQuote, customer_name: e.target.value })}
                    className="input"
                    required
                  />
                </div>
                <div>
                  <label className="label">Contact</label>
                  <input
                    type="text"
                    value={newQuote.customer_contact}
                    onChange={(e) => setNewQuote({ ...newQuote, customer_contact: e.target.value })}
                    className="input"
                  />
                </div>
              </div>
              <div className="grid grid-cols-3 gap-4">
                <div>
                  <label className="label">Email</label>
                  <input
                    type="email"
                    value={newQuote.customer_email}
                    onChange={(e) => setNewQuote({ ...newQuote, customer_email: e.target.value })}
                    className="input"
                  />
                </div>
                <div>
                  <label className="label">Valid Days</label>
                  <input
                    type="number"
                    value={newQuote.valid_days}
                    onChange={(e) => setNewQuote({ ...newQuote, valid_days: parseInt(e.target.value) })}
                    className="input"
                    min={1}
                  />
                </div>
                <div>
                  <label className="label">Lead Time (days)</label>
                  <input
                    type="number"
                    value={newQuote.lead_time_days}
                    onChange={(e) => setNewQuote({ ...newQuote, lead_time_days: parseInt(e.target.value) })}
                    className="input"
                  />
                </div>
              </div>

              {/* Line Items */}
              <div>
                <div className="flex justify-between items-center mb-2">
                  <label className="label">Line Items</label>
                  <button type="button" onClick={addLine} className="text-werco-primary text-sm hover:underline">
                    + Add Line
                  </button>
                </div>
                {newQuote.lines.length > 0 && (
                  <div className="flex gap-2 mb-1 text-xs text-gray-500 font-medium">
                    <div className="w-48">Part</div>
                    <div className="flex-1">Description</div>
                    <div className="w-20">Qty</div>
                    <div className="w-24">Unit Price</div>
                    <div className="w-24">Total</div>
                    <div className="w-6"></div>
                  </div>
                )}
                {newQuote.lines.map((line, idx) => (
                  <div key={idx} className="flex gap-2 mb-2 items-start">
                    <div className="w-48">
                      <select
                        value={line.part_id}
                        onChange={(e) => updateLine(idx, 'part_id', parseInt(e.target.value))}
                        className="input text-sm"
                      >
                        <option value={0}>Custom item...</option>
                        {parts.filter(p => ['manufactured', 'assembly'].includes(p.part_type as string)).map(p => (
                          <option key={p.id} value={p.id}>{p.part_number}</option>
                        ))}
                      </select>
                    </div>
                    <div className="flex-1">
                      <input
                        type="text"
                        value={line.description}
                        onChange={(e) => updateLine(idx, 'description', e.target.value)}
                        className="input text-sm"
                        placeholder="Description"
                        required
                      />
                    </div>
                    <div className="w-20">
                      <input
                        type="number"
                        value={line.quantity}
                        onChange={(e) => updateLine(idx, 'quantity', parseFloat(e.target.value))}
                        className="input text-sm"
                        min={1}
                      />
                    </div>
                    <div className="w-24">
                      <input
                        type="number"
                        value={line.unit_price}
                        onChange={(e) => updateLine(idx, 'unit_price', parseFloat(e.target.value))}
                        className="input text-sm"
                        step={0.01}
                        min={0}
                      />
                    </div>
                    <div className="w-24 text-right pt-2 font-medium">
                      ${(line.quantity * line.unit_price).toFixed(2)}
                    </div>
                    <button type="button" onClick={() => removeLine(idx)} className="text-red-500 hover:text-red-700 mt-2">
                      &times;
                    </button>
                  </div>
                ))}
                {newQuote.lines.length === 0 && (
                  <p className="text-gray-500 text-sm">Click "+ Add Line" to add items</p>
                )}
                {newQuote.lines.length > 0 && (
                  <div className="text-right mt-4 pt-4 border-t">
                    <span className="text-lg font-bold">
                      Total: ${calculateTotal().toLocaleString(undefined, { minimumFractionDigits: 2 })}
                    </span>
                  </div>
                )}
              </div>

              <div>
                <label className="label">Notes</label>
                <textarea
                  value={newQuote.notes}
                  onChange={(e) => setNewQuote({ ...newQuote, notes: e.target.value })}
                  className="input"
                  rows={2}
                />
              </div>

              <div className="flex justify-end gap-3 pt-4 border-t">
                <button type="button" onClick={() => setShowCreateModal(false)} className="btn-secondary">
                  Cancel
                </button>
                <button type="submit" className="btn-primary">Create Quote</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
