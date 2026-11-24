import React, { useEffect, useState, useCallback } from 'react';
import { useParams } from 'react-router-dom';
import api from '../services/api';
import { formatCentralDate, getCentralTodayISODate } from '../utils/centralTime';
import { ErrorState } from '../components/ui';
import CocPdfPreviewModal from '../components/shipping/CocPdfPreviewModal';
import type { ShipmentDetail } from '../types/shipping';

const SLIP_DATE = { month: '2-digit', day: '2-digit', year: 'numeric' } as const;

/** "City, ST 12345" from whichever parts the shipment carries. */
function cityStateZip(shipment: ShipmentDetail): string {
  const cityState = [shipment.ship_to_city, shipment.ship_to_state].filter(Boolean).join(', ');
  return [cityState, shipment.ship_to_zip].filter(Boolean).join(' ');
}

/**
 * What the slip says about the certificate. The certificate itself is the issued,
 * numbered PDF the server renders (opened with "Print certificate"); this page never
 * draws a look-alike.
 */
function certificateLine(shipment: ShipmentDetail): string {
  if (shipment.coc_number) {
    return shipment.coc_issued_at
      ? `${shipment.coc_number} (issued ${formatCentralDate(shipment.coc_issued_at, SLIP_DATE)})`
      : shipment.coc_number;
  }
  if (shipment.coc_withheld) return 'Withheld, Quality to issue';
  if (shipment.status === 'pending' || shipment.status === 'packed') {
    // Open quality items on the job: the server withholds the automatic certificate at
    // ship, so the slip must not promise one.
    const open = shipment.release_exceptions?.length ?? 0;
    if (open > 0) return `Withheld until Quality accepts ${open} open quality item(s)`;
    // Required when the shipment asks for one or the customer requires it (server rule).
    return 'Issued at ship when required';
  }
  return 'Not issued';
}

export default function PrintPackingSlip() {
  const { id } = useParams();
  const [shipment, setShipment] = useState<ShipmentDetail | null>(null);
  const [showCertificate, setShowCertificate] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);

  const loadShipment = useCallback(async () => {
    setLoadError(false);
    setLoading(true);
    try {
      const response = await api.getShipment(parseInt(id!));
      setShipment(response);
    } catch (err) {
      console.error('Failed to load shipment:', err);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    loadShipment();
  }, [loadShipment]);

  useEffect(() => {
    if (shipment && !loading) {
      setTimeout(() => window.print(), 500);
    }
  }, [shipment, loading]);

  if (loadError && !loading) {
    return (
      <div className="p-8 max-w-4xl mx-auto">
        <ErrorState message="Could not load this shipment." onRetry={loadShipment} />
      </div>
    );
  }

  if (loading || !shipment) {
    return <div className="p-8">Loading...</div>;
  }

  return (
    <div className="p-8 max-w-4xl mx-auto print:p-4">
      <style>{`
        @media print {
          body { -webkit-print-color-adjust: exact; }
          .no-print { display: none; }
          .page-break { page-break-after: always; }
        }
      `}</style>

      {/* Header */}
      <div className="flex justify-between items-start border-b-2 border-black pb-4 mb-6">
        <div>
          <img src="/Werco_Logo-PNG.png" alt="Werco" className="h-16 mb-2" />
          <p className="text-sm">Werco Manufacturing</p>
          <p className="text-sm text-gray-600">AS9100D / ISO 9001 Certified</p>
        </div>
        <div className="text-right">
          <h1 className="text-2xl font-bold">PACKING SLIP</h1>
          <p className="text-lg font-mono">{shipment.shipment_number}</p>
          <p className="text-sm text-gray-600">
            Date: {formatCentralDate(shipment.ship_date || getCentralTodayISODate(), SLIP_DATE)}
          </p>
        </div>
      </div>

      {/* Ship To / From */}
      <div className="grid grid-cols-2 gap-8 mb-6">
        <div className="border p-4">
          <h3 className="font-bold text-sm text-gray-500 mb-2">SHIP TO:</h3>
          <p className="font-medium text-lg">{shipment.ship_to_name || shipment.customer_name}</p>
          {shipment.ship_to_address && <p className="whitespace-pre-line text-sm">{shipment.ship_to_address}</p>}
          {cityStateZip(shipment) && <p className="text-sm">{cityStateZip(shipment)}</p>}
        </div>
        <div className="border p-4">
          <h3 className="font-bold text-sm text-gray-500 mb-2">SHIPPING INFO:</h3>
          <table className="text-sm w-full">
            <tbody>
              <tr>
                <td className="font-medium pr-4">Carrier:</td>
                <td>{shipment.carrier || '-'}</td>
              </tr>
              <tr>
                <td className="font-medium pr-4">Tracking #:</td>
                <td className="font-mono">{shipment.tracking_number || '-'}</td>
              </tr>
              <tr>
                <td className="font-medium pr-4">Weight:</td>
                <td>{shipment.weight_lbs ? `${shipment.weight_lbs} lbs` : '-'}</td>
              </tr>
              <tr>
                <td className="font-medium pr-4">Packages:</td>
                <td>{shipment.num_packages || 1}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      {/* Order Info */}
      <div className="mb-6">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="bg-gray-200">
              <th className="border p-2 text-left">Work Order</th>
              <th className="border p-2 text-left">Customer PO</th>
              <th className="border p-2 text-left">PO Line</th>
              <th className="border p-2 text-left">Part Number</th>
              <th className="border p-2 text-left">Rev</th>
              <th className="border p-2 text-left">Description</th>
              <th className="border p-2 text-center">Qty Shipped</th>
              <th className="border p-2 text-left">Lot #</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td className="border p-2 font-mono">{shipment.work_order_number}</td>
              <td className="border p-2">{shipment.customer_po || '-'}</td>
              <td className="border p-2">{shipment.po_line_item || '-'}</td>
              <td className="border p-2 font-mono font-medium">{shipment.part_number}</td>
              <td className="border p-2 font-mono">{shipment.part_revision || '-'}</td>
              <td className="border p-2">{shipment.part_name}</td>
              <td className="border p-2 text-center font-bold text-lg">{shipment.quantity_shipped}</td>
              <td className="border p-2 font-mono">{shipment.lot_number || '-'}</td>
            </tr>
          </tbody>
        </table>
        <table className="w-full border-collapse text-sm mt-2">
          <tbody>
            {shipment.unit_number && (
              <tr>
                <th scope="row" className="border p-2 text-left w-48 bg-gray-100">
                  Unit #
                </th>
                <td className="border p-2 font-mono">{shipment.unit_number}</td>
              </tr>
            )}
            {shipment.serial_numbers.length > 0 && (
              <tr>
                <th scope="row" className="border p-2 text-left w-48 bg-gray-100">
                  Serial numbers
                </th>
                <td className="border p-2 font-mono">{shipment.serial_numbers.join(', ')}</td>
              </tr>
            )}
            <tr>
              <th scope="row" className="border p-2 text-left w-48 bg-gray-100">
                Certificate of Conformance
              </th>
              <td className="border p-2 font-mono">{certificateLine(shipment)}</td>
            </tr>
          </tbody>
        </table>
      </div>

      {/* Notes */}
      {shipment.packing_notes && (
        <div className="mb-6 border p-3">
          <h3 className="font-bold text-sm mb-1">PACKING NOTES:</h3>
          <p className="text-sm">{shipment.packing_notes}</p>
        </div>
      )}

      {/* Signatures */}
      <div className="grid grid-cols-2 gap-8 mt-12">
        <div>
          <div className="border-t-2 border-black pt-2">
            <p className="font-medium">Packed By</p>
          </div>
        </div>
        <div>
          <div className="border-t-2 border-black pt-2">
            <p className="font-medium">Date</p>
          </div>
        </div>
      </div>

      {/* Print Button */}
      <div className="no-print mt-8 text-center">
        <button type="button" onClick={() => window.print()} className="btn-primary">
          Print
        </button>
        {shipment.coc_number && (
          <button type="button" onClick={() => setShowCertificate(true)} className="btn-secondary ml-4">
            Print certificate
          </button>
        )}
        <button type="button" onClick={() => window.close()} className="btn-secondary ml-4">
          Close
        </button>
      </div>
      {showCertificate && shipment.coc_number && (
        <CocPdfPreviewModal
          shipmentId={shipment.id}
          cocNumber={shipment.coc_number}
          onClose={() => setShowCertificate(false)}
        />
      )}
    </div>
  );
}
