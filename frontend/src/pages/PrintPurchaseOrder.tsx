import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useLocation, useParams } from 'react-router-dom';
import api from '../services/api';

interface POLinePrintData {
  line_number: number;
  part_number: string;
  part_name: string;
  quantity_ordered: string;
  quantity_received: string;
  unit_price: string;
  line_total: string;
  required_date?: string | null;
}

interface PurchaseOrderPrintData {
  po_number: string;
  status: string;
  vendor_name: string;
  vendor_code: string;
  vendor_contact?: string | null;
  vendor_email?: string | null;
  vendor_phone?: string | null;
  vendor_address?: string | null;
  buyer_name?: string | null;
  buyer_email?: string | null;
  order_date?: string | null;
  required_date?: string | null;
  expected_date?: string | null;
  ship_to?: string | null;
  shipping_method?: string | null;
  subtotal: string;
  tax: string;
  shipping: string;
  total: string;
  notes?: string | null;
  lines: POLinePrintData[];
  printed_at: string;
}

export default function PrintPurchaseOrder() {
  const { id } = useParams();
  const location = useLocation();
  const [po, setPo] = useState<PurchaseOrderPrintData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const shouldAutoPrint = useMemo(() => {
    const params = new URLSearchParams(location.search);
    return params.get('autoprint') === '1' || params.get('print') === '1';
  }, [location.search]);

  const loadPO = useCallback(async () => {
    try {
      setError('');
      const response = await api.getPurchaseOrderPrintData(parseInt(id || '0', 10));
      setPo(response);
    } catch (err) {
      console.error('Failed to load purchase order:', err);
      setError('Unable to load purchase order. Please verify the PO and try again.');
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    loadPO();
  }, [loadPO]);

  useEffect(() => {
    if (po && shouldAutoPrint) {
      const timer = setTimeout(() => window.print(), 400);
      return () => clearTimeout(timer);
    }
    return undefined;
  }, [po, shouldAutoPrint]);

  if (loading) {
    return <div className="p-8">Loading...</div>;
  }

  if (!po || error) {
    return (
      <div className="p-8 max-w-3xl mx-auto">
        <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-red-700">
          {error || 'Purchase order not found.'}
        </div>
        <div className="mt-6">
          <button onClick={() => window.close()} className="btn-secondary">
            Close
          </button>
        </div>
      </div>
    );
  }

  const groupedLines = po.lines.reduce<Record<string, POLinePrintData[]>>((acc, line) => {
    const key = line.required_date || 'AS AVAILABLE';
    if (!acc[key]) acc[key] = [];
    acc[key].push(line);
    return acc;
  }, {});

  const sortedGroups = Object.keys(groupedLines).sort((a, b) => {
    if (a === 'AS AVAILABLE') return 1;
    if (b === 'AS AVAILABLE') return -1;
    return new Date(a).getTime() - new Date(b).getTime();
  });

  const vendorAddress = po.vendor_address ? po.vendor_address.split('\n') : [];
  const shipToLines = po.ship_to ? po.ship_to.split('\n') : [];

  return (
    <div className="p-8 max-w-5xl mx-auto print:p-4">
      <style>{`
        @media print {
          body { -webkit-print-color-adjust: exact; }
          .no-print { display: none; }
        }
      `}</style>

      <div className="flex justify-between items-start border-b-2 border-black pb-4 mb-4">
        <div>
          <img src="/Werco_Logo-PNG.png" alt="Werco" className="h-12 mb-2" />
          <div className="text-xs text-gray-700">
            <div>WERCO MANUFACTURING</div>
            <div>415 East Houston Street</div>
            <div>Broken Arrow, OK 74012</div>
            <div>Phone 918.251.6880 • Fax 918.251.5397</div>
          </div>
        </div>
        <div className="text-right">
          <h1 className="text-2xl font-bold tracking-wide">PURCHASE ORDER</h1>
          <p className="text-lg font-mono">{po.po_number}</p>
          <p className="text-xs text-gray-600">Printed {po.printed_at}</p>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4 mb-6">
        <div className="border p-3">
          <h3 className="text-sm font-semibold mb-2">Supplier</h3>
          <div className="text-sm">
            <div className="font-medium">{po.vendor_name}</div>
            {vendorAddress.map((line, idx) => (
              <div key={idx}>{line}</div>
            ))}
            {po.vendor_contact && <div>Contact: {po.vendor_contact}</div>}
            {po.vendor_phone && <div>Phone: {po.vendor_phone}</div>}
            {po.vendor_email && <div>Email: {po.vendor_email}</div>}
          </div>
        </div>
        <div className="border p-3">
          <h3 className="text-sm font-semibold mb-2">PO Details</h3>
          <table className="w-full text-sm">
            <tbody>
              <tr>
                <td className="pr-3 text-gray-600">Order Date</td>
                <td>{po.order_date || '-'}</td>
              </tr>
              <tr>
                <td className="pr-3 text-gray-600">Required Date</td>
                <td>{po.required_date || '-'}</td>
              </tr>
              <tr>
                <td className="pr-3 text-gray-600">Expected Date</td>
                <td>{po.expected_date || '-'}</td>
              </tr>
              <tr>
                <td className="pr-3 text-gray-600">Buyer</td>
                <td>{po.buyer_name || 'Werco Purchasing'}</td>
              </tr>
              {po.buyer_email && (
                <tr>
                  <td className="pr-3 text-gray-600">Buyer Email</td>
                  <td>{po.buyer_email}</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4 mb-6">
        <div className="border p-3">
          <h3 className="text-sm font-semibold mb-2">Ship To</h3>
          <div className="text-sm">
            {shipToLines.length > 0 ? (
              shipToLines.map((line, idx) => <div key={idx}>{line}</div>)
            ) : (
              <div>Werco Manufacturing • Receiving</div>
            )}
          </div>
        </div>
        <div className="border p-3">
          <h3 className="text-sm font-semibold mb-2">Shipping Method</h3>
          <div className="text-sm">{po.shipping_method || '-'}</div>
        </div>
      </div>

      <div className="text-xs text-gray-600 mb-2">
        Please acknowledge orders with price and delivery. Include PO number on invoices, B/L, bundles, cases, and packing lists.
      </div>

      <table className="w-full border-collapse border text-sm mb-6">
        <thead>
          <tr className="bg-gray-200">
            <th className="border p-2 text-left">Qty</th>
            <th className="border p-2 text-left">Received</th>
            <th className="border p-2 text-left">Backorder</th>
            <th className="border p-2 text-left">Material Description</th>
            <th className="border p-2 text-right">Price Each</th>
            <th className="border p-2 text-right">Ext. Total</th>
          </tr>
        </thead>
        <tbody>
          {sortedGroups.map((group) => (
            <React.Fragment key={group}>
              <tr>
                <td colSpan={6} className="border p-2 text-center text-xs italic">
                  {group === 'AS AVAILABLE' ? 'TO BE DELIVERED AS AVAILABLE' : `TO BE DELIVERED ON ${group}`}
                </td>
              </tr>
              {groupedLines[group].map((line) => {
                const ordered = parseFloat(line.quantity_ordered || '0');
                const received = parseFloat(line.quantity_received || '0');
                const backorder = Math.max(ordered - received, 0);
                return (
                  <tr key={`${group}-${line.line_number}`}>
                    <td className="border p-2">{line.quantity_ordered}</td>
                    <td className="border p-2">{line.quantity_received}</td>
                    <td className="border p-2">{backorder.toFixed(0)}</td>
                    <td className="border p-2">
                      <div className="font-medium">{line.part_number}</div>
                      <div className="text-xs text-gray-600">{line.part_name}</div>
                    </td>
                    <td className="border p-2 text-right">{line.unit_price}</td>
                    <td className="border p-2 text-right">{line.line_total}</td>
                  </tr>
                );
              })}
            </React.Fragment>
          ))}
        </tbody>
      </table>

      <div className="flex justify-end">
        <table className="text-sm min-w-[260px]">
          <tbody>
            <tr>
              <td className="pr-4 text-gray-600">Subtotal</td>
              <td className="text-right font-medium">{po.subtotal}</td>
            </tr>
            <tr>
              <td className="pr-4 text-gray-600">Tax</td>
              <td className="text-right font-medium">{po.tax}</td>
            </tr>
            <tr>
              <td className="pr-4 text-gray-600">Shipping</td>
              <td className="text-right font-medium">{po.shipping}</td>
            </tr>
            <tr>
              <td className="pr-4 text-gray-900 font-semibold">Total</td>
              <td className="text-right font-semibold">{po.total}</td>
            </tr>
          </tbody>
        </table>
      </div>

      {po.notes && (
        <div className="border p-3 mt-6 text-sm">
          <h3 className="font-semibold mb-1">Notes</h3>
          <p>{po.notes}</p>
        </div>
      )}

      <div className="no-print mt-8 text-center">
        <button onClick={() => window.print()} className="btn-primary">
          Print Purchase Order
        </button>
        <button onClick={() => window.close()} className="btn-secondary ml-4">
          Close
        </button>
      </div>
    </div>
  );
}
