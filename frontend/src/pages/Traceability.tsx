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
  received: <TruckIcon className="h-4 w-4 text-fd-green" />,
  receive: <TruckIcon className="h-4 w-4 text-fd-green" />,
  issue: <ArrowRightIcon className="h-4 w-4 text-fd-blue" />,
  transfer: <ArrowRightIcon className="h-4 w-4 text-fd-cyan" />,
  adjust: <ClipboardDocumentListIcon className="h-4 w-4 text-fd-amber" />,
  scrap: <ExclamationTriangleIcon className="h-4 w-4 text-fd-red" />,
  ncr: <ExclamationTriangleIcon className="h-4 w-4 text-fd-amber" />,
  ship: <TruckIcon className="h-4 w-4 text-fd-blue" />,
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
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-2xl font-bold text-white">Lot / Serial Traceability</h1>
        <p className="flex items-center gap-1.5 text-xs text-fd-blue">
          <CheckCircleIcon className="h-4 w-4 flex-shrink-0" />
          <span>
            <strong className="font-semibold">AS9100D:</strong> lot/serial/cert tracking across the full
            production lifecycle — receiving through shipment.
          </span>
        </p>
      </div>

      {/* Search */}
      <div className="bg-fd-panel border border-fd-line rounded-sm p-3" data-tour="qa-traceability">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400 mb-2">Search</h2>
        <div className="flex gap-3">
          <div className="relative flex-1">
            <MagnifyingGlassIcon className="h-5 w-5 absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onKeyDown={handleKeyDown}
              className="input pl-10"
              placeholder="Enter lot number, serial number, cert number, or heat lot..."
              aria-label="Search by lot, serial, cert, or heat lot number"
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
        <div className="bg-fd-red/10 border border-fd-red/30 text-fd-red px-3 py-2 rounded-sm text-sm">
          {error}
        </div>
      )}

      {/* Search Results */}
      {searchResults.length > 1 && !lotTrace && (
        <div className="bg-fd-panel border border-fd-line rounded-sm p-3">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-slate-400 mb-2">
            Search Results ({searchResults.length})
          </h3>
          <div className="space-y-1.5">
            {searchResults.map((r, idx) => (
              <button
                key={idx}
                onClick={() => loadLotTrace(r.number)}
                className="w-full text-left p-2.5 border border-fd-line rounded-sm hover:bg-fd-sunken flex items-center justify-between gap-3 min-w-0"
              >
                <div className="flex items-center min-w-0">
                  <span className={`px-1.5 py-0.5 rounded-sm text-[10px] font-medium mr-3 flex-shrink-0 ${
                    r.type === 'lot' ? 'bg-fd-blue/15 text-fd-blue' : 'bg-fd-cyan/15 text-fd-cyan'
                  }`}>
                    {r.type.toUpperCase()}
                  </span>
                  <div className="min-w-0">
                    <span className="font-mono font-medium tabular-nums">{r.number}</span>
                    {r.part_number && (
                      <span className="text-slate-400 ml-2 truncate">- {r.part_number}</span>
                    )}
                  </div>
                </div>
                <div className="text-sm text-slate-400 flex-shrink-0 tabular-nums">
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
        <div className="space-y-3">
          {/* Summary Card */}
          <div className="bg-fd-panel border border-fd-line rounded-sm p-3">
            <div className="flex items-start justify-between gap-3">
              <div className="flex items-center min-w-0">
                <CubeIcon className="h-7 w-7 text-werco-navy-600 mr-3 flex-shrink-0" />
                <div className="min-w-0">
                  <h2 className="text-lg font-bold truncate">Lot: <span className="tabular-nums">{lotTrace.lot_number}</span></h2>
                  {lotTrace.part_number && (
                    <p className="text-sm text-slate-400 truncate">{lotTrace.part_number} - {lotTrace.part_name}</p>
                  )}
                </div>
              </div>
              <span className={`px-2 py-0.5 rounded-sm text-xs font-medium flex-shrink-0 ${
                lotTrace.status === 'available' ? 'bg-fd-green/15 text-fd-green' :
                lotTrace.status === 'quarantine' ? 'bg-fd-amber/15 text-fd-amber' :
                lotTrace.status === 'rejected' ? 'bg-fd-red/15 text-fd-red' :
                'bg-fd-sunken text-slate-300'
              }`}>
                {lotTrace.status}
              </span>
            </div>

            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mt-3 pt-3 border-t border-fd-line">
              <div className="min-w-0">
                <span className="block text-[10px] uppercase tracking-wide text-slate-500">Current Quantity</span>
                <p className="font-bold text-base tabular-nums">{lotTrace.current_quantity}</p>
              </div>
              <div className="min-w-0">
                <span className="block text-[10px] uppercase tracking-wide text-slate-500">Location</span>
                <p className="font-medium text-sm truncate">{lotTrace.current_location || '-'}</p>
              </div>
              <div className="min-w-0">
                <span className="block text-[10px] uppercase tracking-wide text-slate-500">Supplier</span>
                <p className="font-medium text-sm truncate">{lotTrace.supplier_name || '-'}</p>
              </div>
              <div className="min-w-0">
                <span className="block text-[10px] uppercase tracking-wide text-slate-500">PO Number</span>
                <p className="font-medium text-sm tabular-nums truncate">{lotTrace.po_number || '-'}</p>
              </div>
              <div className="min-w-0">
                <span className="block text-[10px] uppercase tracking-wide text-slate-500">Cert Number</span>
                <p className="font-mono text-sm tabular-nums truncate">{lotTrace.cert_number || '-'}</p>
              </div>
              <div className="min-w-0">
                <span className="block text-[10px] uppercase tracking-wide text-slate-500">Heat Lot</span>
                <p className="font-mono text-sm tabular-nums truncate">{lotTrace.heat_lot || '-'}</p>
              </div>
              <div className="min-w-0">
                <span className="block text-[10px] uppercase tracking-wide text-slate-500">Received Date</span>
                <p className="font-medium text-sm tabular-nums">
                  {lotTrace.received_date
                    ? formatCentralDate(lotTrace.received_date, { month: '2-digit', day: '2-digit', year: 'numeric' })
                    : '-'}
                </p>
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            {/* Usage Summary */}
            <div className="grid grid-cols-1 md:grid-cols-3 lg:grid-cols-1 gap-3">
              <div className="bg-fd-panel border border-fd-line rounded-sm p-3">
                <div className="flex items-center mb-2">
                  <ClipboardDocumentListIcon className="h-5 w-5 text-fd-blue mr-2" />
                  <h3 className="text-sm font-semibold">Work Orders</h3>
                </div>
                {lotTrace.work_orders_used.length > 0 ? (
                  <ul className="text-sm space-y-1">
                    {lotTrace.work_orders_used.map((wo, idx) => (
                      <li key={idx} className="font-mono tabular-nums text-fd-blue truncate">{wo}</li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-sm text-slate-400">Not used in any work orders</p>
                )}
              </div>

              <div className="bg-fd-panel border border-fd-line rounded-sm p-3">
                <div className="flex items-center mb-2">
                  <TruckIcon className="h-5 w-5 text-fd-green mr-2" />
                  <h3 className="text-sm font-semibold">Shipments</h3>
                </div>
                {lotTrace.shipments.length > 0 ? (
                  <ul className="text-sm space-y-1">
                    {lotTrace.shipments.map((s, idx) => (
                      <li key={idx} className="font-mono tabular-nums text-fd-green truncate">{s}</li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-sm text-slate-400">Not shipped</p>
                )}
              </div>

              <div className="bg-fd-panel border border-fd-line rounded-sm p-3">
                <div className="flex items-center mb-2">
                  <ExclamationTriangleIcon className="h-5 w-5 text-fd-red mr-2" />
                  <h3 className="text-sm font-semibold">NCRs</h3>
                </div>
                {lotTrace.ncrs.length > 0 ? (
                  <ul className="text-sm space-y-1">
                    {lotTrace.ncrs.map((ncr, idx) => (
                      <li key={idx} className="font-mono tabular-nums text-fd-red truncate">{ncr}</li>
                    ))}
                  </ul>
                ) : (
                  <div className="flex items-center text-sm text-fd-green">
                    <CheckCircleIcon className="h-4 w-4 mr-1" />
                    No quality issues
                  </div>
                )}
              </div>
            </div>

            {/* Timeline */}
            <div className="bg-fd-panel border border-fd-line rounded-sm p-3">
              <h3 className="text-sm font-semibold uppercase tracking-wide text-slate-400 mb-3">History Timeline</h3>
              <div className="relative max-h-[28rem] overflow-y-auto">
                <div className="absolute left-3.5 top-0 bottom-0 w-px bg-fd-line"></div>
                <div className="space-y-3">
                  {lotTrace.history.map((item, idx) => (
                    <div key={idx} className="relative flex items-start pl-9">
                      <div className="absolute left-1.5 p-1 bg-fd-sunken rounded-sm border border-fd-line">
                        {eventTypeIcons[item.event_type] || <CubeIcon className="h-4 w-4 text-slate-400" />}
                      </div>
                      <div className="flex-1 min-w-0 bg-fd-sunken border border-fd-line rounded-sm p-2.5">
                        <div className="flex justify-between items-start gap-2">
                          <div className="min-w-0">
                            <span className="font-medium text-sm">{item.description}</span>
                            {item.reference && (
                              <span className="ml-2 text-xs text-slate-400 tabular-nums">
                                Ref: {item.reference}
                              </span>
                            )}
                          </div>
                          <span className="text-xs text-slate-400 tabular-nums flex-shrink-0">
                            {formatCentralDateTime(item.timestamp, {
                              month: '2-digit',
                              day: '2-digit',
                              year: 'numeric',
                            })}
                          </span>
                        </div>
                        {item.user && (
                          <p className="text-xs text-slate-400 mt-1 truncate">By: {item.user}</p>
                        )}
                        {item.location && (
                          <p className="text-xs text-slate-400 truncate">Location: {item.location}</p>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
              {lotTrace.history.length === 0 && (
                <p className="text-center text-sm text-slate-400 py-4">No history recorded</p>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
