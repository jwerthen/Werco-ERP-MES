import React, { useState } from 'react';
import api from '../services/api';
import { formatCentralDate, formatCentralDateTime } from '../utils/centralTime';
import {
  MagnifyingGlassIcon,
  DocumentMagnifyingGlassIcon,
  CubeIcon,
  TruckIcon,
  ClipboardDocumentListIcon,
  ExclamationTriangleIcon,
  CheckCircleIcon,
  ArrowRightIcon,
} from '@heroicons/react/24/outline';

interface LotHistoryItem {
  timestamp: string;
  event_type: string;
  description: string;
  quantity?: number;
  location?: string;
  reference?: string;
  user?: string;
  details?: Record<string, any>;
}

interface LotTrace {
  lot_number: string;
  part_id?: number;
  part_number?: string;
  part_name?: string;
  supplier_name?: string;
  po_number?: string;
  received_date?: string;
  cert_number?: string;
  heat_lot?: string;
  current_quantity: number;
  current_location?: string;
  status: string;
  work_orders_used: string[];
  shipments: string[];
  ncrs: string[];
  history: LotHistoryItem[];
}

interface SearchResult {
  type: string;
  number: string;
  part_number?: string;
  part_name?: string;
  quantity?: number;
  location?: string;
}

const eventTypeIcons: Record<string, React.ReactNode> = {
  received: <TruckIcon className="h-5 w-5 text-green-600" />,
  receive: <TruckIcon className="h-5 w-5 text-green-600" />,
  issue: <ArrowRightIcon className="h-5 w-5 text-blue-600" />,
  transfer: <ArrowRightIcon className="h-5 w-5 text-purple-600" />,
  adjust: <ClipboardDocumentListIcon className="h-5 w-5 text-yellow-600" />,
  scrap: <ExclamationTriangleIcon className="h-5 w-5 text-red-600" />,
  ncr: <ExclamationTriangleIcon className="h-5 w-5 text-orange-600" />,
  ship: <TruckIcon className="h-5 w-5 text-indigo-600" />,
};

export default function Traceability() {
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [lotTrace, setLotTrace] = useState<LotTrace | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleSearch = async () => {
    if (!searchQuery.trim()) return;
    
    setLoading(true);
    setError('');
    setSearchResults([]);
    setLotTrace(null);
    
    try {
      const results = await api.searchLots(searchQuery.trim());
      setSearchResults(results);
      
      // If exactly one result, auto-load it
      if (results.length === 1) {
        loadLotTrace(results[0].number);
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Search failed');
    } finally {
      setLoading(false);
    }
  };

  const loadLotTrace = async (lotNumber: string) => {
    setLoading(true);
    setError('');
    
    try {
      const trace = await api.traceLot(lotNumber);
      setLotTrace(trace);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to load traceability');
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      handleSearch();
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-white">Lot / Serial Traceability</h1>
      </div>

      <div className="card bg-blue-500/10 border-blue-500/30">
        <p className="text-sm text-blue-300">
          <strong>AS9100D Compliance:</strong> Track lot numbers, serial numbers, and certificates 
          through the complete production lifecycle - from receiving through shipment.
        </p>
      </div>

      {/* Search */}
      <div className="card" data-tour="qa-traceability">
        <h2 className="text-lg font-semibold mb-4">Search</h2>
        <div className="flex gap-4">
          <div className="relative flex-1">
            <MagnifyingGlassIcon className="h-5 w-5 absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onKeyDown={handleKeyDown}
              className="input pl-10"
              placeholder="Enter lot number, serial number, cert number, or heat lot..."
            />
          </div>
          <button
            onClick={handleSearch}
            disabled={loading}
            className="btn-primary flex items-center"
          >
            <DocumentMagnifyingGlassIcon className="h-5 w-5 mr-2" />
            {loading ? 'Searching...' : 'Trace'}
          </button>
        </div>
      </div>

      {/* Error */}
      {error && (
        <div className="bg-red-500/10 border border-red-500/30 text-red-400 px-4 py-3 rounded-lg">
          {error}
        </div>
      )}

      {/* Search Results */}
      {searchResults.length > 1 && !lotTrace && (
        <div className="card">
          <h3 className="font-semibold mb-4">Search Results ({searchResults.length})</h3>
          <div className="space-y-2">
            {searchResults.map((r, idx) => (
              <button
                key={idx}
                onClick={() => loadLotTrace(r.number)}
                className="w-full text-left p-3 border rounded-lg hover:bg-slate-800 flex items-center justify-between"
              >
                <div className="flex items-center">
                  <span className={`px-2 py-1 rounded text-xs font-medium mr-3 ${
                    r.type === 'lot' ? 'bg-blue-500/20 text-blue-300' : 'bg-purple-500/20 text-purple-800'
                  }`}>
                    {r.type.toUpperCase()}
                  </span>
                  <div>
                    <span className="font-mono font-medium">{r.number}</span>
                    {r.part_number && (
                      <span className="text-slate-400 ml-2">- {r.part_number}</span>
                    )}
                  </div>
                </div>
                <div className="text-sm text-slate-400">
                  {r.quantity !== undefined && <span>Qty: {r.quantity}</span>}
                  {r.location && <span className="ml-4">@ {r.location}</span>}
                </div>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Lot Trace Result */}
      {lotTrace && (
        <div className="space-y-6">
          {/* Summary Card */}
          <div className="card">
            <div className="flex items-start justify-between">
              <div>
                <div className="flex items-center mb-2">
                  <CubeIcon className="h-8 w-8 text-werco-primary mr-3" />
                  <div>
                    <h2 className="text-xl font-bold">Lot: {lotTrace.lot_number}</h2>
                    {lotTrace.part_number && (
                      <p className="text-slate-400">{lotTrace.part_number} - {lotTrace.part_name}</p>
                    )}
                  </div>
                </div>
              </div>
              <span className={`px-3 py-1 rounded-full text-sm font-medium ${
                lotTrace.status === 'available' ? 'bg-green-500/20 text-emerald-300' :
                lotTrace.status === 'quarantine' ? 'bg-yellow-500/20 text-yellow-300' :
                lotTrace.status === 'rejected' ? 'bg-red-500/20 text-red-300' :
                'bg-slate-800/50 text-slate-100'
              }`}>
                {lotTrace.status}
              </span>
            </div>

            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mt-4">
              <div>
                <label className="text-sm text-slate-400">Current Quantity</label>
                <p className="font-bold text-lg">{lotTrace.current_quantity}</p>
              </div>
              <div>
                <label className="text-sm text-slate-400">Location</label>
                <p className="font-medium">{lotTrace.current_location || '-'}</p>
              </div>
              <div>
                <label className="text-sm text-slate-400">Supplier</label>
                <p className="font-medium">{lotTrace.supplier_name || '-'}</p>
              </div>
              <div>
                <label className="text-sm text-slate-400">PO Number</label>
                <p className="font-medium">{lotTrace.po_number || '-'}</p>
              </div>
              <div>
                <label className="text-sm text-slate-400">Cert Number</label>
                <p className="font-mono">{lotTrace.cert_number || '-'}</p>
              </div>
              <div>
                <label className="text-sm text-slate-400">Heat Lot</label>
                <p className="font-mono">{lotTrace.heat_lot || '-'}</p>
              </div>
              <div>
                <label className="text-sm text-slate-400">Received Date</label>
                <p className="font-medium">
                  {lotTrace.received_date
                    ? formatCentralDate(lotTrace.received_date, { month: '2-digit', day: '2-digit', year: 'numeric' })
                    : '-'}
                </p>
              </div>
            </div>
          </div>

          {/* Usage Summary */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div className="card">
              <div className="flex items-center mb-2">
                <ClipboardDocumentListIcon className="h-6 w-6 text-blue-600 mr-2" />
                <h3 className="font-semibold">Work Orders</h3>
              </div>
              {lotTrace.work_orders_used.length > 0 ? (
                <ul className="text-sm space-y-1">
                  {lotTrace.work_orders_used.map((wo, idx) => (
                    <li key={idx} className="font-mono text-blue-600">{wo}</li>
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-slate-400">Not used in any work orders</p>
              )}
            </div>

            <div className="card">
              <div className="flex items-center mb-2">
                <TruckIcon className="h-6 w-6 text-green-600 mr-2" />
                <h3 className="font-semibold">Shipments</h3>
              </div>
              {lotTrace.shipments.length > 0 ? (
                <ul className="text-sm space-y-1">
                  {lotTrace.shipments.map((s, idx) => (
                    <li key={idx} className="font-mono text-green-600">{s}</li>
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-slate-400">Not shipped</p>
              )}
            </div>

            <div className="card">
              <div className="flex items-center mb-2">
                <ExclamationTriangleIcon className="h-6 w-6 text-red-600 mr-2" />
                <h3 className="font-semibold">NCRs</h3>
              </div>
              {lotTrace.ncrs.length > 0 ? (
                <ul className="text-sm space-y-1">
                  {lotTrace.ncrs.map((ncr, idx) => (
                    <li key={idx} className="font-mono text-red-600">{ncr}</li>
                  ))}
                </ul>
              ) : (
                <div className="flex items-center text-sm text-green-600">
                  <CheckCircleIcon className="h-4 w-4 mr-1" />
                  No quality issues
                </div>
              )}
            </div>
          </div>

          {/* Timeline */}
          <div className="card">
            <h3 className="font-semibold mb-4">History Timeline</h3>
            <div className="relative">
              <div className="absolute left-4 top-0 bottom-0 w-0.5 bg-gray-200"></div>
              <div className="space-y-4">
                {lotTrace.history.map((item, idx) => (
                  <div key={idx} className="relative flex items-start pl-10">
                    <div className="absolute left-2 p-1 bg-[#151b28] rounded-full border">
                      {eventTypeIcons[item.event_type] || <CubeIcon className="h-5 w-5 text-slate-400" />}
                    </div>
                    <div className="flex-1 bg-slate-800 rounded-lg p-3">
                      <div className="flex justify-between items-start">
                        <div>
                          <span className="font-medium">{item.description}</span>
                          {item.reference && (
                            <span className="ml-2 text-sm text-slate-400">
                              Ref: {item.reference}
                            </span>
                          )}
                        </div>
                        <span className="text-sm text-slate-400">
                          {formatCentralDateTime(item.timestamp, {
                            month: '2-digit',
                            day: '2-digit',
                            year: 'numeric',
                          })}
                        </span>
                      </div>
                      {item.user && (
                        <p className="text-sm text-slate-400 mt-1">By: {item.user}</p>
                      )}
                      {item.location && (
                        <p className="text-sm text-slate-400">Location: {item.location}</p>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
            {lotTrace.history.length === 0 && (
              <p className="text-center text-slate-400 py-4">No history recorded</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
