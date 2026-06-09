/**
 * Schedule-Shipment wizard — the real multi-carrier flow that replaces the old
 * hard-coded carrier dropdown + ``prompt()`` tracking capture.
 *
 * Steps:
 *   1. Packages   — one or more parcel rows (dims + weight); a Freight toggle
 *                   adds pallet rows with freight class / NMFC.
 *   2. Address    — validate the ship-to via ``validateAddress``; show the
 *                   normalized address + any messages; proceed with the chosen one.
 *   3. Rates      — ``rateShop`` -> a sortable rate-comparison table (carrier /
 *                   service / mode / amount / ETA); the user selects one.
 *   4. Buy        — ``buyLabel`` for parcel, ``buyBol`` for freight; on success
 *                   show the tracking number + a Print Label/BOL button.
 *
 * Egress is OFF by default server-side: any provider-calling step can return
 * HTTP 409. We surface that as a clear inline banner (CTA to Admin > Carriers),
 * NOT a raw toast. HTTP 501 from buy-bol (EasyPost doesn't do freight) is handled
 * gracefully as "freight purchase not yet enabled for this carrier".
 *
 * Carrier actions are gated on ``shipping:complete`` (the role set the backend
 * restricts carrier writes to: ADMIN / MANAGER / SUPERVISOR / SHIPPING).
 */

import React, { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  XMarkIcon,
  PlusIcon,
  TrashIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  MapPinIcon,
  CubeIcon,
  CurrencyDollarIcon,
  PrinterIcon,
  ArrowsUpDownIcon,
} from '@heroicons/react/24/outline';
import api from '../../services/api';
import { useToast } from '../ui/Toast';
import { LoadingButton } from '../ui/LoadingButton';
import { formatCentralDate } from '../../utils/centralTime';
import type {
  ShippingAddress,
  AddressValidationResult,
  RateQuote,
  ParcelInput,
  PalletInput,
  ShipMode,
} from '../../types/shipping';

export interface ScheduleShipmentTarget {
  shipment_id: number;
  shipment_number?: string;
  work_order_number?: string;
  ship_to_name?: string;
  customer_name?: string;
  part_number?: string;
}

type Step = 'packages' | 'address' | 'rates' | 'buy';

type SortKey = 'price' | 'eta';

interface ParcelRow {
  length_in: string;
  width_in: string;
  height_in: string;
  weight_lbs: string;
}

interface PalletRow extends ParcelRow {
  freight_class: string;
  nmfc: string;
  stackable: boolean;
}

interface BuyOutcome {
  carrier?: string | null;
  service?: string | null;
  tracking_number?: string | null;
  cost?: string | number | null;
  currency?: string | null;
  documentId?: number | null;
  alreadyPurchased: boolean;
  mode: ShipMode;
}

const emptyParcel = (): ParcelRow => ({ length_in: '', width_in: '', height_in: '', weight_lbs: '' });
const emptyPallet = (): PalletRow => ({
  length_in: '',
  width_in: '',
  height_in: '',
  weight_lbs: '',
  freight_class: '',
  nmfc: '',
  stackable: false,
});

const emptyAddress = (name?: string): ShippingAddress => ({
  name: name || '',
  company: '',
  phone: '',
  street1: '',
  street2: '',
  city: '',
  state: '',
  zip: '',
  country: 'US',
});

const STEPS: { key: Step; label: string; icon: React.ComponentType<{ className?: string }> }[] = [
  { key: 'packages', label: 'Packages', icon: CubeIcon },
  { key: 'address', label: 'Address', icon: MapPinIcon },
  { key: 'rates', label: 'Rates', icon: CurrencyDollarIcon },
  { key: 'buy', label: 'Buy', icon: PrinterIcon },
];

const httpStatus = (err: unknown): number | undefined =>
  (err as { response?: { status?: number } })?.response?.status;
const httpDetail = (err: unknown, fallback: string): string =>
  (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
  (err as { message?: string })?.message ||
  fallback;

const money = (amount: string | number | null | undefined, currency?: string | null): string => {
  if (amount == null || amount === '') return '—';
  const n = typeof amount === 'number' ? amount : parseFloat(amount);
  if (!Number.isFinite(n)) return String(amount);
  return `${n.toFixed(2)} ${currency || 'USD'}`;
};

const num = (v: string): number => {
  const n = parseFloat(v);
  return Number.isFinite(n) ? n : 0;
};

export default function ScheduleShipmentModal({
  target,
  onClose,
  onCompleted,
}: {
  target: ScheduleShipmentTarget;
  onClose: () => void;
  onCompleted: () => void;
}) {
  const { showToast } = useToast();

  const [step, setStep] = useState<Step>('packages');
  const [isFreight, setIsFreight] = useState(false);
  const [parcels, setParcels] = useState<ParcelRow[]>([emptyParcel()]);
  const [pallets, setPallets] = useState<PalletRow[]>([emptyPallet()]);
  const [palletCount, setPalletCount] = useState('1');

  const [address, setAddress] = useState<ShippingAddress>(
    emptyAddress(target.ship_to_name || target.customer_name),
  );
  const [validation, setValidation] = useState<AddressValidationResult | null>(null);
  const [useNormalized, setUseNormalized] = useState(true);

  const [rates, setRates] = useState<RateQuote[]>([]);
  const [sortKey, setSortKey] = useState<SortKey>('price');
  const [selectedRate, setSelectedRate] = useState<RateQuote | null>(null);

  const [outcome, setOutcome] = useState<BuyOutcome | null>(null);

  const [busy, setBusy] = useState(false);
  // When set, an egress-disabled (HTTP 409) banner is shown instead of a toast.
  const [egressBlocked, setEgressBlocked] = useState<string | null>(null);

  const effectiveAddress: ShippingAddress = useMemo(
    () => (validation && useNormalized ? validation.normalized : address),
    [validation, useNormalized, address],
  );

  const sortedRates = useMemo(() => {
    const copy = [...rates];
    copy.sort((a, b) => {
      if (sortKey === 'price') return num(String(a.amount)) - num(String(b.amount));
      // ETA: nulls last.
      const ea = a.est_delivery_days ?? Number.MAX_SAFE_INTEGER;
      const eb = b.est_delivery_days ?? Number.MAX_SAFE_INTEGER;
      return ea - eb;
    });
    return copy;
  }, [rates, sortKey]);

  // ----- step 1: packages -----
  const updateParcel = (i: number, patch: Partial<ParcelRow>) =>
    setParcels((prev) => prev.map((p, idx) => (idx === i ? { ...p, ...patch } : p)));
  const updatePallet = (i: number, patch: Partial<PalletRow>) =>
    setPallets((prev) => prev.map((p, idx) => (idx === i ? { ...p, ...patch } : p)));

  const packagesValid = (): boolean => {
    const rows = isFreight ? pallets : parcels;
    return rows.every(
      (r) => num(r.length_in) > 0 && num(r.width_in) > 0 && num(r.height_in) > 0 && num(r.weight_lbs) > 0,
    );
  };

  const goToAddress = () => {
    if (!packagesValid()) {
      showToast('error', 'Every package needs positive length, width, height, and weight.');
      return;
    }
    setStep('address');
  };

  // ----- step 2: address -----
  const handleValidate = async () => {
    if (!address.street1.trim() || !address.city.trim() || !address.state.trim() || !address.zip.trim()) {
      showToast('error', 'Street, city, state, and ZIP are required.');
      return;
    }
    setBusy(true);
    setEgressBlocked(null);
    try {
      const result = await api.validateAddress({ address });
      setValidation(result);
      setUseNormalized(true);
      if (!result.is_valid) {
        showToast('info', 'Address could not be fully verified — review the messages before proceeding.');
      } else {
        showToast('success', 'Address validated.');
      }
    } catch (err) {
      if (httpStatus(err) === 409) {
        setEgressBlocked(httpDetail(err, 'Carrier egress is disabled.'));
      } else {
        showToast('error', httpDetail(err, 'Address validation failed.'));
      }
    } finally {
      setBusy(false);
    }
  };

  // ----- step 3: rate shop -----
  const handleRateShop = async () => {
    setBusy(true);
    setEgressBlocked(null);
    try {
      const parcelsPayload: ParcelInput[] = isFreight
        ? []
        : parcels.map((p) => ({
            length_in: num(p.length_in),
            width_in: num(p.width_in),
            height_in: num(p.height_in),
            weight_lbs: num(p.weight_lbs),
          }));
      const palletsPayload: PalletInput[] = isFreight
        ? pallets.map((p) => ({
            length_in: num(p.length_in),
            width_in: num(p.width_in),
            height_in: num(p.height_in),
            weight_lbs: num(p.weight_lbs),
            freight_class: p.freight_class || undefined,
            nmfc: p.nmfc || undefined,
            stackable: p.stackable,
          }))
        : [];

      const quotes = await api.rateShop(target.shipment_id, {
        ship_to: effectiveAddress,
        parcels: parcelsPayload,
        pallets: palletsPayload,
      });
      setRates(quotes);
      setSelectedRate(quotes.find((q) => q.is_selected) || null);
      setStep('rates');
      if (quotes.length === 0) {
        showToast('info', 'No rates were returned for these packages and destination.');
      }
    } catch (err) {
      if (httpStatus(err) === 409) {
        setEgressBlocked(httpDetail(err, 'Carrier egress is disabled.'));
      } else {
        showToast('error', httpDetail(err, 'Rate shop failed.'));
      }
    } finally {
      setBusy(false);
    }
  };

  // ----- step 4: buy -----
  const handleBuy = async () => {
    if (!selectedRate) {
      showToast('error', 'Select a rate first.');
      return;
    }
    setBusy(true);
    setEgressBlocked(null);
    try {
      if (selectedRate.mode === 'freight') {
        const res = await api.buyBol(target.shipment_id, { rate_id: selectedRate.provider_rate_id });
        setOutcome({
          carrier: res.carrier,
          service: res.bol_number ? `BOL ${res.bol_number}` : selectedRate.service_name,
          tracking_number: res.pro_number || res.bol_number,
          cost: res.actual_cost ?? selectedRate.amount,
          currency: res.cost_currency || selectedRate.currency,
          documentId: res.bol_document_id,
          alreadyPurchased: res.already_purchased,
          mode: 'freight',
        });
      } else {
        const res = await api.buyLabel(target.shipment_id, { rate_id: selectedRate.provider_rate_id });
        setOutcome({
          carrier: res.carrier,
          service: res.service_code || selectedRate.service_name,
          tracking_number: res.tracking_number,
          cost: res.actual_cost ?? selectedRate.amount,
          currency: res.cost_currency || selectedRate.currency,
          documentId: res.label_document_id,
          alreadyPurchased: res.already_purchased,
          mode: 'parcel',
        });
      }
      setStep('buy');
      showToast('success', selectedRate.mode === 'freight' ? 'Bill of Lading purchased.' : 'Label purchased.');
      onCompleted();
    } catch (err) {
      const code = httpStatus(err);
      if (code === 409) {
        setEgressBlocked(httpDetail(err, 'Carrier egress is disabled.'));
      } else if (code === 501) {
        // EasyPost has no freight path — graceful, not a raw error.
        showToast(
          'info',
          'Freight purchase is not yet enabled for this carrier. Configure a freight-capable carrier (e.g. FedEx Freight via Zenkraft) in Admin > Carriers.',
        );
      } else {
        showToast('error', httpDetail(err, 'Purchase failed.'));
      }
    } finally {
      setBusy(false);
    }
  };

  const printDoc = () => {
    if (!outcome?.documentId) return;
    const type = outcome.mode === 'freight' ? 'bol' : 'label';
    window.open(
      `/print/shipping-label/${target.shipment_id}?doc=${outcome.documentId}&type=${type}`,
      '_blank',
    );
  };

  const currentStepIndex = STEPS.findIndex((s) => s.key === step);

  return (
    <div className="fixed inset-0 bg-black bg-opacity-60 flex items-center justify-center z-50 p-4">
      <div className="bg-[#151b28] border border-slate-700 w-full max-w-3xl max-h-[90vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-slate-700 px-5 py-4">
          <div>
            <h3 className="text-lg font-semibold text-white">Schedule Shipment</h3>
            <p className="text-xs text-slate-400">
              {target.shipment_number ? `${target.shipment_number} · ` : ''}
              {target.work_order_number || ''}
              {target.part_number ? ` · ${target.part_number}` : ''}
            </p>
          </div>
          <button onClick={onClose} className="p-2 text-slate-400 hover:text-white">
            <XMarkIcon className="h-5 w-5" />
          </button>
        </div>

        {/* Stepper */}
        <div className="flex items-center gap-1 border-b border-slate-700 px-5 py-3">
          {STEPS.map((s, idx) => {
            const Icon = s.icon;
            const active = s.key === step;
            const done = idx < currentStepIndex;
            return (
              <React.Fragment key={s.key}>
                <div
                  className={`flex items-center gap-1.5 text-xs font-medium ${
                    active ? 'text-werco-primary' : done ? 'text-emerald-400' : 'text-slate-500'
                  }`}
                >
                  <span
                    className={`flex h-6 w-6 items-center justify-center border text-[11px] ${
                      active
                        ? 'border-werco-primary text-werco-primary'
                        : done
                          ? 'border-emerald-400 text-emerald-400'
                          : 'border-slate-600 text-slate-500'
                    }`}
                  >
                    {done ? '✓' : idx + 1}
                  </span>
                  <Icon className="h-4 w-4" />
                  {s.label}
                </div>
                {idx < STEPS.length - 1 && <div className="h-px flex-1 bg-slate-700" />}
              </React.Fragment>
            );
          })}
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          {egressBlocked && <EgressDisabledBanner detail={egressBlocked} />}

          {step === 'packages' && (
            <PackagesStep
              isFreight={isFreight}
              setIsFreight={setIsFreight}
              parcels={parcels}
              setParcels={setParcels}
              pallets={pallets}
              setPallets={setPallets}
              palletCount={palletCount}
              setPalletCount={setPalletCount}
              updateParcel={updateParcel}
              updatePallet={updatePallet}
            />
          )}

          {step === 'address' && (
            <AddressStep
              address={address}
              setAddress={setAddress}
              validation={validation}
              useNormalized={useNormalized}
              setUseNormalized={setUseNormalized}
            />
          )}

          {step === 'rates' && (
            <RatesStep
              rates={sortedRates}
              sortKey={sortKey}
              setSortKey={setSortKey}
              selectedRate={selectedRate}
              setSelectedRate={setSelectedRate}
            />
          )}

          {step === 'buy' && outcome && <BuyResultStep outcome={outcome} onPrint={printDoc} />}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between border-t border-slate-700 px-5 py-4">
          <div>
            {step !== 'packages' && step !== 'buy' && (
              <button
                onClick={() => setStep(step === 'address' ? 'packages' : 'address')}
                className="btn-secondary"
                disabled={busy}
              >
                Back
              </button>
            )}
          </div>
          <div className="flex items-center gap-3">
            {step === 'packages' && (
              <button onClick={goToAddress} className="btn-primary">
                Continue
              </button>
            )}
            {step === 'address' && (
              <>
                <LoadingButton onClick={handleValidate} loading={busy} loadingText="Validating…" variant="secondary">
                  Validate Address
                </LoadingButton>
                <LoadingButton onClick={handleRateShop} loading={busy} loadingText="Shopping…">
                  Get Rates
                </LoadingButton>
              </>
            )}
            {step === 'rates' && (
              <LoadingButton onClick={handleBuy} loading={busy} loadingText="Purchasing…" disabled={!selectedRate}>
                {selectedRate?.mode === 'freight' ? 'Buy BOL' : 'Buy Label'}
              </LoadingButton>
            )}
            {step === 'buy' && (
              <button onClick={onClose} className="btn-primary">
                Done
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Egress-disabled banner (HTTP 409). Not a raw toast.
// ---------------------------------------------------------------------------

function EgressDisabledBanner({ detail }: { detail: string }) {
  return (
    <div className="flex items-start gap-3 border border-amber-500/40 bg-amber-500/5 px-4 py-3">
      <ExclamationTriangleIcon className="h-5 w-5 text-amber-400 flex-shrink-0 mt-0.5" />
      <div className="text-sm">
        <p className="font-semibold text-amber-300">Carrier egress is turned off</p>
        <p className="text-slate-400 mt-0.5">
          {detail} Rate-shop, address validation, and label/BOL purchase transmit customer addresses to a
          third-party carrier and stay blocked until carrier egress is enabled — after CUI / data-egress
          sign-off.
        </p>
        <Link
          to="/admin/settings?tab=carriers"
          className="mt-1.5 inline-block text-werco-primary hover:underline font-medium"
        >
          Enable carrier egress in Admin &gt; Carriers →
        </Link>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 1: packages / pallets.
// ---------------------------------------------------------------------------

function PackagesStep({
  isFreight,
  setIsFreight,
  parcels,
  setParcels,
  pallets,
  setPallets,
  palletCount,
  setPalletCount,
  updateParcel,
  updatePallet,
}: {
  isFreight: boolean;
  setIsFreight: (v: boolean) => void;
  parcels: ParcelRow[];
  setParcels: React.Dispatch<React.SetStateAction<ParcelRow[]>>;
  pallets: PalletRow[];
  setPallets: React.Dispatch<React.SetStateAction<PalletRow[]>>;
  palletCount: string;
  setPalletCount: (v: string) => void;
  updateParcel: (i: number, patch: Partial<ParcelRow>) => void;
  updatePallet: (i: number, patch: Partial<PalletRow>) => void;
}) {
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-slate-300">Enter each box (parcel) or pallet (freight) to ship.</p>
        <div className="inline-flex border border-slate-700 text-xs">
          <button
            type="button"
            onClick={() => setIsFreight(false)}
            className={`px-3 py-1.5 ${!isFreight ? 'bg-werco-primary text-white' : 'text-slate-400 hover:text-white'}`}
          >
            Parcel
          </button>
          <button
            type="button"
            onClick={() => setIsFreight(true)}
            className={`px-3 py-1.5 ${isFreight ? 'bg-werco-primary text-white' : 'text-slate-400 hover:text-white'}`}
          >
            Freight / LTL
          </button>
        </div>
      </div>

      {!isFreight &&
        parcels.map((p, i) => (
          <div key={i} className="grid grid-cols-12 gap-2 items-end border border-slate-700 p-3">
            <DimField label="Length (in)" value={p.length_in} onChange={(v) => updateParcel(i, { length_in: v })} />
            <DimField label="Width (in)" value={p.width_in} onChange={(v) => updateParcel(i, { width_in: v })} />
            <DimField label="Height (in)" value={p.height_in} onChange={(v) => updateParcel(i, { height_in: v })} />
            <DimField label="Weight (lbs)" value={p.weight_lbs} onChange={(v) => updateParcel(i, { weight_lbs: v })} />
            <div className="col-span-12 sm:col-span-2 flex justify-end">
              {parcels.length > 1 && (
                <button
                  type="button"
                  onClick={() => setParcels((prev) => prev.filter((_, idx) => idx !== i))}
                  className="p-2 text-slate-500 hover:text-red-400"
                  title="Remove package"
                >
                  <TrashIcon className="h-4 w-4" />
                </button>
              )}
            </div>
          </div>
        ))}

      {isFreight &&
        pallets.map((p, i) => (
          <div key={i} className="space-y-2 border border-slate-700 p-3">
            <div className="grid grid-cols-12 gap-2 items-end">
              <DimField label="Length (in)" value={p.length_in} onChange={(v) => updatePallet(i, { length_in: v })} />
              <DimField label="Width (in)" value={p.width_in} onChange={(v) => updatePallet(i, { width_in: v })} />
              <DimField label="Height (in)" value={p.height_in} onChange={(v) => updatePallet(i, { height_in: v })} />
              <DimField label="Weight (lbs)" value={p.weight_lbs} onChange={(v) => updatePallet(i, { weight_lbs: v })} />
              <div className="col-span-12 sm:col-span-2 flex justify-end">
                {pallets.length > 1 && (
                  <button
                    type="button"
                    onClick={() => setPallets((prev) => prev.filter((_, idx) => idx !== i))}
                    className="p-2 text-slate-500 hover:text-red-400"
                    title="Remove pallet"
                  >
                    <TrashIcon className="h-4 w-4" />
                  </button>
                )}
              </div>
            </div>
            <div className="grid grid-cols-12 gap-2 items-end">
              <div className="col-span-6 sm:col-span-3">
                <label className="label">Freight class</label>
                <input
                  className="input"
                  value={p.freight_class}
                  onChange={(e) => updatePallet(i, { freight_class: e.target.value })}
                  placeholder="e.g. 70"
                />
              </div>
              <div className="col-span-6 sm:col-span-3">
                <label className="label">NMFC</label>
                <input
                  className="input"
                  value={p.nmfc}
                  onChange={(e) => updatePallet(i, { nmfc: e.target.value })}
                  placeholder="NMFC code"
                />
              </div>
              <div className="col-span-12 sm:col-span-3 flex items-center pb-2">
                <label className="flex items-center gap-2 text-sm text-slate-300">
                  <input
                    type="checkbox"
                    checked={p.stackable}
                    onChange={(e) => updatePallet(i, { stackable: e.target.checked })}
                  />
                  Stackable
                </label>
              </div>
            </div>
          </div>
        ))}

      {isFreight && (
        <div className="w-40">
          <label className="label">Pallet count (BOL)</label>
          <input
            type="number"
            min={1}
            className="input"
            value={palletCount}
            onChange={(e) => setPalletCount(e.target.value)}
          />
        </div>
      )}

      <button
        type="button"
        onClick={() =>
          isFreight ? setPallets((prev) => [...prev, emptyPallet()]) : setParcels((prev) => [...prev, emptyParcel()])
        }
        className="btn-secondary text-sm inline-flex items-center"
      >
        <PlusIcon className="h-4 w-4 mr-1" />
        Add {isFreight ? 'pallet' : 'package'}
      </button>
    </div>
  );
}

function DimField({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <div className="col-span-6 sm:col-span-2">
      <label className="label">{label}</label>
      <input
        type="number"
        min={0}
        step="0.1"
        className="input"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 2: ship-to address + validation result.
// ---------------------------------------------------------------------------

function AddressStep({
  address,
  setAddress,
  validation,
  useNormalized,
  setUseNormalized,
}: {
  address: ShippingAddress;
  setAddress: React.Dispatch<React.SetStateAction<ShippingAddress>>;
  validation: AddressValidationResult | null;
  useNormalized: boolean;
  setUseNormalized: (v: boolean) => void;
}) {
  const set = (patch: Partial<ShippingAddress>) => setAddress((prev) => ({ ...prev, ...patch }));
  return (
    <div className="space-y-4">
      <p className="text-sm text-slate-300">
        Confirm the ship-to address, then validate it with the carrier (or skip straight to rates).
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div className="sm:col-span-2">
          <label className="label">Name / Attn</label>
          <input className="input" value={address.name ?? ''} onChange={(e) => set({ name: e.target.value })} />
        </div>
        <div className="sm:col-span-2">
          <label className="label">Company</label>
          <input className="input" value={address.company ?? ''} onChange={(e) => set({ company: e.target.value })} />
        </div>
        <div className="sm:col-span-2">
          <label className="label">Street 1</label>
          <input
            className="input"
            aria-label="Street 1"
            value={address.street1}
            onChange={(e) => set({ street1: e.target.value })}
          />
        </div>
        <div className="sm:col-span-2">
          <label className="label">Street 2</label>
          <input
            className="input"
            aria-label="Street 2"
            value={address.street2 ?? ''}
            onChange={(e) => set({ street2: e.target.value })}
          />
        </div>
        <div>
          <label className="label">City</label>
          <input
            className="input"
            aria-label="City"
            value={address.city}
            onChange={(e) => set({ city: e.target.value })}
          />
        </div>
        <div className="grid grid-cols-3 gap-2">
          <div>
            <label className="label">State</label>
            <input
              className="input"
              aria-label="State"
              value={address.state}
              onChange={(e) => set({ state: e.target.value })}
            />
          </div>
          <div>
            <label className="label">ZIP</label>
            <input
              className="input"
              aria-label="ZIP"
              value={address.zip}
              onChange={(e) => set({ zip: e.target.value })}
            />
          </div>
          <div>
            <label className="label">Country</label>
            <input
              className="input"
              value={address.country ?? 'US'}
              onChange={(e) => set({ country: e.target.value })}
            />
          </div>
        </div>
        <div>
          <label className="label">Phone</label>
          <input className="input" value={address.phone ?? ''} onChange={(e) => set({ phone: e.target.value })} />
        </div>
      </div>

      {validation && (
        <div
          className={`border px-4 py-3 ${
            validation.is_valid ? 'border-emerald-500/40 bg-emerald-500/5' : 'border-amber-500/40 bg-amber-500/5'
          }`}
        >
          <div className="flex items-center gap-2 mb-2">
            {validation.is_valid ? (
              <CheckCircleIcon className="h-5 w-5 text-emerald-400" />
            ) : (
              <ExclamationTriangleIcon className="h-5 w-5 text-amber-400" />
            )}
            <span className="text-sm font-semibold text-slate-200">
              {validation.is_valid ? 'Address verified' : 'Address could not be fully verified'}
              {validation.deliverability ? ` · ${validation.deliverability}` : ''}
            </span>
          </div>
          <div className="text-sm text-slate-300 whitespace-pre-line">
            {[
              validation.normalized.street1,
              validation.normalized.street2,
              `${validation.normalized.city}, ${validation.normalized.state} ${validation.normalized.zip}`,
              validation.normalized.country,
            ]
              .filter(Boolean)
              .join('\n')}
          </div>
          {validation.messages.length > 0 && (
            <ul className="mt-2 list-disc list-inside text-xs text-slate-400 space-y-0.5">
              {validation.messages.map((m, i) => (
                <li key={i}>{m}</li>
              ))}
            </ul>
          )}
          <label className="mt-2 flex items-center gap-2 text-xs text-slate-300">
            <input type="checkbox" checked={useNormalized} onChange={(e) => setUseNormalized(e.target.checked)} />
            Use the carrier-normalized address for rating
          </label>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 3: rate-comparison table.
// ---------------------------------------------------------------------------

function RatesStep({
  rates,
  sortKey,
  setSortKey,
  selectedRate,
  setSelectedRate,
}: {
  rates: RateQuote[];
  sortKey: SortKey;
  setSortKey: (k: SortKey) => void;
  selectedRate: RateQuote | null;
  setSelectedRate: (r: RateQuote) => void;
}) {
  if (rates.length === 0) {
    return <p className="text-center text-slate-400 py-10">No rates returned. Go back and adjust packages.</p>;
  }
  const SortBtn = ({ k, children }: { k: SortKey; children: React.ReactNode }) => (
    <button
      type="button"
      onClick={() => setSortKey(k)}
      className={`inline-flex items-center gap-1 ${sortKey === k ? 'text-werco-primary' : 'text-slate-400'}`}
    >
      <ArrowsUpDownIcon className="h-3.5 w-3.5" />
      {children}
    </button>
  );
  const rateKey = (r: RateQuote) => `${r.id ?? ''}:${r.provider_rate_id}`;
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-4 text-xs">
        <span className="text-slate-500">Sort by:</span>
        <SortBtn k="price">Price</SortBtn>
        <SortBtn k="eta">Delivery ETA</SortBtn>
      </div>
      <div className="overflow-x-auto border border-slate-700">
        <table className="min-w-full divide-y divide-slate-700 text-sm">
          <thead className="bg-slate-800">
            <tr>
              <th className="px-3 py-2 w-8"></th>
              <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Carrier</th>
              <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Service</th>
              <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Mode</th>
              <th className="px-3 py-2 text-right text-xs font-medium text-slate-400 uppercase">Amount</th>
              <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Est. delivery</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-700">
            {rates.map((r) => {
              const selected = selectedRate && rateKey(selectedRate) === rateKey(r);
              return (
                <tr
                  key={rateKey(r)}
                  onClick={() => setSelectedRate(r)}
                  className={`cursor-pointer ${selected ? 'bg-werco-primary/15' : 'hover:bg-slate-800'}`}
                >
                  <td className="px-3 py-2 text-center">
                    <input
                      type="radio"
                      name="rate"
                      checked={!!selected}
                      onChange={() => setSelectedRate(r)}
                    />
                  </td>
                  <td className="px-3 py-2 font-medium text-slate-200">{r.carrier}</td>
                  <td className="px-3 py-2 text-slate-300">{r.service_name || r.service_code || '—'}</td>
                  <td className="px-3 py-2">
                    <span
                      className={`px-2 py-0.5 text-xs ${
                        r.mode === 'freight' ? 'bg-purple-500/20 text-purple-300' : 'bg-blue-500/20 text-blue-300'
                      }`}
                    >
                      {r.mode}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-slate-200">{money(r.amount, r.currency)}</td>
                  <td className="px-3 py-2 text-slate-300">
                    {r.est_delivery_date
                      ? formatCentralDate(r.est_delivery_date, { year: undefined })
                      : r.est_delivery_days != null
                        ? `${r.est_delivery_days} day${r.est_delivery_days === 1 ? '' : 's'}`
                        : '—'}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 4: purchase result.
// ---------------------------------------------------------------------------

function BuyResultStep({ outcome, onPrint }: { outcome: BuyOutcome; onPrint: () => void }) {
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3 border border-emerald-500/40 bg-emerald-500/5 px-4 py-3">
        <CheckCircleIcon className="h-6 w-6 text-emerald-400" />
        <div>
          <p className="font-semibold text-emerald-300">
            {outcome.mode === 'freight' ? 'Bill of Lading' : 'Label'}{' '}
            {outcome.alreadyPurchased ? 'already purchased' : 'purchased'}
          </p>
          <p className="text-xs text-slate-400">
            {outcome.carrier || '—'} {outcome.service ? `· ${outcome.service}` : ''}
          </p>
        </div>
      </div>

      <dl className="grid grid-cols-2 gap-3 text-sm">
        <div>
          <dt className="text-xs text-slate-500 uppercase">
            {outcome.mode === 'freight' ? 'PRO / BOL #' : 'Tracking #'}
          </dt>
          <dd className="font-mono text-slate-200">{outcome.tracking_number || '—'}</dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500 uppercase">Actual cost</dt>
          <dd className="font-mono text-slate-200">{money(outcome.cost, outcome.currency)}</dd>
        </div>
      </dl>

      <button onClick={onPrint} disabled={!outcome.documentId} className="btn-primary inline-flex items-center">
        <PrinterIcon className="h-4 w-4 mr-1.5" />
        Print {outcome.mode === 'freight' ? 'BOL' : 'Label'}
      </button>
      {!outcome.documentId && (
        <p className="text-xs text-slate-500">No printable document was returned by the carrier.</p>
      )}
    </div>
  );
}
