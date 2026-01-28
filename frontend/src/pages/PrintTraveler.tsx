import React, { useEffect, useState, useCallback, useMemo } from 'react';
import { useParams, useLocation } from 'react-router-dom';
import api from '../services/api';
import { WorkOrder } from '../types';
import { format } from 'date-fns';

export default function PrintTraveler() {
  const { id } = useParams();
  const location = useLocation();
  const [workOrder, setWorkOrder] = useState<WorkOrder | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const shouldAutoPrint = useMemo(() => {
    const params = new URLSearchParams(location.search);
    return params.get('autoprint') === '1' || params.get('print') === '1';
  }, [location.search]);

  const loadWorkOrder = useCallback(async () => {
    try {
      setError('');
      const response = await api.getWorkOrder(parseInt(id!));
      setWorkOrder(response);
    } catch (err) {
      console.error('Failed to load work order:', err);
      setError('Unable to load traveler. Please verify the work order ID and try again.');
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    loadWorkOrder();
  }, [loadWorkOrder]);

  useEffect(() => {
    if (workOrder && !loading && shouldAutoPrint) {
      setTimeout(() => window.print(), 500);
    }
  }, [workOrder, loading, shouldAutoPrint]);

  if (loading) {
    return <div className="p-8">Loading...</div>;
  }

  if (!workOrder || error) {
    return (
      <div className="p-8 max-w-3xl mx-auto">
        <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-red-700">
          {error || 'Work order not found.'}
        </div>
        <div className="mt-6">
          <button onClick={() => window.close()} className="btn-secondary">
            Close
          </button>
        </div>
      </div>
    );
  }

  const operations = workOrder.operations ?? [];

  return (
    <div className="p-8 max-w-4xl mx-auto print:p-4">
      <style>{`
        @media print {
          body { -webkit-print-color-adjust: exact; }
          .no-print { display: none; }
        }
      `}</style>

      {/* Header */}
      <div className="flex justify-between items-start border-b-2 border-black pb-4 mb-4">
        <div>
          <h1 className="text-2xl font-bold">WORK ORDER TRAVELER</h1>
          <p className="text-lg font-mono">{workOrder.work_order_number}</p>
        </div>
        <div className="text-right">
          <img src="/Werco_Logo-PNG.png" alt="Werco" className="h-12 mb-2" />
          <p className="text-sm">AS9100D / ISO 9001 Certified</p>
        </div>
      </div>

      {/* Work Order Info */}
      <div className="grid grid-cols-2 gap-4 mb-6">
        <div className="border p-3">
          <table className="w-full text-sm">
            <tbody>
              <tr>
                <td className="font-medium pr-4">Part Number:</td>
                <td className="font-mono">{workOrder.part_id}</td>
              </tr>
              <tr>
                <td className="font-medium pr-4">Quantity:</td>
                <td>{workOrder.quantity_ordered}</td>
              </tr>
              <tr>
                <td className="font-medium pr-4">Priority:</td>
                <td>{workOrder.priority}</td>
              </tr>
              <tr>
                <td className="font-medium pr-4">Lot Number:</td>
                <td>{workOrder.lot_number || '-'}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div className="border p-3">
          <table className="w-full text-sm">
            <tbody>
              <tr>
                <td className="font-medium pr-4">Customer:</td>
                <td>{workOrder.customer_name || '-'}</td>
              </tr>
              <tr>
                <td className="font-medium pr-4">Customer PO:</td>
                <td>{workOrder.customer_po || '-'}</td>
              </tr>
              <tr>
                <td className="font-medium pr-4">Due Date:</td>
                <td className="font-bold">{workOrder.due_date ? format(new Date(workOrder.due_date), 'MM/dd/yyyy') : '-'}</td>
              </tr>
              <tr>
                <td className="font-medium pr-4">Must Ship By:</td>
                <td>{workOrder.must_ship_by ? format(new Date(workOrder.must_ship_by), 'MM/dd/yyyy') : '-'}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      {/* Special Instructions */}
      {workOrder.special_instructions && (
        <div className="border-2 border-red-500 p-3 mb-6 bg-red-50">
          <h3 className="font-bold text-red-700 mb-1">SPECIAL INSTRUCTIONS</h3>
          <p>{workOrder.special_instructions}</p>
        </div>
      )}

      {/* Operations */}
      <h3 className="font-bold text-lg mb-2">OPERATIONS</h3>
      <table className="w-full border-collapse border text-sm mb-6">
        <thead>
          <tr className="bg-gray-200">
            <th className="border p-2 text-left">Op#</th>
            <th className="border p-2 text-left">Operation</th>
            <th className="border p-2 text-left">Work Center</th>
            <th className="border p-2 text-center">Setup</th>
            <th className="border p-2 text-center">Run</th>
            <th className="border p-2 text-center">Qty</th>
            <th className="border p-2 text-center">Operator</th>
            <th className="border p-2 text-center">Date</th>
          </tr>
        </thead>
        <tbody>
          {operations.map((op) => (
            <tr key={op.id}>
              <td className="border p-2 font-mono">{op.operation_number || op.sequence}</td>
              <td className="border p-2">{op.name}</td>
              <td className="border p-2">{op.work_center_name || op.work_center_id}</td>
              <td className="border p-2 text-center">{op.setup_time_hours.toFixed(2)}</td>
              <td className="border p-2 text-center">{op.run_time_hours.toFixed(2)}</td>
              <td className="border p-2 text-center h-8"></td>
              <td className="border p-2 text-center h-8"></td>
              <td className="border p-2 text-center h-8"></td>
            </tr>
          ))}
        </tbody>
      </table>

      {/* Notes */}
      {workOrder.notes && (
        <div className="border p-3 mb-6">
          <h3 className="font-bold mb-1">Notes</h3>
          <p className="text-sm">{workOrder.notes}</p>
        </div>
      )}

      {/* Signoff */}
      <div className="grid grid-cols-3 gap-4 mt-8">
        <div className="border-t-2 border-black pt-2 text-center">
          <p className="font-medium">QC Inspection</p>
        </div>
        <div className="border-t-2 border-black pt-2 text-center">
          <p className="font-medium">Final Approval</p>
        </div>
        <div className="border-t-2 border-black pt-2 text-center">
          <p className="font-medium">Date</p>
        </div>
      </div>

      {/* Print Button */}
      <div className="no-print mt-8 text-center">
        <button onClick={() => window.print()} className="btn-primary">
          Print Traveler
        </button>
        <button onClick={() => window.close()} className="btn-secondary ml-4">
          Close
        </button>
      </div>
    </div>
  );
}
