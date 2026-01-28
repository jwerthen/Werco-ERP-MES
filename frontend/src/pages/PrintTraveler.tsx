import React, { useEffect, useState, useCallback, useMemo } from 'react';
import { useParams, useLocation } from 'react-router-dom';
import api from '../services/api';
import { Part, WorkOrder } from '../types';
import { format } from 'date-fns';
import QRCode from 'qrcode';

interface MaterialRequirement {
  bom_item_id: number;
  item_number: number;
  part_id: number;
  part_number: string;
  part_name: string;
  part_type: string;
  quantity_per_assembly: number;
  quantity_required: number;
  scrap_factor: number;
  scrap_allowance: number;
  total_required: number;
  unit_of_measure: string;
  item_type: string;
  is_optional: boolean;
  notes: string | null;
}

interface MaterialRequirementsResponse {
  work_order_id: number;
  work_order_number: string;
  quantity_ordered: number;
  has_bom: boolean;
  bom_id?: number;
  bom_revision?: string;
  materials: MaterialRequirement[];
}

export default function PrintTraveler() {
  const { id } = useParams();
  const location = useLocation();
  const [workOrder, setWorkOrder] = useState<WorkOrder | null>(null);
  const [part, setPart] = useState<Part | null>(null);
  const [materialReqs, setMaterialReqs] = useState<MaterialRequirementsResponse | null>(null);
  const [qrDataUrl, setQrDataUrl] = useState<string>('');
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

      try {
        const [partRes, matReqsRes] = await Promise.all([
          api.getPart(response.part_id),
          api.getMaterialRequirements(response.id),
        ]);
        setPart(partRes);
        setMaterialReqs(matReqsRes);
      } catch (err) {
        // Material requirements or part data may not exist for all WOs
      }
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

  useEffect(() => {
    const generateQr = async () => {
      if (!workOrder) return;
      const baseUrl = window.location.origin;
      const workOrderUrl = `${baseUrl}/work-orders/${workOrder.id}`;
      try {
        const dataUrl = await QRCode.toDataURL(workOrderUrl, { width: 160, margin: 1 });
        setQrDataUrl(dataUrl);
      } catch (err) {
        console.error('Failed to generate QR code:', err);
      }
    };
    generateQr();
  }, [workOrder]);

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
  const formatHours = (value?: number | string | null) => {
    const parsed = typeof value === 'string' ? Number(value) : value;
    return Number.isFinite(parsed) ? Number(parsed).toFixed(2) : '0.00';
  };
  const printDate = format(new Date(), 'MM/dd/yyyy');
  const dueDate = workOrder.due_date ? format(new Date(workOrder.due_date), 'MM/dd/yyyy') : '-';
  const mustShipBy = workOrder.must_ship_by ? format(new Date(workOrder.must_ship_by), 'MM/dd/yyyy') : '-';

  return (
    <div className="p-8 max-w-5xl mx-auto print:p-4">
      <style>{`
        @media print {
          body { -webkit-print-color-adjust: exact; }
          .no-print { display: none; }
          .print-tight { margin-top: 0 !important; }
          .print-text-xs { font-size: 10px; }
        }
      `}</style>

      {/* Header */}
      <div className="flex justify-between items-start border-b-2 border-black pb-4 mb-4">
        <div>
          <h1 className="text-2xl font-bold">WORK ORDER TRAVELER</h1>
          <p className="text-lg font-mono">{workOrder.work_order_number}</p>
          <p className="text-sm text-gray-600">Issued: {printDate}</p>
        </div>
        <div className="text-right">
          <img src="/Werco_Logo-PNG.png" alt="Werco" className="h-12 mb-2" />
          <p className="text-sm">AS9100D / ISO 9001 Certified</p>
          {qrDataUrl && (
            <div className="mt-2 flex justify-end">
              <img src={qrDataUrl} alt="Work Order QR" className="h-20 w-20 print-qrcode" />
            </div>
          )}
        </div>
      </div>

      {/* Work Order Summary */}
      <div className="grid grid-cols-2 gap-4 mb-6">
        <div className="border p-3">
          <table className="w-full text-sm">
            <tbody>
              <tr>
                <td className="font-medium pr-4">Part Number:</td>
                <td className="font-mono">{part?.part_number || workOrder.part_id}</td>
              </tr>
              <tr>
                <td className="font-medium pr-4">Part Name:</td>
                <td>{part?.name || '-'}</td>
              </tr>
              <tr>
                <td className="font-medium pr-4">Revision:</td>
                <td>{part?.revision || '-'}</td>
              </tr>
              <tr>
                <td className="font-medium pr-4">Drawing #:</td>
                <td>{part?.drawing_number || '-'}</td>
              </tr>
              <tr>
                <td className="font-medium pr-4">Customer Part #:</td>
                <td>{part?.customer_part_number || '-'}</td>
              </tr>
              <tr>
                <td className="font-medium pr-4">Unit of Measure:</td>
                <td>{part?.unit_of_measure || '-'}</td>
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
                <td className="font-medium pr-4">Quantity Ordered:</td>
                <td className="font-bold">{workOrder.quantity_ordered}</td>
              </tr>
              <tr>
                <td className="font-medium pr-4">Quantity Complete:</td>
                <td>{workOrder.quantity_complete}</td>
              </tr>
              <tr>
                <td className="font-medium pr-4">Due Date:</td>
                <td className="font-bold">{dueDate}</td>
              </tr>
              <tr>
                <td className="font-medium pr-4">Must Ship By:</td>
                <td>{mustShipBy}</td>
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
      </div>

      {/* Special Instructions */}
      {workOrder.special_instructions && (
        <div className="border-2 border-red-500 p-3 mb-6 bg-red-50">
          <h3 className="font-bold text-red-700 mb-1">SPECIAL INSTRUCTIONS</h3>
          <p>{workOrder.special_instructions}</p>
        </div>
      )}

      {/* Routing / Operations */}
      <h3 className="font-bold text-lg mb-2">ROUTING & OPERATOR SIGN-OFF</h3>
      <table className="w-full border-collapse border text-xs mb-6 print-text-xs">
        <thead>
          <tr className="bg-gray-200">
            <th className="border p-2 text-left">Seq</th>
            <th className="border p-2 text-left">Operation / Instructions</th>
            <th className="border p-2 text-left">Work Center</th>
            <th className="border p-2 text-left">Component / Qty Req</th>
            <th className="border p-2 text-center">Setup</th>
            <th className="border p-2 text-center">Run</th>
            <th className="border p-2 text-center">Operator / Date / Qty</th>
          </tr>
        </thead>
        <tbody>
          {operations.map((op) => (
            <tr key={op.id}>
              <td className="border p-2 font-mono">{op.operation_number || op.sequence}</td>
              <td className="border p-2">
                <div className="font-medium">{op.name}</div>
                {op.description && <div className="text-gray-600">{op.description}</div>}
                {op.setup_instructions && <div className="text-gray-600">Setup: {op.setup_instructions}</div>}
                {op.run_instructions && <div className="text-gray-600">Run: {op.run_instructions}</div>}
              </td>
              <td className="border p-2">{op.work_center_name || op.work_center_id}</td>
              <td className="border p-2">
                {op.component_part_number ? (
                  <div>
                    <div className="font-medium">{op.component_part_number}</div>
                    <div className="text-gray-600">{op.component_part_name || '-'}</div>
                    <div className="text-gray-600">Qty: {op.component_quantity ?? '-'}</div>
                  </div>
                ) : (
                  <div>
                    <div className="text-gray-600">Qty: {workOrder.quantity_ordered}</div>
                  </div>
                )}
              </td>
              <td className="border p-2 text-center">{formatHours(op.setup_time_hours)}</td>
              <td className="border p-2 text-center">{formatHours(op.run_time_hours)}</td>
              <td className="border p-2 text-xs">
                <div>Operator: __________________</div>
                <div>Date: _____________________</div>
                <div>Qty Done: ________</div>
                <div>Qty Scrap: ______</div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {/* Material Requirements */}
      {materialReqs && materialReqs.has_bom && materialReqs.materials.length > 0 && (
        <div className="mb-6">
          <h3 className="font-bold text-lg mb-2">MATERIAL REQUIREMENTS / KITTING</h3>
          <table className="w-full border-collapse border text-xs print-text-xs">
            <thead>
              <tr className="bg-gray-200">
                <th className="border p-2 text-left">Item</th>
                <th className="border p-2 text-left">Part Number</th>
                <th className="border p-2 text-left">Description</th>
                <th className="border p-2 text-right">Qty/Asm</th>
                <th className="border p-2 text-right">Qty Required</th>
                <th className="border p-2 text-right">Scrap</th>
                <th className="border p-2 text-right">Total Needed</th>
                <th className="border p-2 text-left">UOM</th>
                <th className="border p-2 text-left">Optional</th>
              </tr>
            </thead>
            <tbody>
              {materialReqs.materials.map((mat) => (
                <tr key={mat.bom_item_id} className={mat.is_optional ? 'bg-yellow-50' : ''}>
                  <td className="border p-2">{mat.item_number}</td>
                  <td className="border p-2 font-medium">{mat.part_number}</td>
                  <td className="border p-2">{mat.part_name}</td>
                  <td className="border p-2 text-right">{mat.quantity_per_assembly}</td>
                  <td className="border p-2 text-right font-medium">{mat.quantity_required}</td>
                  <td className="border p-2 text-right">{mat.scrap_allowance > 0 ? `+${mat.scrap_allowance}` : '-'}</td>
                  <td className="border p-2 text-right font-bold">{mat.total_required}</td>
                  <td className="border p-2">{mat.unit_of_measure}</td>
                  <td className="border p-2">{mat.is_optional ? 'Yes' : 'No'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="text-xs text-gray-600 mt-2">Optional items are highlighted. Verify material heat/lot and certifications before release.</div>
        </div>
      )}

      {materialReqs && !materialReqs.has_bom && (
        <div className="border p-3 mb-6 text-sm text-gray-600">
          No BOM defined for this part. Verify required materials manually.
        </div>
      )}

      {/* Notes */}
      {workOrder.notes && (
        <div className="border p-3 mb-6">
          <h3 className="font-bold mb-1">Notes</h3>
          <p className="text-sm">{workOrder.notes}</p>
        </div>
      )}

      {/* Quality / Inspection */}
      <div className="border p-3 mb-6">
        <h3 className="font-bold mb-2">QUALITY CHECKPOINTS</h3>
        <div className="grid grid-cols-2 gap-4 text-sm">
          <div>
            <div>First Article Inspection: ____ Pass ____ Fail</div>
            <div>In-Process Inspection: ____ Pass ____ Fail</div>
            <div>Final Inspection: ____ Pass ____ Fail</div>
          </div>
          <div>
            <div>Inspector: ______________________</div>
            <div>Date: __________________________</div>
            <div>Notes: _________________________</div>
          </div>
        </div>
      </div>

      {/* Signoff */}
      <div className="grid grid-cols-3 gap-4 mt-8 print-tight">
        <div className="border-t-2 border-black pt-2 text-center">
          <p className="font-medium">QC Inspection Sign-Off</p>
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
