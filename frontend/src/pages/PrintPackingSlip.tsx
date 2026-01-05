import React, { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import api from '../services/api';
import { format } from 'date-fns';

interface ShipmentDetail {
  id: number;
  shipment_number: string;
  work_order_id: number;
  work_order_number?: string;
  customer_name?: string;
  part_number?: string;
  part_name?: string;
  status: string;
  ship_to_name?: string;
  ship_to_address?: string;
  carrier?: string;
  tracking_number?: string;
  quantity_shipped: number;
  weight_lbs?: number;
  num_packages?: number;
  ship_date?: string;
  cert_of_conformance?: boolean;
  packing_notes?: string;
  lot_number?: string;
  customer_po?: string;
}

export default function PrintPackingSlip() {
  const { id } = useParams();
  const [shipment, setShipment] = useState<ShipmentDetail | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    loadShipment();
  }, [id]);

  const loadShipment = async () => {
    try {
      const response = await api.getShipment(parseInt(id!));
      setShipment(response);
    } catch (err) {
      console.error('Failed to load shipment:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (shipment && !loading) {
      setTimeout(() => window.print(), 500);
    }
  }, [shipment, loading]);

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
            Date: {shipment.ship_date ? format(new Date(shipment.ship_date), 'MM/dd/yyyy') : format(new Date(), 'MM/dd/yyyy')}
          </p>
        </div>
      </div>

      {/* Ship To / From */}
      <div className="grid grid-cols-2 gap-8 mb-6">
        <div className="border p-4">
          <h3 className="font-bold text-sm text-gray-500 mb-2">SHIP TO:</h3>
          <p className="font-medium text-lg">{shipment.ship_to_name || shipment.customer_name}</p>
          {shipment.ship_to_address && (
            <p className="whitespace-pre-line text-sm">{shipment.ship_to_address}</p>
          )}
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
              <th className="border p-2 text-left">Part Number</th>
              <th className="border p-2 text-left">Description</th>
              <th className="border p-2 text-center">Qty Shipped</th>
              <th className="border p-2 text-left">Lot #</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td className="border p-2 font-mono">{shipment.work_order_number}</td>
              <td className="border p-2">{shipment.customer_po || '-'}</td>
              <td className="border p-2 font-mono font-medium">{shipment.part_number}</td>
              <td className="border p-2">{shipment.part_name}</td>
              <td className="border p-2 text-center font-bold text-lg">{shipment.quantity_shipped}</td>
              <td className="border p-2 font-mono">{shipment.lot_number || '-'}</td>
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

      {/* COC Section - New Page */}
      {shipment.cert_of_conformance && (
        <>
          <div className="page-break"></div>
          <div className="mt-8 print:mt-0">
            {/* COC Header */}
            <div className="flex justify-between items-start border-b-2 border-black pb-4 mb-6">
              <div>
                <img src="/Werco_Logo-PNG.png" alt="Werco" className="h-16 mb-2" />
                <p className="text-sm">Werco Manufacturing</p>
                <p className="text-sm text-gray-600">AS9100D / ISO 9001 Certified</p>
              </div>
              <div className="text-right">
                <h1 className="text-2xl font-bold">CERTIFICATE OF CONFORMANCE</h1>
                <p className="text-lg font-mono">{shipment.shipment_number}</p>
              </div>
            </div>

            <div className="border-2 border-black p-6 mb-6">
              <p className="text-center mb-4">
                This is to certify that the items listed below were manufactured in accordance with
                customer specifications, applicable drawings, and Werco Manufacturing quality
                management system requirements.
              </p>
              
              <table className="w-full border-collapse text-sm mb-4">
                <thead>
                  <tr className="bg-gray-100">
                    <th className="border p-2 text-left">Part Number</th>
                    <th className="border p-2 text-left">Description</th>
                    <th className="border p-2 text-center">Quantity</th>
                    <th className="border p-2 text-left">Lot/Serial</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td className="border p-2 font-mono font-medium">{shipment.part_number}</td>
                    <td className="border p-2">{shipment.part_name}</td>
                    <td className="border p-2 text-center font-bold">{shipment.quantity_shipped}</td>
                    <td className="border p-2 font-mono">{shipment.lot_number || '-'}</td>
                  </tr>
                </tbody>
              </table>

              <div className="grid grid-cols-2 gap-4 text-sm">
                <div>
                  <p><strong>Customer:</strong> {shipment.customer_name}</p>
                  <p><strong>Customer PO:</strong> {shipment.customer_po || '-'}</p>
                </div>
                <div>
                  <p><strong>Work Order:</strong> {shipment.work_order_number}</p>
                  <p><strong>Ship Date:</strong> {shipment.ship_date ? format(new Date(shipment.ship_date), 'MM/dd/yyyy') : format(new Date(), 'MM/dd/yyyy')}</p>
                </div>
              </div>
            </div>

            <p className="text-sm mb-8">
              All items conform to purchase order requirements and applicable specifications.
              Quality records are maintained in accordance with our quality management system
              and are available upon request.
            </p>

            <div className="grid grid-cols-2 gap-8">
              <div>
                <div className="border-t-2 border-black pt-2">
                  <p className="font-medium">Quality Representative</p>
                </div>
              </div>
              <div>
                <div className="border-t-2 border-black pt-2">
                  <p className="font-medium">Date</p>
                </div>
              </div>
            </div>
          </div>
        </>
      )}

      {/* Print Button */}
      <div className="no-print mt-8 text-center">
        <button onClick={() => window.print()} className="btn-primary">
          Print
        </button>
        <button onClick={() => window.close()} className="btn-secondary ml-4">
          Close
        </button>
      </div>
    </div>
  );
}
