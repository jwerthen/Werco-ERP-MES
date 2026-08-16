import React, { useEffect, useRef, useState } from 'react';
import api from '../services/api';
import { useSearchParams } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { hasPermission } from '../utils/permissions';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import useUnsavedChanges from '../hooks/useUnsavedChanges';
import { formatCentralDate, formatCentralDateTime } from '../utils/centralTime';
import { Modal } from '../components/ui/Modal';
import {
  PlusIcon,
  CheckCircleIcon,
  BuildingOfficeIcon,
  ClipboardDocumentListIcon,
  TrashIcon,
} from '@heroicons/react/24/outline';
import { MiniStat, MiniStatStrip } from '../components/cockpit';
import {
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  FormField,
  LoadingButton,
  StatusBadge,
  useToast,
  DataTable,
  DataTableColumn,
  MobileDataCard,
} from '../components/ui';

interface Vendor {
  id: number;
  code: string;
  name: string;
  contact_name?: string;
  email?: string;
  phone?: string;
  address_line1?: string;
  address_line2?: string;
  city?: string;
  state?: string;
  postal_code?: string;
  country?: string;
  payment_terms?: string;
  is_approved: boolean;
  is_as9100_certified: boolean;
  is_iso9001_certified: boolean;
  is_active?: boolean;
  notes?: string;
  version?: number;
}

interface PurchaseOrder {
  id: number;
  po_number: string;
  vendor_id: number;
  vendor_name?: string;
  status: string;
  order_date?: string;
  required_date?: string;
  total: number;
  line_count: number;
  // Soft-delete provenance. Populated ONLY by the deleted view
  // (GET /purchasing/purchase-orders?deleted_only=true); undefined on every live row,
  // which is exactly how the two views stay tellable apart in one shared row shape.
  is_deleted?: boolean;
  deleted_at?: string;
  deleted_by_name?: string;
}

interface Part {
  id: number;
  part_number: string;
  name: string;
}

interface VendorDocument {
  id: number;
  document_number: string;
  revision: string;
  title: string;
  document_type: string;
  description?: string;
  file_name?: string;
  file_size?: number;
  mime_type?: string;
  created_at: string;
}

interface DocumentType {
  value: string;
  label: string;
}

type TabType = 'orders' | 'vendors';

// Which book of purchase orders the Orders tab is showing. Mutually exclusive on
// purpose (see the segmented control in the Orders tab): a deleted PO is a RECORD,
// not a workable order, so it must never sit in the same list as a live one where
// somebody could print it, send it, or receive against it by mistake.
type POView = 'active' | 'deleted';

// Blank form states for the create modals. Module-level constants so the
// unsaved-changes dirty checks can compare against the pristine shape.
const BLANK_PO = {
  vendor_id: 0,
  required_date: '',
  notes: '',
  lines: [] as Array<{ part_id: number; quantity_ordered: number; unit_price: number }>,
};

const BLANK_VENDOR = {
  code: '',
  name: '',
  contact_name: '',
  email: '',
  phone: '',
  is_approved: false,
  payment_terms: '',
};

const BLANK_EDIT_VENDOR = {
  code: '',
  name: '',
  contact_name: '',
  email: '',
  phone: '',
  address_line1: '',
  address_line2: '',
  city: '',
  state: '',
  postal_code: '',
  country: 'US',
  payment_terms: '',
  is_approved: false,
  is_as9100_certified: false,
  is_iso9001_certified: false,
  is_active: true,
  notes: '',
};

export default function Purchasing() {
  const { showToast } = useToast();
  const { user } = useAuth();
  // Mirror the backend role gates so no button 403s:
  // - POST /purchasing/purchase-orders allows admin/manager/supervisor → purchasing:create
  // - PO send and vendor create are admin/manager only → purchasing:approve (same role set)
  const canCreatePO = hasPermission(user?.role, 'purchasing:create');
  const canSendPO = hasPermission(user?.role, 'purchasing:approve');
  const canCreateVendor = hasPermission(user?.role, 'purchasing:approve');
  // Soft-delete of POs and vendors is admin/manager only (DELETE endpoints use
  // require_role([ADMIN, MANAGER])); a superuser qualifies too.
  const canDeletePurchasing = user?.role === 'admin' || user?.role === 'manager' || !!user?.is_superuser;
  // Restoring a soft-deleted PO tracks POST /purchasing/purchase-orders/{id}/restore
  // (require_role([ADMIN, MANAGER]); superuser qualifies) — today the same role set as
  // canDeletePurchasing, but a separate constant so a future change to either gate cannot
  // silently move the other. The deleted VIEW is deliberately not gated: the list endpoint
  // stays on get_current_user because it returns rows this reader could already see before
  // the delete. Only the verb is privileged, so only the button is hidden.
  //
  // PLATFORM_ADMIN is omitted, matching canDeletePurchasing and the house convention noted
  // at Receiving.tsx:364 — the UI does not surface purchasing write controls to it. That is
  // narrower than the server, which admits PLATFORM_ADMIN unconditionally (api/deps.py ::
  // require_role short-circuits on it before the role list is read), so this errs toward a
  // hidden control rather than a button that 403s. Deliberate, not a mismatch to "fix" on
  // its own — the two purchasing gates move together or not at all.
  const canRestorePO = user?.role === 'admin' || user?.role === 'manager' || !!user?.is_superuser;
  const [searchParams] = useSearchParams();
  const [activeTab, setActiveTab] = useState<TabType>('orders');
  const [vendors, setVendors] = useState<Vendor[]>([]);
  const [purchaseOrders, setPurchaseOrders] = useState<PurchaseOrder[]>([]);
  const [parts, setParts] = useState<Part[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [vendorDocsError, setVendorDocsError] = useState(false);
  const [poSearch, setPoSearch] = useState('');
  const debouncedPoSearch = useDebouncedValue(poSearch, 250);

  // The deleted-PO book. Kept in its OWN state rather than merged into
  // `purchaseOrders`, and fetched LAZILY the first time the user switches into the
  // Deleted view — so the default page load emits exactly the requests it always did,
  // and no live-PO count (the KPI strip, the tab badge) can ever pick up a deleted row.
  const [poView, setPoView] = useState<POView>('active');
  const [deletedPOs, setDeletedPOs] = useState<PurchaseOrder[]>([]);
  const [deletedPOsLoading, setDeletedPOsLoading] = useState(false);
  const [deletedPOsError, setDeletedPOsError] = useState(false);
  // Restore is server-GATED (400 if the PO is not actually deleted, 403 below
  // admin/manager), so it stays NON-OPTIMISTIC: the row moves only after the server
  // says yes. Holding the in-flight PO's id rather than a bare boolean lets the one
  // row that was clicked show the spinner while the rest merely disable.
  const [restorePOPendingId, setRestorePOPendingId] = useState<number | null>(null);

  const [showPOModal, setShowPOModal] = useState(false);
  const [showVendorModal, setShowVendorModal] = useState(false);
  const [showEditVendorModal, setShowEditVendorModal] = useState(false);
  const [showAddPartModal, setShowAddPartModal] = useState(false);
  const [addPartForLineIndex, setAddPartForLineIndex] = useState<number | null>(null);
  const [selectedVendor, setSelectedVendor] = useState<Vendor | null>(null);
  const [vendorDocuments, setVendorDocuments] = useState<VendorDocument[]>([]);
  const [documentTypes, setDocumentTypes] = useState<DocumentType[]>([]);
  const [vendorDocsLoading, setVendorDocsLoading] = useState(false);

  // Soft-delete confirm targets + in-flight guards. These deletes are
  // server-GATED (a PO with received material / a vendor with active POs is
  // refused with an actionable 400), so the flow stays non-optimistic: await
  // the call, then reflect only what the server returns.
  const [deletePOTarget, setDeletePOTarget] = useState<PurchaseOrder | null>(null);
  const [deletePOPending, setDeletePOPending] = useState(false);
  const [deleteVendorTarget, setDeleteVendorTarget] = useState<Vendor | null>(null);
  const [deleteVendorPending, setDeleteVendorPending] = useState(false);
  const [deleteVendorDocTarget, setDeleteVendorDocTarget] = useState<VendorDocument | null>(null);
  const [deleteVendorDocPending, setDeleteVendorDocPending] = useState(false);
  const [sendPOTarget, setSendPOTarget] = useState<PurchaseOrder | null>(null);
  const [sendPOPending, setSendPOPending] = useState(false);

  const [newPO, setNewPO] = useState(BLANK_PO);

  const [newVendor, setNewVendor] = useState(BLANK_VENDOR);

  const [editVendorForm, setEditVendorForm] = useState(BLANK_EDIT_VENDOR);
  // Snapshot captured when the edit-vendor modal opens; the dirty check
  // compares against it (Materials.tsx idiom).
  const [initialEditVendorForm, setInitialEditVendorForm] = useState(BLANK_EDIT_VENDOR);

  // Unsaved-changes guards: every Cancel/Close/backdrop path of the three form
  // modals is gated with confirmDiscard() so in-progress edits aren't silently
  // dropped; beforeunload is covered while dirty. The create modals reset to
  // their blank shape on every (confirmed) close, so their snapshot is the
  // blank constant itself.
  const isPODirty = showPOModal && JSON.stringify(newPO) !== JSON.stringify(BLANK_PO);
  const isVendorDirty = showVendorModal && JSON.stringify(newVendor) !== JSON.stringify(BLANK_VENDOR);
  const isEditVendorDirty =
    showEditVendorModal && JSON.stringify(editVendorForm) !== JSON.stringify(initialEditVendorForm);
  const { confirmDiscard: confirmDiscardPO } = useUnsavedChanges(isPODirty);
  const { confirmDiscard: confirmDiscardVendor } = useUnsavedChanges(isVendorDirty);
  const { confirmDiscard: confirmDiscardEditVendor } = useUnsavedChanges(isEditVendorDirty);

  // Cancel/Close gates. The successful submit paths close their modal directly
  // (never through these), so saving never prompts.
  const requestClosePOModal = () => {
    if (!confirmDiscardPO()) return;
    setShowPOModal(false);
    setNewPO(BLANK_PO);
  };

  const requestCloseVendorModal = () => {
    if (!confirmDiscardVendor()) return;
    setShowVendorModal(false);
    setNewVendor(BLANK_VENDOR);
  };

  const requestCloseEditVendorModal = () => {
    if (!confirmDiscardEditVendor()) return;
    setShowEditVendorModal(false);
  };

  const [newPart, setNewPart] = useState({
    part_number: '',
    name: '',
    description: '',
    part_type: 'purchased',
    unit_of_measure: 'EA',
    unit_cost: 0
  });

  const [vendorDocForm, setVendorDocForm] = useState({
    title: '',
    document_type: 'certificate',
    description: '',
    revision: 'A',
    file: null as File | null
  });

  // Once-per-id latch for the `?po=` deep-link fallback fetch below.
  const deepLinkedPoIdRef = useRef<number | null>(null);
  // Monotonic request id for the deleted-PO reads — see loadDeletedPOs.
  const deletedPOsRequestRef = useRef(0);

  useEffect(() => {
    loadData();
  }, []);

  useEffect(() => {
    const vendorId = Number(searchParams.get('vendor') || 0);
    const poId = Number(searchParams.get('po') || 0);

    if (vendorId && vendors.length > 0 && selectedVendor?.id !== vendorId) {
      const vendor = vendors.find(v => v.id === vendorId);
      if (vendor) {
        selectTab('vendors');
        openEditVendorModal(vendor);
      }
    }

    if (!poId) {
      // Allow a later click on the same deep link to re-attempt the fetch.
      deepLinkedPoIdRef.current = null;
    } else if (!loading && !loadError) {
      const po = purchaseOrders.find(order => order.id === poId);
      if (po) {
        // Land on the ACTIVE book: the term below names a live PO, and against the
        // Deleted view it would render an empty table reading as "the PO is gone".
        setActiveTab('orders');
        setPoView('active');
        setPoSearch(po.po_number);
      } else if (deepLinkedPoIdRef.current !== poId) {
        // The PO is not in the loaded list — list_purchase_orders excludes
        // CLOSED/CANCELLED — so re-fetch it by id and merge the row in rather
        // than silently doing nothing (a silent miss looks like success, which
        // is worse than a 404).
        //
        // The ref guard is NOT optional: `purchaseOrders` is in this effect's
        // dependency array and the fetch WRITES it, so without a once-per-id
        // latch the miss path is an infinite fetch loop.
        deepLinkedPoIdRef.current = poId;
        void loadDeepLinkedPurchaseOrder(poId);
      }
    }
  }, [searchParams, vendors, purchaseOrders, selectedVendor?.id, loading, loadError]);

  const loadData = async () => {
    setLoading(true);
    setLoadError(false);
    try {
      const [vendorsRes, posRes, partsRes] = await Promise.all([
        api.getVendors(),
        api.getPurchaseOrders(),
        api.getParts({ active_only: true, item_group: 'all' })
      ]);
      setVendors(vendorsRes);
      setPurchaseOrders(posRes);
      setParts(partsRes);
    } catch (err) {
      console.error('Failed to load purchasing data:', err);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  };

  /**
   * Load the deleted-PO book.
   *
   * `deleted_only=true` is the ONLY way any caller sees a soft-deleted PO — every
   * other read of this endpoint hard-filters `is_deleted == False` — so without this
   * fetch the Restore control below would have nothing to act on. Kept out of
   * `loadData` deliberately: it runs only when the user asks for the Deleted view, and
   * carries its own loading/error state so a failure here degrades that one table
   * instead of blanking the whole page.
   */
  const loadDeletedPOs = async () => {
    // Request-sequence latch. Entering the view fires a read, so Deleted → Active →
    // Deleted can leave two in flight; without this the SLOWER (older) response wins and
    // paints a stale archive — the exact condition the re-fetch-on-every-entry rule above
    // exists to prevent. Same shape as `deepLinkedPoIdRef` below.
    const requestId = deletedPOsRequestRef.current + 1;
    deletedPOsRequestRef.current = requestId;
    setDeletedPOsLoading(true);
    setDeletedPOsError(false);
    try {
      const rows = await api.getPurchaseOrders({ deleted_only: true });
      if (deletedPOsRequestRef.current !== requestId) return;
      setDeletedPOs(rows);
    } catch (err) {
      console.error('Failed to load deleted purchase orders:', err);
      if (deletedPOsRequestRef.current !== requestId) return;
      setDeletedPOsError(true);
    } finally {
      if (deletedPOsRequestRef.current === requestId) setDeletedPOsLoading(false);
    }
  };

  const showPOView = (view: POView) => {
    setPoView(view);
    // Fetch on every entry into the Deleted view, not just the first: somebody else may
    // have deleted or restored a PO since, and a stale archive is how you end up clicking
    // Restore on a row the server will refuse.
    if (view === 'deleted') void loadDeletedPOs();
  };

  /**
   * Leave the Orders tab. `poView` is state nothing else resets, and every entry into the
   * Deleted view is supposed to re-read it — so a tab round-trip (Orders → Vendors →
   * Orders) would otherwise re-open the archive on rows fetched minutes ago, with no
   * fetch, which is precisely what `showPOView` re-reads to avoid. The `?po=` deep link
   * has the same problem from the other side: it selects the Orders tab and sets a search
   * term for a LIVE PO, which against the Deleted view renders an empty table that reads
   * as "the PO you were sent is gone". Both are fixed by landing on the active book.
   */
  const selectTab = (tab: TabType) => {
    setActiveTab(tab);
    if (tab !== 'orders') setPoView('active');
  };

  /**
   * Undo a soft delete (compliance invariant 3 — the record was never destroyed, only
   * hidden). Restore is server-GATED, so this is NON-OPTIMISTIC: nothing moves in the UI
   * until the call resolves. On success both books are re-read, which is what takes the
   * row out of the Deleted view and puts it back in the active one; on failure the row
   * stays where it is and the server's `detail` is surfaced verbatim.
   */
  const handleRestorePO = async (po: PurchaseOrder) => {
    if (restorePOPendingId !== null) return;
    setRestorePOPendingId(po.id);
    try {
      await api.restorePurchaseOrder(po.id);
      // The restore SUCCEEDED — but succeeding is not the same as being findable, and a
      // plain success toast here would send someone hunting for a record that is on no
      // list they can open. The active book is `getPurchaseOrders()` with no status, and
      // that endpoint excludes CLOSED and CANCELLED by default, so a restored PO in either
      // status leaves the Deleted view without appearing in the Active one. That is not an
      // edge case: the deleted view's whole status carve-out exists because a
      // cancelled-then-deleted PO is the likeliest thing anyone wants back. `warning` is
      // the house variant for "it worked, but not everything you expected" — success would
      // hide the shortfall, error would claim a failure that did not happen.
      const offActiveList = po.status === 'closed' || po.status === 'cancelled';
      if (offActiveList) {
        showToast(
          'warning',
          `Purchase order ${po.po_number} restored. It is ${po.status}, so it stays off the ` +
            'active list until its status changes.',
        );
      } else {
        showToast('success', `Purchase order ${po.po_number} restored`);
      }
      await Promise.all([loadData(), loadDeletedPOs()]);
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to restore purchase order');
      // A 4xx means the SERVER disagrees with what this table is showing — overwhelmingly
      // "Purchase order is not deleted", i.e. somebody else restored it while this archive
      // sat open. Re-read so the phantom row goes away; otherwise every further click
      // reproduces the same refusal until the user leaves and re-enters the view. Still
      // non-optimistic: the row moves only because the server's next answer moved it.
      // Deliberately NOT on 5xx/offline — that tells us nothing about the row, and a
      // failing re-read would swap the archive for an error state over a transient blip.
      const statusCode = err.response?.status;
      if (typeof statusCode === 'number' && statusCode >= 400 && statusCode < 500) {
        await loadDeletedPOs();
      }
    } finally {
      setRestorePOPendingId(null);
    }
  };

  /**
   * `?po=<id>` deep-link fallback: fetch the PO by id and fold it into the
   * list so the search filter can actually match it. `GET /purchasing/purchase-orders/{id}`
   * returns POResponse (nested `vendor`, full `lines`) rather than the list's
   * flat summary shape, hence the explicit mapping.
   */
  const loadDeepLinkedPurchaseOrder = async (poId: number) => {
    try {
      const detail = await api.getPurchaseOrder(poId);
      const summary: PurchaseOrder = {
        id: detail.id,
        po_number: detail.po_number,
        vendor_id: detail.vendor_id,
        vendor_name: detail.vendor?.name,
        status: detail.status,
        order_date: detail.order_date,
        required_date: detail.required_date,
        total: detail.total,
        line_count: Array.isArray(detail.lines) ? detail.lines.length : 0,
      };
      setPurchaseOrders(prev => (prev.some(p => p.id === summary.id) ? prev : [summary, ...prev]));
      setActiveTab('orders');
      setPoView('active');
      setPoSearch(summary.po_number);
    } catch (err) {
      console.error('Failed to load deep-linked purchase order:', err);
      showToast('error', 'Purchase order not found');
    }
  };

  const loadVendorDocuments = async (vendorId: number) => {
    setVendorDocsLoading(true);
    setVendorDocsError(false);
    try {
      const [docsRes, typesRes] = await Promise.all([
        api.getDocuments({ vendor_id: vendorId }),
        api.getDocumentTypes()
      ]);
      setVendorDocuments(docsRes);
      setDocumentTypes(typesRes);
    } catch (err) {
      console.error('Failed to load vendor documents:', err);
      setVendorDocsError(true);
    } finally {
      setVendorDocsLoading(false);
    }
  };

  const openEditVendorModal = (vendor: Vendor) => {
    setSelectedVendor(vendor);
    const nextForm = {
      code: vendor.code || '',
      name: vendor.name || '',
      contact_name: vendor.contact_name || '',
      email: vendor.email || '',
      phone: vendor.phone || '',
      address_line1: vendor.address_line1 || '',
      address_line2: vendor.address_line2 || '',
      city: vendor.city || '',
      state: vendor.state || '',
      postal_code: vendor.postal_code || '',
      country: vendor.country || 'US',
      payment_terms: vendor.payment_terms || '',
      is_approved: vendor.is_approved,
      is_as9100_certified: vendor.is_as9100_certified,
      is_iso9001_certified: vendor.is_iso9001_certified,
      is_active: vendor.is_active ?? true,
      notes: vendor.notes || ''
    };
    setEditVendorForm(nextForm);
    setInitialEditVendorForm(nextForm);
    setVendorDocForm({
      title: '',
      document_type: 'certificate',
      description: '',
      revision: 'A',
      file: null
    });
    setShowEditVendorModal(true);
    loadVendorDocuments(vendor.id);
  };

  const handleUpdateVendor = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedVendor) return;
    try {
      const cleanedForm = {
        ...editVendorForm,
        code: editVendorForm.code.trim() || undefined,
        contact_name: editVendorForm.contact_name.trim() || undefined,
        email: editVendorForm.email.trim() || undefined,
        phone: editVendorForm.phone.trim() || undefined,
        address_line1: editVendorForm.address_line1.trim() || undefined,
        address_line2: editVendorForm.address_line2.trim() || undefined,
        city: editVendorForm.city.trim() || undefined,
        state: editVendorForm.state.trim().toUpperCase() || undefined,
        postal_code: editVendorForm.postal_code.trim() || undefined,
        country: editVendorForm.country.trim().toUpperCase() || undefined,
        payment_terms: editVendorForm.payment_terms.trim() || undefined,
        notes: editVendorForm.notes.trim() || undefined,
      };
      await api.updateVendor(selectedVendor.id, {
        version: selectedVendor.version ?? 0,
        ...cleanedForm
      });
      setShowEditVendorModal(false);
      setSelectedVendor(null);
      showToast('success', 'Vendor updated');
      loadData();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to update vendor');
    }
  };

  const handleVendorDocUpload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedVendor || !vendorDocForm.file) {
      showToast('error', 'Please select a file');
      return;
    }
    const formData = new FormData();
    formData.append('file', vendorDocForm.file);
    formData.append('title', vendorDocForm.title || vendorDocForm.file.name);
    formData.append('document_type', vendorDocForm.document_type);
    formData.append('description', vendorDocForm.description || '');
    formData.append('revision', vendorDocForm.revision || 'A');
    formData.append('vendor_id', selectedVendor.id.toString());

    try {
      await api.uploadDocument(formData);
      setVendorDocForm({
        title: '',
        document_type: vendorDocForm.document_type,
        description: '',
        revision: 'A',
        file: null
      });
      showToast('success', 'Document uploaded');
      loadVendorDocuments(selectedVendor.id);
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to upload document');
    }
  };

  const handleVendorDocDownload = async (doc: VendorDocument) => {
    try {
      const response = await api.downloadDocument(doc.id);
      const url = window.URL.createObjectURL(new Blob([response]));
      const link = document.createElement('a');
      link.href = url;
      link.setAttribute('download', doc.file_name || 'document');
      document.body.appendChild(link);
      link.click();
      link.remove();
    } catch {
      showToast('error', 'Failed to download document');
    }
  };

  const handleVendorDocDelete = (doc: VendorDocument) => {
    setDeleteVendorDocTarget(doc);
  };

  const handleConfirmDeleteVendorDoc = async () => {
    if (!deleteVendorDocTarget || deleteVendorDocPending) return;
    setDeleteVendorDocPending(true);
    try {
      await api.deleteDocument(deleteVendorDocTarget.id);
      showToast('success', 'Document deleted');
      if (selectedVendor) {
        loadVendorDocuments(selectedVendor.id);
      }
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to delete document');
    } finally {
      setDeleteVendorDocPending(false);
      setDeleteVendorDocTarget(null);
    }
  };

  const formatFileSize = (bytes?: number) => {
    if (!bytes) return '-';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const handleCreatePO = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newPO.vendor_id || newPO.vendor_id <= 0) {
      showToast('error', 'Please select a vendor');
      return;
    }
    if (newPO.lines.length === 0) {
      showToast('error', 'Please add at least one line item');
      return;
    }
    for (let i = 0; i < newPO.lines.length; i++) {
      const line = newPO.lines[i];
      if (!line.part_id || line.part_id <= 0) {
        showToast('error', `Line ${i + 1}: please select a part`);
        return;
      }
      if (!Number.isFinite(line.quantity_ordered) || line.quantity_ordered <= 0) {
        showToast('error', `Line ${i + 1}: quantity must be a number greater than 0`);
        return;
      }
      if (!Number.isFinite(line.unit_price) || line.unit_price < 0) {
        showToast('error', `Line ${i + 1}: unit price must be a number of 0 or more`);
        return;
      }
    }
    try {
      // An empty-string date 422s server-side — omit it when blank.
      await api.createPurchaseOrder({
        ...newPO,
        required_date: newPO.required_date || undefined,
      });
      setShowPOModal(false);
      setNewPO(BLANK_PO);
      showToast('success', 'Purchase order created');
      loadData();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to create PO');
    }
  };

  const handleSendPO = (po: PurchaseOrder) => {
    setSendPOTarget(po);
  };

  const handleConfirmSendPO = async () => {
    if (!sendPOTarget || sendPOPending) return;
    setSendPOPending(true);
    try {
      await api.sendPurchaseOrder(sendPOTarget.id);
      showToast('success', 'Purchase order sent');
      loadData();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to send PO');
    } finally {
      setSendPOPending(false);
      setSendPOTarget(null);
    }
  };

  const handlePrintPO = (poId: number) => {
    window.open(`/print/purchase-order/${poId}?autoprint=1`, '_blank');
  };

  // ConfirmDialog.onConfirm is synchronous, so it fires this async handler and
  // returns. The dialog closes only on success; a server refusal keeps the row
  // untouched and surfaces the verbatim detail as an error toast.
  const handleConfirmDeletePO = async () => {
    if (!deletePOTarget || deletePOPending) return;
    setDeletePOPending(true);
    try {
      await api.deletePurchaseOrder(deletePOTarget.id);
      showToast('success', `Purchase order ${deletePOTarget.po_number} deleted`);
      setDeletePOTarget(null);
      loadData();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to delete purchase order');
    } finally {
      setDeletePOPending(false);
    }
  };

  const handleConfirmDeleteVendor = async () => {
    if (!deleteVendorTarget || deleteVendorPending) return;
    setDeleteVendorPending(true);
    try {
      await api.deleteVendor(deleteVendorTarget.id);
      showToast('success', `Vendor ${deleteVendorTarget.name} deleted`);
      setDeleteVendorTarget(null);
      loadData();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to delete vendor');
    } finally {
      setDeleteVendorPending(false);
    }
  };

  const handleCreateVendor = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await api.createVendor(newVendor);
      setShowVendorModal(false);
      setNewVendor(BLANK_VENDOR);
      showToast('success', 'Vendor created');
      loadData();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to create vendor');
    }
  };

  const openAddPartModal = (lineIndex: number) => {
    setAddPartForLineIndex(lineIndex);
    setNewPart({
      part_number: '',
      name: '',
      description: '',
      part_type: 'purchased',
      unit_of_measure: 'EA',
      unit_cost: 0
    });
    setShowAddPartModal(true);
  };

  const handleCreatePart = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const createdPart = await api.createPart(newPart);
      // Add to local parts list
      setParts([...parts, createdPart]);
      // Update the PO line with the new part
      if (addPartForLineIndex !== null) {
        const lines = [...newPO.lines];
        lines[addPartForLineIndex] = { 
          ...lines[addPartForLineIndex], 
          part_id: createdPart.id,
          unit_price: newPart.unit_cost
        };
        setNewPO({ ...newPO, lines });
      }
      setShowAddPartModal(false);
      showToast('success', 'Part created');
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to create part');
    }
  };

  const addPOLine = () => {
    setNewPO({
      ...newPO,
      lines: [...newPO.lines, { part_id: 0, quantity_ordered: 1, unit_price: 0 }]
    });
  };

  const updatePOLine = (index: number, field: string, value: any) => {
    const lines = [...newPO.lines];
    lines[index] = { ...lines[index], [field]: value };
    setNewPO({ ...newPO, lines });
  };

  const removePOLine = (index: number) => {
    setNewPO({ ...newPO, lines: newPO.lines.filter((_, i) => i !== index) });
  };

  // One search predicate, both books — the search box stays useful in the Deleted
  // view, which is where you arrive knowing the PO number you want back.
  const matchesPoSearch = (po: PurchaseOrder) => {
    const term = debouncedPoSearch.trim().toLowerCase();
    if (!term) return true;
    return (
      po.po_number.toLowerCase().includes(term) ||
      (po.vendor_name || '').toLowerCase().includes(term)
    );
  };

  const filteredPOs = purchaseOrders.filter(matchesPoSearch);
  const filteredDeletedPOs = deletedPOs.filter(matchesPoSearch);

  // Columns shared by both books. The two views then append DIFFERENT tails: the active
  // one gets Print / Send / Delete, the deleted one gets the delete provenance and a
  // lone Restore. Deliberately no Print and no Send on a deleted PO — a deleted order is
  // a record, and mailing one to a vendor is the worst thing this page could do.
  const poBaseColumns: Array<DataTableColumn<PurchaseOrder>> = [
    {
      key: 'po_number',
      header: 'PO #',
      sortable: true,
      accessor: (po) => po.po_number,
      render: (po) => <span className="font-medium text-werco-primary">{po.po_number}</span>,
    },
    {
      key: 'vendor_name',
      header: 'Vendor',
      sortable: true,
      accessor: (po) => po.vendor_name ?? '',
    },
    {
      key: 'status',
      header: 'Status',
      sortable: true,
      accessor: (po) => po.status,
      render: (po) => <StatusBadge status={po.status} />,
      csv: (po) => po.status.replace('_', ' '),
    },
    {
      key: 'order_date',
      header: 'Order Date',
      sortable: true,
      accessor: (po) => po.order_date ?? '',
      render: (po) => (po.order_date ? formatCentralDate(po.order_date) : '-'),
      csv: (po) => (po.order_date ? formatCentralDate(po.order_date) : ''),
    },
    {
      key: 'required_date',
      header: 'Due Date',
      sortable: true,
      accessor: (po) => po.required_date ?? '',
      render: (po) => (po.required_date ? formatCentralDate(po.required_date) : '-'),
      csv: (po) => (po.required_date ? formatCentralDate(po.required_date) : ''),
    },
    {
      key: 'total',
      header: 'Total',
      sortable: true,
      align: 'right',
      accessor: (po) => Number(po.total || 0),
      render: (po) => <span className="font-medium">${Number(po.total || 0).toFixed(2)}</span>,
      csv: (po) => Number(po.total || 0).toFixed(2),
    },
    {
      key: 'line_count',
      header: 'Lines',
      sortable: true,
      align: 'center',
      accessor: (po) => po.line_count,
    },
  ];

  const poColumns: Array<DataTableColumn<PurchaseOrder>> = [
    ...poBaseColumns,
    {
      key: 'actions',
      header: 'Actions',
      align: 'center',
      render: (po) => (
        <div className="flex items-center justify-center gap-3" role="presentation" onClick={(e) => e.stopPropagation()}>
          <button
            onClick={() => handlePrintPO(po.id)}
            className="text-surface-600 hover:text-werco-primary text-sm"
          >
            Print
          </button>
          {canSendPO && po.status === 'draft' && (
            <button
              onClick={() => handleSendPO(po)}
              className="text-werco-primary hover:underline text-sm"
            >
              Send
            </button>
          )}
          {canDeletePurchasing && (
            <button
              onClick={() => setDeletePOTarget(po)}
              className="text-red-400 hover:text-red-300 text-sm"
            >
              Delete
            </button>
          )}
        </div>
      ),
    },
  ];

  // `deleted_at` is UTC over the wire and renders in shop-local Central, like every
  // other timestamp in the app. `deleted_by_name` comes back null when that user row is
  // gone; "Unknown" is honest there, and the audit log still holds the actor either way.
  const deletedPOColumns: Array<DataTableColumn<PurchaseOrder>> = [
    ...poBaseColumns,
    {
      key: 'deleted_at',
      header: 'Deleted',
      sortable: true,
      accessor: (po) => po.deleted_at ?? '',
      render: (po) => (po.deleted_at ? formatCentralDateTime(po.deleted_at) : '-'),
      csv: (po) => (po.deleted_at ? formatCentralDateTime(po.deleted_at) : ''),
    },
    {
      key: 'deleted_by_name',
      header: 'Deleted By',
      sortable: true,
      accessor: (po) => po.deleted_by_name ?? '',
      render: (po) => po.deleted_by_name || <span className="text-slate-500">Unknown</span>,
      csv: (po) => po.deleted_by_name ?? '',
    },
    {
      key: 'actions',
      header: 'Actions',
      align: 'center',
      render: (po) =>
        canRestorePO ? (
          // No confirm dialog on purpose: restore is non-destructive and one click from
          // being undone by the Delete control this row will have again the moment it is
          // back. A confirm here would be reflex-training, not a safeguard.
          <div role="presentation" onClick={(e) => e.stopPropagation()}>
            <LoadingButton
              variant="secondary"
              size="sm"
              loading={restorePOPendingId === po.id}
              loadingText="Restoring…"
              disabled={restorePOPendingId !== null}
              onClick={() => handleRestorePO(po)}
              aria-label={`Restore purchase order ${po.po_number}`}
            >
              Restore
            </LoadingButton>
          </div>
        ) : (
          <span className="text-sm text-slate-500">—</span>
        ),
    },
  ];

  const renderPOCard = (po: PurchaseOrder) => (
    <MobileDataCard
      title={po.po_number}
      subtitle={po.vendor_name || undefined}
      onClick={() => handlePrintPO(po.id)}
      badge={<StatusBadge status={po.status} />}
      fields={[
        { label: 'Order Date', value: po.order_date ? formatCentralDate(po.order_date) : '-' },
        { label: 'Due Date', value: po.required_date ? formatCentralDate(po.required_date) : '-' },
        {
          label: 'Total',
          value: <span className="tabular-nums">${Number(po.total || 0).toFixed(2)}</span>,
        },
        { label: 'Lines', value: <span className="tabular-nums">{po.line_count}</span> },
      ]}
      actions={
        <div className="flex flex-wrap gap-3 justify-end" role="presentation" onClick={(e) => e.stopPropagation()}>
          <button
            onClick={() => handlePrintPO(po.id)}
            className="text-surface-600 hover:text-werco-primary text-sm"
          >
            Print
          </button>
          {canSendPO && po.status === 'draft' && (
            <button
              onClick={() => handleSendPO(po)}
              className="text-werco-primary hover:underline text-sm"
            >
              Send
            </button>
          )}
          {canDeletePurchasing && (
            <button
              onClick={() => setDeletePOTarget(po)}
              className="text-red-400 hover:text-red-300 text-sm"
            >
              Delete
            </button>
          )}
        </div>
      }
    />
  );

  // Mobile twin of the deleted row. No onClick (a deleted PO is not selectable work)
  // and no Print/Send/Delete — the only affordance is Restore, same as the table.
  const renderDeletedPOCard = (po: PurchaseOrder) => (
    <MobileDataCard
      title={po.po_number}
      subtitle={po.vendor_name || undefined}
      badge={<StatusBadge status={po.status} />}
      fields={[
        { label: 'Deleted', value: po.deleted_at ? formatCentralDateTime(po.deleted_at) : '-' },
        { label: 'Deleted By', value: po.deleted_by_name || 'Unknown' },
        {
          label: 'Total',
          value: <span className="tabular-nums">${Number(po.total || 0).toFixed(2)}</span>,
        },
        { label: 'Lines', value: <span className="tabular-nums">{po.line_count}</span> },
      ]}
      actions={
        canRestorePO ? (
          <div className="flex justify-end" role="presentation" onClick={(e) => e.stopPropagation()}>
            <LoadingButton
              variant="secondary"
              size="sm"
              loading={restorePOPendingId === po.id}
              loadingText="Restoring…"
              disabled={restorePOPendingId !== null}
              onClick={() => handleRestorePO(po)}
              aria-label={`Restore purchase order ${po.po_number}`}
            >
              Restore
            </LoadingButton>
          </div>
        ) : undefined
      }
    />
  );

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-werco-primary"></div>
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-bold text-white">Purchasing &amp; Receiving</h1>
        <div className="card">
          <ErrorState
            message="Could not load purchasing data."
            onRetry={loadData}
          />
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-white">Purchasing & Receiving</h1>
        <div className="flex gap-2">
          {canCreateVendor && (
            <Button variant="secondary" onClick={() => setShowVendorModal(true)} className="flex items-center">
              <BuildingOfficeIcon className="h-5 w-5 mr-2" />
              New Vendor
            </Button>
          )}
          {canCreatePO && (
            <Button onClick={() => setShowPOModal(true)} className="flex items-center">
              <PlusIcon className="h-5 w-5 mr-2" />
              New PO
            </Button>
          )}
        </div>
      </div>

      {/* KPI strip */}
      <MiniStatStrip className="grid grid-cols-2 gap-2">
        <MiniStat
          icon={ClipboardDocumentListIcon}
          iconBg="bg-werco-navy-500/15"
          iconColor="text-werco-navy-400"
          label="Open POs"
          value={purchaseOrders.length}
        />
        <MiniStat
          icon={CheckCircleIcon}
          iconBg="bg-fd-green/15"
          iconColor="text-fd-green"
          label="Approved Vendors"
          value={vendors.filter(v => v.is_approved).length}
          valueColor="text-fd-green"
        />
      </MiniStatStrip>

      {/* Tabs */}
      <div className="border-b border-slate-700">
        <nav className="flex space-x-8">
          {[
            { id: 'orders', label: 'Purchase Orders', count: purchaseOrders.length },
            { id: 'vendors', label: 'Vendors', count: vendors.length }
          ].map(tab => (
            <button
              key={tab.id}
              onClick={() => selectTab(tab.id as TabType)}
              className={`py-2 px-1 border-b-2 font-medium text-sm ${
                activeTab === tab.id
                  ? 'border-werco-primary text-werco-primary'
                  : 'border-transparent text-slate-400 hover:text-slate-300'
              }`}
            >
              {tab.label}
              {tab.count > 0 && (
                <span className={`ml-2 px-2 py-0.5 rounded-full text-xs ${
                  activeTab === tab.id ? 'bg-werco-primary text-white' : 'bg-slate-800/50'
                }`}>
                  {tab.count}
                </span>
              )}
            </button>
          ))}
        </nav>
      </div>

      {/* Purchase Orders Tab */}
      {activeTab === 'orders' && (
        <div className="card">
          <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-4 mb-4">
            <h2 className="text-lg font-semibold">
              {poView === 'deleted' ? 'Deleted Purchase Orders' : 'Purchase Orders'}
            </h2>
            <div className="flex flex-col sm:flex-row sm:items-center gap-3">
              {/* Active / Deleted is a SEGMENTED control, not a "show deleted" checkbox,
                  because the two sets must never share a list: a deleted PO is a record
                  and a live one is workable, and a merged table is one misread row away
                  from someone printing or receiving against an order that no longer
                  exists. Mutually exclusive by construction. */}
              <div
                className="inline-flex border border-slate-700 rounded overflow-hidden self-start"
                role="group"
                aria-label="Purchase order view"
              >
                {([
                  { id: 'active' as POView, label: 'Active' },
                  { id: 'deleted' as POView, label: 'Deleted' },
                ]).map((view) => (
                  <button
                    key={view.id}
                    type="button"
                    onClick={() => showPOView(view.id)}
                    aria-pressed={poView === view.id}
                    className={`px-3 py-1.5 text-sm font-medium ${
                      poView === view.id
                        ? 'bg-werco-primary text-white'
                        : 'bg-transparent text-slate-400 hover:text-slate-200 hover:bg-slate-800'
                    }`}
                  >
                    {view.label}
                  </button>
                ))}
              </div>
              <input
                type="text"
                value={poSearch}
                onChange={(e) => setPoSearch(e.target.value)}
                className="input max-w-sm"
                placeholder="Search by PO # or vendor..."
                aria-label="Search purchase orders"
              />
            </div>
          </div>

          {poView === 'deleted' ? (
            <>
              <div className="flex items-start gap-2 mb-4 px-3 py-2 rounded border border-amber-500/40 bg-amber-500/10 text-sm text-amber-200">
                <TrashIcon className="h-5 w-5 flex-shrink-0" aria-hidden="true" />
                <p>
                  These purchase orders are deleted records. They are off the receiving list and
                  nothing can be received against them.
                  {canRestorePO
                    ? ' Restore one to put it back in the active book.'
                    : ' An admin or manager can restore one.'}
                </p>
              </div>
              <DataTable
                columns={deletedPOColumns}
                data={filteredDeletedPOs}
                rowKey={(po) => po.id}
                // No onRowClick: the active table's row click PRINTS the PO, and a deleted
                // order is not a document anyone should be handing to a vendor.
                rowClassName={() => 'opacity-70'}
                loading={deletedPOsLoading}
                error={deletedPOsError}
                onRetry={loadDeletedPOs}
                defaultSort={{ key: 'deleted_at', dir: 'desc' }}
                pageSize={25}
                csvExport={{ filename: 'deleted-purchase-orders' }}
                mobileCards={renderDeletedPOCard}
                empty={{
                  icon: TrashIcon,
                  title: debouncedPoSearch.trim()
                    ? 'No matching deleted purchase orders'
                    : 'No deleted purchase orders',
                  description: debouncedPoSearch.trim()
                    ? 'No deleted purchase orders match your search. Adjust the term above.'
                    : 'Purchase orders deleted from Purchasing or Receiving appear here so they can be restored.',
                }}
              />
            </>
          ) : (
            <DataTable
              columns={poColumns}
              data={filteredPOs}
              rowKey={(po) => po.id}
              onRowClick={(po) => handlePrintPO(po.id)}
              defaultSort={{ key: 'po_number', dir: 'asc' }}
              pageSize={25}
              csvExport={{ filename: 'purchase-orders' }}
              mobileCards={renderPOCard}
              empty={{
                icon: ClipboardDocumentListIcon,
                title: debouncedPoSearch.trim() ? 'No matching purchase orders' : 'No purchase orders',
                description: debouncedPoSearch.trim()
                  ? 'No purchase orders match your search. Adjust the term above.'
                  : 'Purchase orders you create will appear here.',
                action:
                  debouncedPoSearch.trim() || !canCreatePO
                    ? undefined
                    : { label: 'New PO', onClick: () => setShowPOModal(true) },
              }}
            />
          )}
        </div>
      )}

      {/* Vendors Tab */}
      {activeTab === 'vendors' && (
        <div className="card">
          <h2 className="text-lg font-semibold mb-4">Vendors</h2>
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-700">
              <thead className="bg-slate-800">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Code</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Name</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Contact</th>
                  <th className="px-4 py-3 text-center text-xs font-medium text-slate-400 uppercase">Approved</th>
                  <th className="px-4 py-3 text-center text-xs font-medium text-slate-400 uppercase">AS9100</th>
                  <th className="px-4 py-3 text-center text-xs font-medium text-slate-400 uppercase">ISO9001</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Payment Terms</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase">Actions</th>
                </tr>
              </thead>
              <tbody className="bg-fd-panel divide-y divide-slate-700">
                {vendors.map((vendor) => (
                  <tr key={vendor.id} className="hover:bg-slate-800">
                    <td className="px-4 py-3 font-mono">{vendor.code}</td>
                    <td className="px-4 py-3 font-medium">{vendor.name}</td>
                    <td className="px-4 py-3">
                      <div>{vendor.contact_name}</div>
                      <div className="text-sm text-slate-400">{vendor.email}</div>
                    </td>
                    <td className="px-4 py-3 text-center">
                      {vendor.is_approved ? (
                        <CheckCircleIcon className="h-5 w-5 text-green-500 mx-auto" />
                      ) : (
                        <span className="text-slate-400">-</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-center">
                      {vendor.is_as9100_certified ? (
                        <CheckCircleIcon className="h-5 w-5 text-blue-500 mx-auto" />
                      ) : (
                        <span className="text-slate-400">-</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-center">
                      {vendor.is_iso9001_certified ? (
                        <CheckCircleIcon className="h-5 w-5 text-blue-500 mx-auto" />
                      ) : (
                        <span className="text-slate-400">-</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-sm">{vendor.payment_terms || '-'}</td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-3">
                        <button
                          onClick={() => openEditVendorModal(vendor)}
                          className="text-werco-primary hover:underline text-sm"
                        >
                          Edit
                        </button>
                        {canDeletePurchasing && (
                          <button
                            onClick={() => setDeleteVendorTarget(vendor)}
                            className="text-red-400 hover:text-red-300 text-sm"
                          >
                            Delete
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Create PO Modal */}
      <Modal open={showPOModal} onClose={requestClosePOModal} size="2xl" closeOnBackdrop={false}>
            <h3 className="text-lg font-semibold mb-4">Create Purchase Order</h3>
            <form onSubmit={handleCreatePO} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <FormField label="Vendor" required>
                  {(field) => (
                  <select
                    {...field}
                    value={newPO.vendor_id}
                    onChange={(e) => setNewPO({ ...newPO, vendor_id: parseInt(e.target.value) })}
                    className="input"
                    required
                  >
                    <option value={0}>Select vendor...</option>
                    {vendors.filter(v => v.is_approved).map(v => (
                      <option key={v.id} value={v.id}>{v.code} - {v.name}</option>
                    ))}
                  </select>
                  )}
                </FormField>
                <FormField label="Required Date">
                  {(field) => (
                  <input
                    {...field}
                    type="date"
                    value={newPO.required_date}
                    onChange={(e) => setNewPO({ ...newPO, required_date: e.target.value })}
                    className="input"
                  />
                  )}
                </FormField>
              </div>

              <div>
                <div className="flex justify-between items-center mb-2">
                  <span className="label">Line Items</span>
                  <button type="button" onClick={addPOLine} className="text-werco-primary text-sm hover:underline">
                    + Add Line
                  </button>
                </div>
                {newPO.lines.length > 0 && (
                  <div className="flex gap-2 mb-1 text-xs text-slate-400 font-medium">
                    <div className="flex-1">Part</div>
                    <div className="w-24">Quantity</div>
                    <div className="w-28">Unit Price ($)</div>
                    <div className="w-6"></div>
                  </div>
                )}
                {newPO.lines.map((line, idx) => (
                  <div key={idx} className="flex gap-2 mb-2 items-start">
                    <div className="flex-1">
                      <select
                        value={line.part_id}
                        onChange={(e) => updatePOLine(idx, 'part_id', parseInt(e.target.value))}
                        className="input text-sm"
                        required
                      >
                        <option value={0}>Select part...</option>
                        {parts.map(p => (
                          <option key={p.id} value={p.id}>{p.part_number} - {p.name}</option>
                        ))}
                      </select>
                      <button
                        type="button"
                        onClick={() => openAddPartModal(idx)}
                        className="text-werco-primary text-xs hover:underline mt-1"
                      >
                        + New Part
                      </button>
                    </div>
                    <div className="w-24">
                      <input
                        type="number"
                        value={line.quantity_ordered}
                        onChange={(e) => updatePOLine(idx, 'quantity_ordered', parseFloat(e.target.value) || 0)}
                        className="input text-sm"
                        min={1}
                        required
                        aria-label="Quantity ordered"
                      />
                    </div>
                    <div className="w-28">
                      <input
                        type="number"
                        value={line.unit_price}
                        onChange={(e) => updatePOLine(idx, 'unit_price', parseFloat(e.target.value) || 0)}
                        className="input text-sm"
                        step={0.01}
                        min={0}
                        required
                        aria-label="Unit price"
                      />
                    </div>
                    <button type="button" onClick={() => removePOLine(idx)} className="text-red-500 hover:text-red-400 mt-2">
                      &times;
                    </button>
                  </div>
                ))}
                {newPO.lines.length === 0 && (
                  <p className="text-slate-400 text-sm">Click "+ Add Line" to add items</p>
                )}
              </div>

              <FormField label="Notes">
                {(field) => (
                <textarea
                  {...field}
                  value={newPO.notes}
                  onChange={(e) => setNewPO({ ...newPO, notes: e.target.value })}
                  className="input"
                  rows={2}
                />
                )}
              </FormField>

              <div className="flex justify-end gap-3 pt-4 border-t">
                <Button variant="secondary" onClick={requestClosePOModal}>Cancel</Button>
                <Button type="submit">Create PO</Button>
              </div>
            </form>
      </Modal>

      {/* Create Vendor Modal */}
      <Modal open={showVendorModal} onClose={requestCloseVendorModal} size="md" closeOnBackdrop={false}>
            <h3 className="text-lg font-semibold mb-4">Create Vendor</h3>
            <form onSubmit={handleCreateVendor} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <FormField label="Code" required>
                  {(field) => (
                  <input
                    {...field}
                    type="text"
                    value={newVendor.code}
                    onChange={(e) => setNewVendor({ ...newVendor, code: e.target.value })}
                    className="input"
                    placeholder="VND-001"
                    required
                  />
                  )}
                </FormField>
                <FormField label="Payment Terms">
                  {(field) => (
                  <input
                    {...field}
                    type="text"
                    value={newVendor.payment_terms}
                    onChange={(e) => setNewVendor({ ...newVendor, payment_terms: e.target.value })}
                    className="input"
                    placeholder="e.g., NET 30"
                  />
                  )}
                </FormField>
              </div>
              <FormField label="Name" required>
                {(field) => (
                <input
                  {...field}
                  type="text"
                  value={newVendor.name}
                  onChange={(e) => setNewVendor({ ...newVendor, name: e.target.value })}
                  className="input"
                  required
                />
                )}
              </FormField>
              <FormField label="Contact Name">
                {(field) => (
                <input
                  {...field}
                  type="text"
                  value={newVendor.contact_name}
                  onChange={(e) => setNewVendor({ ...newVendor, contact_name: e.target.value })}
                  className="input"
                />
                )}
              </FormField>
              <div className="grid grid-cols-2 gap-4">
                <FormField label="Email">
                  {(field) => (
                  <input
                    {...field}
                    type="email"
                    value={newVendor.email}
                    onChange={(e) => setNewVendor({ ...newVendor, email: e.target.value })}
                    className="input"
                  />
                  )}
                </FormField>
                <FormField label="Phone">
                  {(field) => (
                  <input
                    {...field}
                    type="text"
                    value={newVendor.phone}
                    onChange={(e) => setNewVendor({ ...newVendor, phone: e.target.value })}
                    className="input"
                  />
                  )}
                </FormField>
              </div>
              <div>
                <label className="flex items-center">
                  <input
                    type="checkbox"
                    checked={newVendor.is_approved}
                    onChange={(e) => setNewVendor({ ...newVendor, is_approved: e.target.checked })}
                    className="mr-2"
                    aria-label="Approved Vendor"
                  />
                  <span>Approved Vendor</span>
                </label>
              </div>
              <div className="flex justify-end gap-3 pt-4 border-t">
                <Button variant="secondary" onClick={requestCloseVendorModal}>Cancel</Button>
                <Button type="submit">Create Vendor</Button>
              </div>
            </form>
      </Modal>

      {/* Edit Vendor Modal */}
      <Modal
        open={showEditVendorModal && !!selectedVendor}
        onClose={requestCloseEditVendorModal}
        size="5xl"
        closeOnBackdrop={false}
      >
        {selectedVendor && (
          <>
            <div className="flex items-center justify-between mb-4">
              <div>
                <h3 className="text-lg font-semibold">Edit Vendor</h3>
                <p className="text-sm text-slate-400">{selectedVendor.code}</p>
              </div>
              <button
                onClick={requestCloseEditVendorModal}
                className="text-slate-400 hover:text-slate-300"
                aria-label="Close"
              >
                <span className="text-xl">×</span>
              </button>
            </div>

            <form onSubmit={handleUpdateVendor} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <FormField label="Vendor Code" required>
                  {(field) => (
                  <input
                    {...field}
                    type="text"
                    value={editVendorForm.code}
                    onChange={(e) => setEditVendorForm({ ...editVendorForm, code: e.target.value })}
                    className="input"
                    placeholder="VND-001"
                    required
                  />
                  )}
                </FormField>
                <FormField label="Vendor Name" required>
                  {(field) => (
                  <input
                    {...field}
                    type="text"
                    value={editVendorForm.name}
                    onChange={(e) => setEditVendorForm({ ...editVendorForm, name: e.target.value })}
                    className="input"
                    required
                  />
                  )}
                </FormField>
                <FormField label="Payment Terms">
                  {(field) => (
                  <input
                    {...field}
                    type="text"
                    value={editVendorForm.payment_terms}
                    onChange={(e) => setEditVendorForm({ ...editVendorForm, payment_terms: e.target.value })}
                    className="input"
                    placeholder="e.g., NET 30"
                  />
                  )}
                </FormField>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <FormField label="Contact Name">
                  {(field) => (
                  <input
                    {...field}
                    type="text"
                    value={editVendorForm.contact_name}
                    onChange={(e) => setEditVendorForm({ ...editVendorForm, contact_name: e.target.value })}
                    className="input"
                  />
                  )}
                </FormField>
                <FormField label="Email">
                  {(field) => (
                  <input
                    {...field}
                    type="email"
                    value={editVendorForm.email}
                    onChange={(e) => setEditVendorForm({ ...editVendorForm, email: e.target.value })}
                    className="input"
                  />
                  )}
                </FormField>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <FormField label="Phone">
                  {(field) => (
                  <input
                    {...field}
                    type="text"
                    value={editVendorForm.phone}
                    onChange={(e) => setEditVendorForm({ ...editVendorForm, phone: e.target.value })}
                    className="input"
                  />
                  )}
                </FormField>
                <FormField label="Country">
                  {(field) => (
                  <input
                    {...field}
                    type="text"
                    value={editVendorForm.country}
                    onChange={(e) => setEditVendorForm({ ...editVendorForm, country: e.target.value.toUpperCase() })}
                    className="input"
                    maxLength={3}
                  />
                  )}
                </FormField>
              </div>

              <FormField label="Address Line 1">
                {(field) => (
                <input
                  {...field}
                  type="text"
                  value={editVendorForm.address_line1}
                  onChange={(e) => setEditVendorForm({ ...editVendorForm, address_line1: e.target.value })}
                  className="input"
                />
                )}
              </FormField>
              <FormField label="Address Line 2">
                {(field) => (
                <input
                  {...field}
                  type="text"
                  value={editVendorForm.address_line2}
                  onChange={(e) => setEditVendorForm({ ...editVendorForm, address_line2: e.target.value })}
                  className="input"
                />
                )}
              </FormField>
              <div className="grid grid-cols-3 gap-4">
                <FormField label="City">
                  {(field) => (
                  <input
                    {...field}
                    type="text"
                    value={editVendorForm.city}
                    onChange={(e) => setEditVendorForm({ ...editVendorForm, city: e.target.value })}
                    className="input"
                  />
                  )}
                </FormField>
                <FormField label="State">
                  {(field) => (
                  <input
                    {...field}
                    type="text"
                    value={editVendorForm.state}
                    onChange={(e) => setEditVendorForm({ ...editVendorForm, state: e.target.value.toUpperCase() })}
                    className="input"
                    maxLength={2}
                  />
                  )}
                </FormField>
                <FormField label="Postal Code">
                  {(field) => (
                  <input
                    {...field}
                    type="text"
                    value={editVendorForm.postal_code}
                    onChange={(e) => setEditVendorForm({ ...editVendorForm, postal_code: e.target.value })}
                    className="input"
                  />
                  )}
                </FormField>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={editVendorForm.is_approved}
                    onChange={(e) => setEditVendorForm({ ...editVendorForm, is_approved: e.target.checked })}
                    className="rounded border-slate-600"
                    aria-label="Approved Vendor"
                  />
                  <span>Approved Vendor</span>
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={editVendorForm.is_active}
                    onChange={(e) => setEditVendorForm({ ...editVendorForm, is_active: e.target.checked })}
                    className="rounded border-slate-600"
                    aria-label="Active"
                  />
                  <span>Active</span>
                </label>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={editVendorForm.is_as9100_certified}
                    onChange={(e) => setEditVendorForm({ ...editVendorForm, is_as9100_certified: e.target.checked })}
                    className="rounded border-slate-600"
                    aria-label="AS9100D Certified"
                  />
                  <span>AS9100D Certified</span>
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={editVendorForm.is_iso9001_certified}
                    onChange={(e) => setEditVendorForm({ ...editVendorForm, is_iso9001_certified: e.target.checked })}
                    className="rounded border-slate-600"
                    aria-label="ISO 9001 Certified"
                  />
                  <span>ISO 9001 Certified</span>
                </label>
              </div>

              <FormField label="Notes">
                {(field) => (
                <textarea
                  {...field}
                  value={editVendorForm.notes}
                  onChange={(e) => setEditVendorForm({ ...editVendorForm, notes: e.target.value })}
                  className="input"
                  rows={3}
                />
                )}
              </FormField>

              <div className="flex justify-end gap-3 pt-4 border-t">
                <Button variant="secondary" onClick={requestCloseEditVendorModal}>
                  Cancel
                </Button>
                <Button type="submit">Save Vendor</Button>
              </div>
            </form>

            <div className="mt-6 pt-4 border-t">
              <div className="flex items-center justify-between mb-3">
                <h4 className="font-semibold">Documents</h4>
              </div>

              <form onSubmit={handleVendorDocUpload} className="grid grid-cols-1 md:grid-cols-6 gap-3 mb-4">
                <input
                  type="text"
                  value={vendorDocForm.title}
                  onChange={(e) => setVendorDocForm({ ...vendorDocForm, title: e.target.value })}
                  className="input md:col-span-2"
                  placeholder="Title"
                  aria-label="Document title"
                />
                <select
                  value={vendorDocForm.document_type}
                  onChange={(e) => setVendorDocForm({ ...vendorDocForm, document_type: e.target.value })}
                  className="input md:col-span-1"
                >
                  {documentTypes.length > 0 ? (
                    documentTypes.map((t) => (
                      <option key={t.value} value={t.value}>{t.label}</option>
                    ))
                  ) : (
                    <option value="certificate">Certificate</option>
                  )}
                </select>
                <input
                  type="text"
                  value={vendorDocForm.revision}
                  onChange={(e) => setVendorDocForm({ ...vendorDocForm, revision: e.target.value })}
                  className="input md:col-span-1"
                  placeholder="Rev"
                  aria-label="Document revision"
                />
                <input
                  type="file"
                  onChange={(e) => setVendorDocForm({ ...vendorDocForm, file: e.target.files?.[0] || null })}
                  className="input md:col-span-1"
                  aria-label="Document file"
                />
                <Button type="submit" className="md:col-span-1">Upload</Button>
                <input
                  type="text"
                  value={vendorDocForm.description}
                  onChange={(e) => setVendorDocForm({ ...vendorDocForm, description: e.target.value })}
                  className="input md:col-span-6"
                  placeholder="Description (optional)"
                  aria-label="Document description"
                />
              </form>

              {vendorDocsLoading ? (
                <div className="text-sm text-slate-400">Loading documents...</div>
              ) : vendorDocsError ? (
                <ErrorState
                  message="Could not load vendor documents."
                  onRetry={() => selectedVendor && loadVendorDocuments(selectedVendor.id)}
                />
              ) : (
                <div className="overflow-x-auto">
                  <table className="min-w-full divide-y divide-slate-700">
                    <thead className="bg-slate-800">
                      <tr>
                        <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Title</th>
                        <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Type</th>
                        <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">File</th>
                        <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Uploaded</th>
                        <th className="px-3 py-2 text-right text-xs font-medium text-slate-400 uppercase">Actions</th>
                      </tr>
                    </thead>
                    <tbody className="bg-fd-panel divide-y divide-slate-700">
                      {vendorDocuments.map((doc) => (
                        <tr key={doc.id} className="hover:bg-slate-800">
                          <td className="px-3 py-2 text-sm">
                            <div className="font-medium">{doc.title}</div>
                            <div className="text-xs text-slate-400">{doc.document_number} Rev {doc.revision}</div>
                          </td>
                          <td className="px-3 py-2 text-sm capitalize">{doc.document_type.replace('_', ' ')}</td>
                          <td className="px-3 py-2 text-sm">
                            <div>{doc.file_name || '-'}</div>
                            <div className="text-xs text-slate-400">{formatFileSize(doc.file_size)}</div>
                          </td>
                          <td className="px-3 py-2 text-sm">{formatCentralDate(doc.created_at)}</td>
                          <td className="px-3 py-2 text-right" aria-label="Document actions">
                            <div className="flex justify-end gap-2">
                              <button
                                type="button"
                                onClick={() => handleVendorDocDownload(doc)}
                                className="text-werco-primary hover:text-blue-400 text-sm"
                              >
                                Download
                              </button>
                              <button
                                type="button"
                                onClick={() => handleVendorDocDelete(doc)}
                                className="text-red-600 hover:text-red-300 text-sm"
                              >
                                Delete
                              </button>
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {vendorDocuments.length === 0 && (
                    <EmptyState
                      title="No documents"
                      description="Upload a certificate or document for this vendor above."
                    />
                  )}
                </div>
              )}
            </div>
          </>
        )}
      </Modal>

      {/* Add New Part Modal */}
      <Modal open={showAddPartModal} onClose={() => setShowAddPartModal(false)} size="md" closeOnBackdrop={false}>
            <h3 className="text-lg font-semibold mb-4">Add New Part</h3>
            <form onSubmit={handleCreatePart} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <FormField label="Part Number" required>
                  {(field) => (
                  <input
                    {...field}
                    type="text"
                    value={newPart.part_number}
                    onChange={(e) => setNewPart({ ...newPart, part_number: e.target.value })}
                    className="input"
                    placeholder="e.g., RAW-001"
                    required
                  />
                  )}
                </FormField>
                <FormField label="Type">
                  {(field) => (
                  <select
                    {...field}
                    value={newPart.part_type}
                    onChange={(e) => setNewPart({ ...newPart, part_type: e.target.value })}
                    className="input"
                  >
                    <option value="purchased">Purchased</option>
                    <option value="raw_material">Raw Material</option>
                    <option value="manufactured">Manufactured</option>
                  </select>
                  )}
                </FormField>
              </div>
              <FormField label="Name" required>
                {(field) => (
                <input
                  {...field}
                  type="text"
                  value={newPart.name}
                  onChange={(e) => setNewPart({ ...newPart, name: e.target.value })}
                  className="input"
                  placeholder="Part description"
                  required
                />
                )}
              </FormField>
              <div className="grid grid-cols-2 gap-4">
                <FormField label="Unit of Measure">
                  {(field) => (
                  <select
                    {...field}
                    value={newPart.unit_of_measure}
                    onChange={(e) => setNewPart({ ...newPart, unit_of_measure: e.target.value })}
                    className="input"
                  >
                    <option value="EA">Each (EA)</option>
                    <option value="FT">Feet (FT)</option>
                    <option value="IN">Inches (IN)</option>
                    <option value="LB">Pounds (LB)</option>
                    <option value="KG">Kilograms (KG)</option>
                    <option value="GAL">Gallons (GAL)</option>
                    <option value="SHT">Sheets (SHT)</option>
                    <option value="BOX">Box (BOX)</option>
                  </select>
                  )}
                </FormField>
                <FormField label="Unit Cost ($)">
                  {(field) => (
                  <input
                    {...field}
                    type="number"
                    value={newPart.unit_cost}
                    onChange={(e) => setNewPart({ ...newPart, unit_cost: parseFloat(e.target.value) || 0 })}
                    className="input"
                    step={0.01}
                    min={0}
                  />
                  )}
                </FormField>
              </div>
              <FormField label="Description">
                {(field) => (
                <textarea
                  {...field}
                  value={newPart.description}
                  onChange={(e) => setNewPart({ ...newPart, description: e.target.value })}
                  className="input"
                  rows={2}
                  placeholder="Optional details"
                />
                )}
              </FormField>
              <div className="flex justify-end gap-3 pt-4 border-t">
                <Button variant="secondary" onClick={() => setShowAddPartModal(false)}>Cancel</Button>
                <Button type="submit">Create Part</Button>
              </div>
            </form>
      </Modal>

      {/* Delete PO confirm */}
      <ConfirmDialog
        open={!!deletePOTarget}
        title="Delete Purchase Order"
        message={
          deletePOTarget
            ? `Delete purchase order ${deletePOTarget.po_number}? This removes it from active lists while ` +
              'preserving the record for audit. An admin or manager can bring it back from the Deleted view above.'
            : ''
        }
        confirmLabel="Delete"
        pending={deletePOPending}
        variant="danger"
        onConfirm={handleConfirmDeletePO}
        onCancel={() => {
          if (!deletePOPending) setDeletePOTarget(null);
        }}
      />

      {/* Delete Vendor confirm */}
      <ConfirmDialog
        open={!!deleteVendorTarget}
        title="Delete Vendor"
        message={
          deleteVendorTarget
            ? `Delete vendor ${deleteVendorTarget.name}? This removes it from active lists while preserving the record for audit/restore.`
            : ''
        }
        confirmLabel="Delete"
        pending={deleteVendorPending}
        variant="danger"
        onConfirm={handleConfirmDeleteVendor}
        onCancel={() => {
          if (!deleteVendorPending) setDeleteVendorTarget(null);
        }}
      />

      {/* Delete vendor document confirm */}
      <ConfirmDialog
        open={!!deleteVendorDocTarget}
        title="Delete Document"
        message={
          deleteVendorDocTarget?.file_name
            ? `Delete "${deleteVendorDocTarget.file_name}"?`
            : 'Delete this document?'
        }
        confirmLabel="Delete"
        pending={deleteVendorDocPending}
        variant="danger"
        onConfirm={handleConfirmDeleteVendorDoc}
        onCancel={() => {
          if (!deleteVendorDocPending) setDeleteVendorDocTarget(null);
        }}
      />

      {/* Send PO confirm (non-destructive) */}
      <ConfirmDialog
        open={!!sendPOTarget}
        title="Send Purchase Order"
        message={sendPOTarget ? `Send ${sendPOTarget.po_number} to the vendor?` : 'Send this PO to vendor?'}
        confirmLabel="Send"
        pending={sendPOPending}
        variant="info"
        onConfirm={handleConfirmSendPO}
        onCancel={() => {
          if (!sendPOPending) setSendPOTarget(null);
        }}
      />
    </div>
  );
}
