/**
 * Print / view a purchased shipping label or freight Bill of Lading.
 *
 * Mirrors ``PrintPackingSlip`` (standalone, no app Layout) but renders the
 * carrier-generated PDF that the backend stored as a ``Document``. The buy-label
 * / buy-bol flow knows the document id, so it is passed through the query string
 * (``?doc=<id>&type=label|bol``); the shipment id in the path is used only for
 * the header / fallback lookup. The PDF blob is fetched through the Axios client
 * (``downloadDocument``) so the auth + refresh interceptor applies, then embedded
 * in an iframe. Auto-prints once the PDF is ready.
 *
 * Route: ``/print/shipping-label/:id`` (gated on ``shipping:view``).
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';
import api from '../services/api';
import { formatCentralDate, getCentralTodayISODate } from '../utils/centralTime';

interface ShipmentHeader {
  id: number;
  shipment_number: string;
  work_order_number?: string;
  customer_name?: string;
  ship_to_name?: string;
  ship_to_address?: string;
  carrier?: string;
  tracking_number?: string;
  ship_date?: string;
}

export default function PrintShippingLabel() {
  const { id } = useParams();
  const [searchParams] = useSearchParams();
  const docParam = searchParams.get('doc');
  const docType = (searchParams.get('type') || 'label').toLowerCase();
  const isBol = docType === 'bol';

  const [shipment, setShipment] = useState<ShipmentHeader | null>(null);
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const printedRef = useRef(false);

  const documentId = useMemo(() => {
    if (!docParam) return null;
    const parsed = parseInt(docParam, 10);
    return Number.isNaN(parsed) ? null : parsed;
  }, [docParam]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // Header (best-effort): a deleted/missing shipment shouldn't block the PDF.
      try {
        const detail = await api.getShipment(parseInt(id!, 10));
        setShipment(detail);
      } catch {
        setShipment(null);
      }

      if (documentId == null) {
        setError(
          isBol
            ? 'No Bill of Lading document is available for this shipment yet.'
            : 'No shipping label document is available for this shipment yet.',
        );
        return;
      }

      const blob = await api.downloadDocument(documentId);
      const url = window.URL.createObjectURL(new Blob([blob], { type: 'application/pdf' }));
      setPdfUrl(url);
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail || `Failed to load ${isBol ? 'Bill of Lading' : 'shipping label'} document.`);
    } finally {
      setLoading(false);
    }
  }, [id, documentId, isBol]);

  useEffect(() => {
    load();
  }, [load]);

  // Revoke the object URL when it changes / on unmount to avoid leaking blobs.
  useEffect(() => {
    return () => {
      if (pdfUrl) window.URL.revokeObjectURL(pdfUrl);
    };
  }, [pdfUrl]);

  // Auto-print once the PDF is embedded (mirror PrintPackingSlip). Print the
  // surrounding document; the user can also print the iframe directly.
  useEffect(() => {
    if (pdfUrl && !loading && !printedRef.current) {
      printedRef.current = true;
      setTimeout(() => window.print(), 700);
    }
  }, [pdfUrl, loading]);

  const docLabel = isBol ? 'Bill of Lading' : 'Shipping Label';
  const shipDate = shipment?.ship_date || getCentralTodayISODate();

  return (
    <div className="p-8 max-w-4xl mx-auto print:p-0">
      <style>{`
        @media print {
          body { -webkit-print-color-adjust: exact; }
          .no-print { display: none !important; }
          .print-frame { height: 95vh !important; border: none !important; }
        }
      `}</style>

      {/* Screen header (hidden on print so only the carrier PDF prints) */}
      <div className="no-print flex items-start justify-between border-b-2 border-black pb-4 mb-6">
        <div>
          <img src="/Werco_Logo-PNG.png" alt="Werco" className="h-12 mb-2" />
          <p className="text-sm">Werco Manufacturing</p>
        </div>
        <div className="text-right">
          <h1 className="text-2xl font-bold">{docLabel.toUpperCase()}</h1>
          {shipment && (
            <>
              <p className="text-lg font-mono">{shipment.shipment_number}</p>
              <p className="text-sm text-gray-600">
                {shipment.carrier || '—'}
                {shipment.tracking_number ? ` · ${shipment.tracking_number}` : ''}
              </p>
              <p className="text-sm text-gray-600">
                Date: {formatCentralDate(shipDate, { month: '2-digit', day: '2-digit', year: 'numeric' })}
              </p>
            </>
          )}
        </div>
      </div>

      {loading && <div className="text-center text-gray-500 py-16">Loading {docLabel} PDF…</div>}

      {!loading && error && (
        <div className="no-print border border-red-300 bg-red-50 text-red-800 rounded p-6 text-center">
          <p className="font-semibold mb-1">{docLabel} unavailable</p>
          <p className="text-sm">{error}</p>
        </div>
      )}

      {!loading && pdfUrl && (
        <iframe
          title={`${docLabel} for ${shipment?.shipment_number || `shipment ${id}`}`}
          src={pdfUrl}
          className="print-frame w-full h-[75vh] bg-white border border-gray-300"
        />
      )}

      <div className="no-print mt-8 text-center">
        <button onClick={() => window.print()} className="btn-primary" disabled={!pdfUrl}>
          Print
        </button>
        <button onClick={() => window.close()} className="btn-secondary ml-4">
          Close
        </button>
      </div>
    </div>
  );
}
