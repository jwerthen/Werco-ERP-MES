import React, { useState, useCallback, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../services/api';
import { FormField, LoadingButton } from '../components/ui';
import {
  CloudArrowUpIcon,
  DocumentIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  InformationCircleIcon,
  XMarkIcon,
  ArrowPathIcon,
  DocumentCheckIcon,
  TrashIcon,
} from '@heroicons/react/24/outline';
import {
  partNumberKey,
  effectivePartNumber,
  newPartCoverageKeys,
  dedupePartsToCreate,
  buildLineItemsPayload,
} from '../utils/poUploadReview';
import { formatCurrency, formatPercent } from '../utils/numberFormat';

interface VendorMatch {
  matched: boolean;
  match_id: number | null;
  match_name: string;
  confidence: number;
  suggestions: Array<{ id: number; name: string; code: string; score: number }>;
}

interface PartMatch {
  matched: boolean;
  match_id: number | null;
  match_name: string;
  confidence: number;
  suggestions: Array<{ id: number; part_number: string; name: string; score: number }>;
}

interface LineItem {
  // Stable per-line identity for index-independent state (search boxes survive
  // line removal; async search results can't land on the wrong line).
  uid: number;
  line_number: number;
  part_number: string;
  description: string;
  qty_ordered: number;
  unit_of_measure: string;
  unit_price: number;
  line_total: number;
  confidence: string;
  suggested_part_type: 'purchased' | 'raw_material' | 'hardware' | 'consumable';
  suggested_part_number?: string | null;
  part_match: PartMatch | null;
  matched_part_id: number | null;
  // Form state
  selected_part_id: number | null;
  create_new_part: boolean;
  new_part_type: 'purchased' | 'raw_material' | 'hardware' | 'consumable';
}

interface ExtractionResult {
  document_type: 'po' | 'quote';
  po_number: string;
  quote_number: string | null;
  vendor: { name: string; address: string };
  vendor_match: VendorMatch | null;
  matched_vendor_id: number | null;
  order_date: string | null;
  expected_delivery_date: string | null;
  required_date: string | null;
  payment_terms: string | null;
  shipping_method: string | null;
  ship_to: string | null;
  line_items: LineItem[];
  subtotal: number | null;
  tax: number | null;
  shipping_cost: number | null;
  total_amount: number | null;
  notes: string | null;
  extraction_confidence: string;
  pdf_was_ocr: boolean;
  pdf_page_count: number;
  pdf_path: string;
  validation_issues: Array<{ field: string; severity: string; message: string }>;
  po_number_exists: boolean;
}

type DocStatus = 'queued' | 'extracting' | 'ready' | 'created' | 'error' | 'skipped';

interface QueuedDoc {
  uid: number;
  file: File;
  status: DocStatus;
  extractionResult: ExtractionResult | null;
  error: string | null;
  createdPO: { id: number; number: string } | null;
}

type Step = 'upload' | 'processing' | 'review' | 'summary';

const ALLOWED_EXTENSIONS = ['.pdf', '.doc', '.docx'];
const ALLOWED_MIME_TYPES = [
  'application/pdf',
  'application/msword',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
];
const MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024;

const isValidFile = (file: File) => {
  const ext = '.' + file.name.toLowerCase().split('.').pop();
  return ALLOWED_EXTENSIONS.includes(ext) || ALLOWED_MIME_TYPES.includes(file.type);
};

// The per-file extraction endpoints are stateless, so batch support is pure
// client-side orchestration; keep the in-flight uploads bounded.
const EXTRACTION_CONCURRENCY = 2;

const DOC_STATUS_LABELS: Record<DocStatus, string> = {
  queued: 'Queued',
  extracting: 'Extracting',
  ready: 'Ready',
  created: 'Created',
  error: 'Failed',
  skipped: 'Skipped',
};

const DOC_STATUS_STYLES: Record<DocStatus, string> = {
  queued: 'bg-slate-700/50 text-slate-300',
  extracting: 'bg-blue-500/20 text-blue-300',
  ready: 'bg-amber-500/20 text-amber-300',
  created: 'bg-green-500/20 text-emerald-300',
  error: 'bg-red-500/20 text-red-300',
  skipped: 'bg-slate-700/50 text-slate-400',
};

export default function POUpload() {
  const navigate = useNavigate();
  const [step, setStep] = useState<Step>('upload');
  const [documents, setDocuments] = useState<QueuedDoc[]>([]);
  const [currentDocUid, setCurrentDocUid] = useState<number | null>(null);
  const docUidRef = useRef(0);
  // Line-item uids come from a single counter shared across the whole batch
  // (same pattern as docUidRef): a stale part-search response from a previous
  // document keys to a uid that no longer exists, so it is inert.
  const lineUidRef = useRef(0);
  // Review generation: bumped whenever a document's review begins (and on
  // batch reset). Vendor and part searches capture the generation at request
  // time and drop responses whose generation is stale — vendorResults are not
  // uid-keyed, so this is what keeps doc N's late response off doc N+1's screen.
  const reviewGenRef = useRef(0);
  // Batch generation: bumped by Start Over / Upload More Documents and on
  // unmount to cancel the extraction pool. Workers check it before starting
  // each file, and late completions from an old generation are dropped
  // instead of mutating state.
  const batchGenRef = useRef(0);
  // Uids with a retry pool currently running — re-entry guard for a Retry
  // double-fire in the same task, before the 'queued' flip has rendered.
  const retryingUidsRef = useRef<Set<number>>(new Set());
  const [documentType, setDocumentType] = useState<'po' | 'quote'>('po');
  const [dragActive, setDragActive] = useState(false);
  const [error, setError] = useState('');
  const [pdfOpening, setPdfOpening] = useState(false);
  const [creatingPO, setCreatingPO] = useState(false);

  // Latest-value mirror of currentDocUid so async create handlers can tell,
  // at resolution time, whether the doc they were submitted for is still the
  // one under review (a late error must not land on the NEXT doc's screen).
  const currentDocUidRef = useRef<number | null>(null);
  useEffect(() => {
    currentDocUidRef.current = currentDocUid;
  }, [currentDocUid]);

  // Cancel any running extraction pool when the page unmounts — each
  // abandoned file otherwise costs a server upload + an AI extraction.
  useEffect(() => {
    return () => {
      batchGenRef.current++;
    };
  }, []);

  // Form state for review — exists only for the doc currently under review
  // (the flow is forward-only; there is no going back to a previous doc).
  const [formData, setFormData] = useState({
    po_number: '',
    vendor_id: null as number | null,
    create_vendor: false,
    new_vendor_name: '',
    new_vendor_code: '',
    new_vendor_address: '',
    order_date: '',
    required_date: '',
    expected_date: '',
    shipping_method: '',
    ship_to: '',
    notes: '',
  });
  const [lineItems, setLineItems] = useState<LineItem[]>([]);
  const [vendorSearch, setVendorSearch] = useState('');
  const [vendorResults, setVendorResults] = useState<any[]>([]);
  const [partSearches, setPartSearches] = useState<{ [key: number]: string }>({});
  const [partResults, setPartResults] = useState<{ [key: number]: any[] }>({});

  const currentDoc = currentDocUid !== null ? (documents.find(d => d.uid === currentDocUid) ?? null) : null;
  const extractionResult = currentDoc?.extractionResult ?? null;

  // Handle file drop
  const handleDrag = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.type === 'dragenter' || e.type === 'dragover') {
      setDragActive(true);
    } else if (e.type === 'dragleave') {
      setDragActive(false);
    }
  }, []);

  // Validate and append files to the queue. Oversize/unsupported files are
  // rejected with a per-file message; valid ones are still added. Dedupe is
  // by name+size so re-dropping the same selection doesn't double up.
  const addFiles = (incoming: File[]) => {
    const rejected: string[] = [];
    const seen = new Set(documents.map(d => `${d.file.name}|${d.file.size}`));
    const added: QueuedDoc[] = [];
    for (const f of incoming) {
      if (!isValidFile(f)) {
        rejected.push(`${f.name}: only PDF and Word documents (.pdf, .doc, .docx) are supported`);
        continue;
      }
      if (f.size > MAX_FILE_SIZE_BYTES) {
        rejected.push(`${f.name}: exceeds the 10MB limit`);
        continue;
      }
      const key = `${f.name}|${f.size}`;
      if (seen.has(key)) continue;
      seen.add(key);
      added.push({
        uid: docUidRef.current++,
        file: f,
        status: 'queued',
        extractionResult: null,
        error: null,
        createdPO: null,
      });
    }
    if (added.length > 0) {
      setDocuments(prev => [...prev, ...added]);
    }
    setError(rejected.join('\n'));
  };

  // Not memoized: addFiles dedupes against the latest documents state.
  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);

    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      addFiles(Array.from(e.dataTransfer.files));
    }
  };

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      addFiles(Array.from(e.target.files));
    }
    // Clear so re-selecting a just-removed file still fires onChange.
    e.target.value = '';
  };

  const removeQueuedFile = (uid: number) => {
    setDocuments(prev => prev.filter(d => d.uid !== uid));
  };

  const handleOpenPdf = async () => {
    if (!extractionResult?.pdf_path || pdfOpening) return;

    setPdfOpening(true);
    const pdfWindow = window.open('about:blank', '_blank');
    if (pdfWindow) {
      pdfWindow.opener = null;
    }

    try {
      const pdfPath = extractionResult.pdf_path.replace('uploads/purchase_orders/', '');
      const blob = await api.downloadPOPdf(pdfPath);
      const url = window.URL.createObjectURL(blob);
      if (pdfWindow) {
        pdfWindow.location.href = url;
      } else {
        const link = document.createElement('a');
        link.href = url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.click();
      }
      window.setTimeout(() => window.URL.revokeObjectURL(url), 30000);
    } catch (err: any) {
      pdfWindow?.close();
      setError(err.response?.data?.detail || 'Failed to open PDF');
    } finally {
      setPdfOpening(false);
    }
  };

  // Run extractions through a small pool (max EXTRACTION_CONCURRENCY in
  // flight). Every per-doc update is a functional setState keyed by uid so
  // concurrent completions never clobber each other. The pool belongs to the
  // batch generation it started under: Start Over / unmount bump the
  // generation, after which no NEW file starts and requests that settle late
  // are dropped instead of mutating a newer batch's state.
  const runExtractionPool = async (docs: Array<{ uid: number; file: File }>) => {
    const gen = batchGenRef.current;
    const queue = [...docs];
    const worker = async () => {
      for (;;) {
        if (batchGenRef.current !== gen) return; // batch cancelled — start nothing new
        const next = queue.shift();
        if (!next) return;
        setDocuments(prev => prev.map(d => (d.uid === next.uid ? { ...d, status: 'extracting' as const } : d)));
        try {
          const result =
            documentType === 'quote' ? await api.uploadQuotePdf(next.file) : await api.uploadPOPdf(next.file);
          if (batchGenRef.current !== gen) return; // late completion from an old batch
          setDocuments(prev =>
            prev.map(d =>
              d.uid === next.uid ? { ...d, status: 'ready' as const, extractionResult: result, error: null } : d
            )
          );
        } catch (err: any) {
          if (batchGenRef.current !== gen) return; // late failure from an old batch
          const detail = err.response?.data?.detail;
          setDocuments(prev =>
            prev.map(d =>
              d.uid === next.uid
                ? {
                    ...d,
                    status: 'error' as const,
                    error: typeof detail === 'string' && detail ? detail : 'Failed to process document',
                    extractionResult: null,
                  }
                : d
            )
          );
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(EXTRACTION_CONCURRENCY, queue.length) }, () => worker()));
  };

  const handleExtractAll = () => {
    if (documents.length === 0) return;
    setError('');
    setStep('processing');
    void runExtractionPool(documents.filter(d => d.status === 'queued').map(d => ({ uid: d.uid, file: d.file })));
  };

  // Load a doc's extraction result into the review draft state (factored from
  // the old single-file init). Search state is reset per doc.
  const beginReview = useCallback((doc: QueuedDoc) => {
    const result = doc.extractionResult;
    if (!result) return;

    // Invalidate any in-flight vendor/part searches from the previous review.
    reviewGenRef.current++;

    setFormData({
      po_number: result.po_number || '',
      vendor_id: result.matched_vendor_id,
      create_vendor: false,
      new_vendor_name: result.vendor?.name || '',
      new_vendor_code: '',
      new_vendor_address: result.vendor?.address || '',
      order_date: result.order_date || '',
      required_date: result.required_date || '',
      expected_date: result.expected_delivery_date || '',
      shipping_method: result.shipping_method || '',
      ship_to: result.ship_to || '',
      notes: result.notes || '',
    });

    // Initialize line items with match info
    setLineItems(
      result.line_items.map((item: any) => {
        const desc = (item.description || '').trim().toLowerCase();
        const pnRaw = item.part_number || '';
        const pn = pnRaw.trim().toLowerCase();
        const suggested = item.suggested_part_number || '';
        const looksLikeDescription =
          (pnRaw && pn.includes(' ') && !pnRaw.includes('-')) || pn.includes('ga') || pn.includes(' x ');
        const shouldUseSuggested = Boolean(suggested) && (!pn || (desc && pn === desc) || looksLikeDescription);
        return {
          ...item,
          uid: lineUidRef.current++,
          part_number: shouldUseSuggested ? suggested : item.part_number,
          selected_part_id: item.matched_part_id,
          create_new_part: false,
          new_part_type: (item.suggested_part_type || 'purchased') as LineItem['new_part_type'],
        };
      })
    );

    setVendorSearch('');
    setVendorResults([]);
    setPartSearches({});
    setPartResults({});
    setError('');
    setCurrentDocUid(doc.uid);
    setStep('review');
  }, []);

  // Queue sequencer: review-when-ready with add-order preference. Whenever no
  // doc is mid-review, route to the FIRST doc in add order that is 'ready' —
  // deliberately not strict add order, so a fast doc 2 is never blocked behind
  // a slow doc 1 (docs that become ready later get reviewed later). Else show
  // the processing screen while extractions are pending; else the summary.
  // 'ready' is the only reviewable status, so created/skipped/error docs are
  // never re-entered.
  useEffect(() => {
    if (step === 'upload') return;
    if (step === 'review' && currentDocUid !== null) return; // user is mid-review
    const nextReady = documents.find(d => d.status === 'ready');
    if (nextReady) {
      beginReview(nextReady);
      return;
    }
    if (documents.some(d => d.status === 'queued' || d.status === 'extracting')) {
      if (step !== 'processing') setStep('processing');
      return;
    }
    if (documents.length > 0 && step !== 'summary') setStep('summary');
  }, [documents, step, currentDocUid, beginReview]);

  const handleSkipDocument = () => {
    if (currentDocUid === null) return;
    const uid = currentDocUid;
    setDocuments(prev => prev.map(d => (d.uid === uid ? { ...d, status: 'skipped' as const } : d)));
    setCurrentDocUid(null);
    setStep('processing'); // sequencer routes to the next doc (or the summary)
  };

  // Re-queue a single failed doc through the extraction pool. Guarded against
  // double-fire: the status flips to 'queued' synchronously (so the Retry
  // button is gone before the retry's request resolves), a doc that is no
  // longer 'error' is a no-op, and the uid-set ref blocks a re-entrant call
  // in the same task, before the 'queued' flip has rendered.
  const handleRetryDocument = (uid: number) => {
    const doc = documents.find(d => d.uid === uid);
    if (!doc || doc.status !== 'error' || retryingUidsRef.current.has(uid)) return;
    retryingUidsRef.current.add(uid);
    setDocuments(prev => prev.map(d => (d.uid === uid ? { ...d, status: 'queued' as const, error: null } : d)));
    setStep('processing');
    void runExtractionPool([{ uid, file: doc.file }]).finally(() => {
      retryingUidsRef.current.delete(uid);
    });
  };

  const resetBatch = () => {
    batchGenRef.current++; // cancel the extraction pool: no new file may start
    reviewGenRef.current++; // drop any in-flight review searches
    setDocuments([]);
    setCurrentDocUid(null);
    setLineItems([]);
    setVendorSearch('');
    setVendorResults([]);
    setPartSearches({});
    setPartResults({});
    setError('');
    setStep('upload');
  };

  // Search vendors. The generation is captured at request time; a response
  // that resolves after the review has moved to another document is dropped
  // rather than populating the new doc's vendor dropdown.
  useEffect(() => {
    if (vendorSearch.length >= 2) {
      const gen = reviewGenRef.current;
      const timeout = setTimeout(async () => {
        try {
          const results = await api.searchVendorsForPO(vendorSearch);
          if (reviewGenRef.current === gen) setVendorResults(results);
        } catch (err) {
          console.error('Vendor search failed:', err);
        }
      }, 300);
      return () => clearTimeout(timeout);
    } else {
      setVendorResults([]);
    }
  }, [vendorSearch]);

  // Search parts for a specific line. Keyed by the line's stable uid — unique
  // across the whole batch, not just this document — so an in-flight response
  // can never land on a different line after a removal re-indexes the list or
  // after the review advances to the next document. The generation check
  // drops stale responses outright rather than storing them under dead uids.
  const searchParts = async (lineUid: number, query: string) => {
    setPartSearches(prev => ({ ...prev, [lineUid]: query }));

    if (query.length >= 2) {
      const gen = reviewGenRef.current;
      try {
        const results = await api.searchPartsForPO(query);
        if (reviewGenRef.current === gen) {
          setPartResults(prev => ({ ...prev, [lineUid]: results }));
        }
      } catch (err) {
        console.error('Part search failed:', err);
      }
    } else {
      setPartResults(prev => ({ ...prev, [lineUid]: [] }));
    }
  };

  const selectPartForLine = (lineUid: number, partId: number, partNumber: string) => {
    setLineItems(prev =>
      prev.map(item =>
        item.uid === lineUid
          ? { ...item, selected_part_id: partId, part_number: partNumber, create_new_part: false }
          : item
      )
    );
    setPartResults(prev => ({ ...prev, [lineUid]: [] }));
    setPartSearches(prev => ({ ...prev, [lineUid]: '' }));
  };

  const toggleCreatePart = (lineUid: number) => {
    setLineItems(prev =>
      prev.map(item =>
        item.uid === lineUid
          ? {
              ...item,
              create_new_part: !item.create_new_part,
              selected_part_id: null,
              new_part_type: item.suggested_part_type || 'purchased',
              part_number: item.part_number || item.suggested_part_number || item.part_number,
            }
          : item
      )
    );
  };

  const removeLine = (lineUid: number) => {
    setLineItems(prev => prev.filter(item => item.uid !== lineUid));
    // Search state is uid-keyed, so only this line's entries need to go;
    // other lines keep their in-progress searches.
    setPartSearches(prev => {
      const rest = { ...prev };
      delete rest[lineUid];
      return rest;
    });
    setPartResults(prev => {
      const rest = { ...prev };
      delete rest[lineUid];
      return rest;
    });
  };

  const setPartType = (lineUid: number, partType: LineItem['new_part_type']) => {
    setLineItems(prev => prev.map(item => (item.uid === lineUid ? { ...item, new_part_type: partType } : item)));
  };

  // True when this PO number was already created earlier in THIS batch: the
  // extraction-time po_number_exists flag predates those creations, so
  // without this check the dupe only surfaces as a server 400 on submit.
  const isPoNumberCreatedInBatch = (poNumber: string) => {
    const key = poNumber.trim().toLowerCase();
    return key !== '' && documents.some(d => d.createdPO !== null && d.createdPO.number.trim().toLowerCase() === key);
  };

  const handleCreatePO = async () => {
    // In-flight guard: the button is also disabled while creating, but the
    // backend duplicate check is check-then-insert, so never let a second
    // create fire for the same doc.
    if (creatingPO || currentDocUid === null) return;
    const uid = currentDocUid; // snapshot at submit time
    setError('');

    // Validate
    if (!formData.po_number) {
      setError('PO number is required');
      return;
    }

    if (isPoNumberCreatedInBatch(formData.po_number)) {
      setError(
        `PO number ${formData.po_number.trim()} was already created earlier in this batch. Enter a different PO number.`
      );
      return;
    }

    if (!formData.vendor_id && !formData.create_vendor) {
      setError('Please select a vendor or create a new one');
      return;
    }

    // Check all line items have parts. A line without its own assignment is
    // still covered when another line with the same part-number key is marked
    // "create new part" — the backend creates the part once for both lines.
    const coveredKeys = newPartCoverageKeys(lineItems);
    const unmatchedLines = lineItems.filter(item => {
      if (item.selected_part_id || item.create_new_part) return false;
      const key = partNumberKey(item);
      return !key || !coveredKeys.has(key);
    });
    if (unmatchedLines.length > 0) {
      setError(`${unmatchedLines.length} line(s) need part assignment`);
      return;
    }

    // Trimmed check: a whitespace-only part number is as missing as an empty one
    // (the payload builders trim, so it would otherwise reach the server as '').
    const missingNewPartNumbers = lineItems.filter(item => item.create_new_part && !effectivePartNumber(item));
    if (missingNewPartNumbers.length > 0) {
      setError('All new parts must have a part number or a suggested Werco number.');
      return;
    }

    setCreatingPO(true);
    try {
      const result = await api.createPOFromUpload({
        po_number: formData.po_number,
        vendor_id: formData.vendor_id || 0,
        create_vendor: formData.create_vendor,
        new_vendor_name: formData.new_vendor_name,
        new_vendor_code: formData.new_vendor_code,
        new_vendor_address: formData.new_vendor_address,
        order_date: formData.order_date || undefined,
        required_date: formData.required_date || undefined,
        expected_date: formData.expected_date || undefined,
        shipping_method: formData.shipping_method || undefined,
        ship_to: formData.ship_to || undefined,
        notes: formData.notes || undefined,
        line_items: buildLineItemsPayload(lineItems),
        create_parts: dedupePartsToCreate(lineItems),
        pdf_path: extractionResult?.pdf_path || '',
      });

      if (result.success) {
        setDocuments(prev =>
          prev.map(d =>
            d.uid === uid
              ? { ...d, status: 'created' as const, createdPO: { id: result.po_id, number: result.po_number } }
              : d
          )
        );
        // Only advance the sequencer if this doc is still the one under
        // review; a late success must not yank a different screen away.
        if (currentDocUidRef.current === uid) {
          setCurrentDocUid(null);
          setStep('processing'); // sequencer routes to the next doc (or the summary)
        }
      } else if (currentDocUidRef.current === uid) {
        setError(result.message || 'Failed to create PO');
      }
    } catch (err: any) {
      // Drop late errors once the review has moved past this doc — they would
      // otherwise render on the NEXT document's review screen.
      if (currentDocUidRef.current === uid) {
        setError(err.response?.data?.detail || 'Failed to create PO');
      }
    } finally {
      setCreatingPO(false);
    }
  };

  const getConfidenceBadge = (confidence: string) => {
    const colors = {
      high: 'bg-green-500/20 text-emerald-300',
      medium: 'bg-amber-500/20 text-amber-300',
      low: 'bg-red-500/20 text-red-300',
    };
    return colors[confidence as keyof typeof colors] || colors.medium;
  };

  // UPLOAD STEP
  if (step === 'upload') {
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-3xl font-bold text-white">Upload Purchasing Document(s)</h1>
          <p className="text-slate-400 mt-1">Upload one or more POs or Quotes (PDF/DOCX) to extract line items</p>
        </div>

        {error && (
          <div className="bg-red-500/10 border border-red-500/30 rounded-xl p-4 flex items-center gap-3">
            <ExclamationTriangleIcon className="h-5 w-5 text-red-600" />
            <span className="text-red-300 whitespace-pre-line">{error}</span>
          </div>
        )}

        <div className="card">
          <div className="mb-6 flex items-center gap-2">
            <button
              type="button"
              onClick={() => setDocumentType('po')}
              className={`px-4 py-2 rounded-lg text-sm font-semibold ${
                documentType === 'po' ? 'bg-werco-primary text-white' : 'bg-slate-800/50 text-slate-400'
              }`}
            >
              Purchase Order
            </button>
            <button
              type="button"
              onClick={() => setDocumentType('quote')}
              className={`px-4 py-2 rounded-lg text-sm font-semibold ${
                documentType === 'quote' ? 'bg-werco-primary text-white' : 'bg-slate-800/50 text-slate-400'
              }`}
            >
              Vendor Quote
            </button>
          </div>
          {/* Drag-and-drop is a pointer-only enhancement: keyboard/AT users use
              the "Browse Files" file input inside this zone, so the dropzone
              itself is presentational rather than a focusable interactive element. */}
          <div
            className={`border-2 border-dashed rounded-xl p-12 text-center transition-colors ${
              dragActive
                ? 'border-werco-primary bg-werco-500/10'
                : documents.length > 0
                  ? 'border-green-500/50 bg-green-500/10'
                  : 'border-slate-600 hover:border-gray-400'
            }`}
            role="presentation"
            onDragEnter={handleDrag}
            onDragLeave={handleDrag}
            onDragOver={handleDrag}
            onDrop={handleDrop}
          >
            {documents.length > 0 ? (
              <div className="flex flex-col items-center">
                <DocumentIcon className="h-12 w-12 text-green-500 mb-4" />
                <ul className="w-full max-w-md space-y-2 text-left">
                  {documents.map(doc => (
                    <li
                      key={doc.uid}
                      className="flex items-center justify-between gap-3 bg-slate-800/50 rounded-lg px-3 py-2"
                    >
                      <div className="min-w-0">
                        <p className="text-sm font-medium text-white truncate">{doc.file.name}</p>
                        <p className="text-xs text-slate-400">{(doc.file.size / 1024 / 1024).toFixed(2)} MB</p>
                      </div>
                      <button
                        type="button"
                        onClick={() => removeQueuedFile(doc.uid)}
                        aria-label={`Remove ${doc.file.name}`}
                        title={`Remove ${doc.file.name}`}
                        className="text-slate-400 hover:text-red-400 shrink-0"
                      >
                        <XMarkIcon className="h-4 w-4" />
                      </button>
                    </li>
                  ))}
                </ul>
                <label className="mt-4 inline-block">
                  <span className="btn-secondary text-sm cursor-pointer">Add More Files</span>
                  <input
                    type="file"
                    multiple
                    aria-label="Add more files"
                    accept=".pdf,.doc,.docx,application/pdf,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    onChange={handleFileSelect}
                    className="hidden"
                  />
                </label>
              </div>
            ) : (
              <>
                <CloudArrowUpIcon className="h-16 w-16 text-slate-400 mx-auto mb-4" />
                <p className="text-lg font-medium text-white">
                  Drag and drop your {documentType === 'quote' ? 'quote' : 'PO'} document(s) here
                </p>
                <p className="text-sm text-slate-400 mt-1">or</p>
                <label className="mt-4 inline-block">
                  <span className="btn-primary cursor-pointer">Browse Files</span>
                  <input
                    type="file"
                    multiple
                    aria-label="Browse files"
                    accept=".pdf,.doc,.docx,application/pdf,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    onChange={handleFileSelect}
                    className="hidden"
                  />
                </label>
                <p className="text-xs text-slate-400 mt-4">Supported: PDF, DOC, DOCX (max 10MB each)</p>
              </>
            )}
          </div>

          {documents.length > 0 && (
            <div className="mt-6 flex justify-end">
              <button onClick={handleExtractAll} className="btn-primary px-8">
                Extract Data{documents.length > 1 ? ` (${documents.length} files)` : ''}
              </button>
            </div>
          )}
        </div>

        <div className="card bg-blue-500/10 border-blue-500/30">
          <h3 className="font-semibold text-blue-300 mb-2">How it works</h3>
          <ol className="list-decimal list-inside space-y-1 text-sm text-blue-400">
            <li>Upload one or more POs or vendor quotes (PDF or Word documents)</li>
            <li>AI extracts vendor, line items, and order details from each document</li>
            <li>Review and verify the extracted data — one document at a time</li>
            <li>Match parts to your inventory or create new ones</li>
            <li>Confirm to create each PO in your system</li>
          </ol>
        </div>
      </div>
    );
  }

  // SUMMARY STEP
  if (step === 'summary') {
    const created = documents.filter(d => d.status === 'created');
    const failed = documents.filter(d => d.status === 'error');
    const skipped = documents.filter(d => d.status === 'skipped');
    return (
      <div className="flex flex-col items-center justify-center min-h-[400px]">
        {failed.length > 0 ? (
          <ExclamationTriangleIcon className="h-20 w-20 text-amber-500 mb-6" />
        ) : created.length > 0 ? (
          <CheckCircleIcon className="h-20 w-20 text-green-500 mb-6" />
        ) : (
          <DocumentIcon className="h-20 w-20 text-slate-400 mb-6" />
        )}
        <h2 className="text-2xl font-bold text-white">
          {created.length === 0
            ? 'No Purchase Orders Created'
            : created.length === 1
              ? 'Purchase Order Created!'
              : `${created.length} Purchase Orders Created!`}
        </h2>
        {created.length === 1 && failed.length === 0 && skipped.length === 0 ? (
          <p className="text-slate-400 mt-2">PO {created[0].createdPO?.number} has been created successfully</p>
        ) : (
          <p className="text-slate-400 mt-2">
            {created.length} created &middot; {failed.length} failed &middot; {skipped.length} skipped
          </p>
        )}

        {(documents.length > 1 || failed.length > 0 || skipped.length > 0) && (
          <div className="card w-full max-w-2xl mt-8 space-y-3">
            {created.map(doc => (
              <div
                key={doc.uid}
                className="flex items-center gap-3 border border-green-500/30 bg-green-500/10 rounded-lg px-4 py-3"
              >
                <CheckCircleIcon className="h-5 w-5 text-green-500 shrink-0" />
                <div className="min-w-0">
                  <button
                    type="button"
                    onClick={() => navigate('/purchasing')}
                    className="font-medium text-emerald-300 hover:underline"
                  >
                    PO {doc.createdPO?.number}
                  </button>
                  <p className="text-xs text-slate-400 truncate">{doc.file.name}</p>
                </div>
              </div>
            ))}
            {failed.map(doc => (
              <div
                key={doc.uid}
                className="flex items-center justify-between gap-3 border border-red-500/30 bg-red-500/10 rounded-lg px-4 py-3"
              >
                <div className="flex items-center gap-3 min-w-0">
                  <ExclamationTriangleIcon className="h-5 w-5 text-red-500 shrink-0" />
                  <div className="min-w-0">
                    <p className="font-medium text-red-300 truncate">{doc.file.name}</p>
                    <p className="text-xs text-red-400">{doc.error}</p>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => handleRetryDocument(doc.uid)}
                  className="btn-secondary text-sm shrink-0"
                >
                  Retry
                </button>
              </div>
            ))}
            {skipped.map(doc => (
              <div key={doc.uid} className="flex items-center gap-3 border border-slate-700 rounded-lg px-4 py-3">
                <DocumentIcon className="h-5 w-5 text-slate-400 shrink-0" />
                <p className="font-medium text-slate-400 truncate">{doc.file.name}</p>
                <span className="ml-auto text-xs text-slate-500 shrink-0">Skipped</span>
              </div>
            ))}
          </div>
        )}

        <div className="flex gap-4 mt-8">
          <button onClick={resetBatch} className="btn-secondary px-6">
            Upload More Documents
          </button>
          <button onClick={() => navigate(`/purchasing`)} className="btn-secondary px-6">
            Go to Purchasing
          </button>
          <button onClick={() => navigate(`/receiving`)} className="btn-primary px-6">
            Go to Receiving
          </button>
        </div>
      </div>
    );
  }

  // PROCESSING STEP (also the transient frame while the sequencer routes
  // between documents — the review render below needs a current doc).
  if (step !== 'review' || !currentDoc || !extractionResult) {
    const done = documents.filter(d => d.status !== 'queued' && d.status !== 'extracting').length;
    return (
      <div className="flex flex-col items-center justify-center min-h-[400px]">
        <ArrowPathIcon className="h-16 w-16 text-werco-primary animate-spin mb-6" />
        <h2 className="text-xl font-semibold text-white">
          {documents.length > 1
            ? `Processing documents (${done}/${documents.length} complete)...`
            : 'Processing document...'}
        </h2>
        <p className="text-slate-400 mt-2">Extracting data with AI. This may take a moment.</p>
        {documents.length > 1 && (
          <ul className="mt-6 w-full max-w-md space-y-1">
            {documents.map(doc => (
              <li key={doc.uid} className="flex items-center justify-between gap-3 text-sm">
                <span className="text-slate-300 truncate">{doc.file.name}</span>
                <span className={`px-2 py-0.5 rounded text-xs shrink-0 ${DOC_STATUS_STYLES[doc.status]}`}>
                  {DOC_STATUS_LABELS[doc.status]}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    );
  }

  // REVIEW STEP
  // Keys of lines marked "create new part" — an unassigned line whose key is
  // in this set shares that new part instead of blocking submission.
  const coverageKeys = newPartCoverageKeys(lineItems);
  // Same-number PO already created earlier in this batch (extraction-time
  // po_number_exists can't know about it) — warn here, block in handleCreatePO.
  const poNumberDuplicateInBatch = isPoNumberCreatedInBatch(formData.po_number);

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-3xl font-bold text-white">Review Extracted Data</h1>
          <p className="text-slate-400 mt-1">Verify and edit the extracted information before creating the PO</p>
        </div>
        <div className="flex items-center gap-3">
          {extractionResult?.pdf_was_ocr && (
            <span className="px-3 py-1 rounded-full text-sm bg-amber-500/20 text-amber-300">OCR Processed</span>
          )}
          <span
            className={`px-3 py-1 rounded-full text-sm ${getConfidenceBadge(extractionResult?.extraction_confidence || 'medium')}`}
          >
            {extractionResult?.extraction_confidence} confidence
          </span>
        </div>
      </div>

      {/* Batch progress strip */}
      {documents.length > 1 && (
        <div className="card py-3">
          <div className="flex items-center justify-between gap-4 flex-wrap">
            <p className="text-sm font-medium text-slate-300">
              Document {documents.findIndex(d => d.uid === currentDoc.uid) + 1} of {documents.length}
              <span className="text-slate-400"> — {currentDoc.file.name}</span>
            </p>
            <div className="flex items-center gap-2 flex-wrap">
              {documents.map(doc => (
                <span
                  key={doc.uid}
                  title={doc.file.name}
                  className={`flex items-center gap-1.5 px-2 py-0.5 rounded text-xs ${
                    doc.uid === currentDoc.uid ? 'ring-1 ring-werco-primary ' : ''
                  }${DOC_STATUS_STYLES[doc.status]}`}
                >
                  <span className="max-w-[8rem] truncate">{doc.file.name}</span>
                  <span className="opacity-75">
                    {doc.uid === currentDoc.uid ? 'Reviewing' : DOC_STATUS_LABELS[doc.status]}
                  </span>
                </span>
              ))}
            </div>
          </div>
        </div>
      )}

      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-xl p-4 flex items-center gap-3">
          <ExclamationTriangleIcon className="h-5 w-5 text-red-600" />
          <span className="text-red-300">{error}</span>
          <button onClick={() => setError('')} className="ml-auto">
            <XMarkIcon className="h-5 w-5 text-red-600" />
          </button>
        </div>
      )}

      {/* Validation Issues */}
      {extractionResult?.validation_issues && extractionResult.validation_issues.length > 0 && (
        <div className="bg-amber-500/10 border border-amber-500/30 rounded-xl p-4">
          <h3 className="font-semibold text-amber-300 mb-2">Issues to Review</h3>
          <ul className="space-y-1">
            {extractionResult.validation_issues.map((issue, idx) => (
              <li key={idx} className={`text-sm ${issue.severity === 'error' ? 'text-red-400' : 'text-amber-400'}`}>
                - {issue.message}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="grid grid-cols-3 gap-6">
        {/* Left: PDF Preview placeholder */}
        <div className="col-span-1">
          <div className="card sticky top-4">
            <h3 className="font-semibold mb-4">Source Document</h3>
            <div className="bg-slate-800/50 rounded-lg p-4 text-center">
              <DocumentIcon className="h-12 w-12 text-slate-400 mx-auto mb-2" />
              <p className="text-sm text-slate-400">{currentDoc.file.name}</p>
              <p className="text-xs text-slate-400 mt-1">{extractionResult?.pdf_page_count} page(s)</p>
              {extractionResult?.pdf_path && (
                <button
                  type="button"
                  onClick={handleOpenPdf}
                  disabled={pdfOpening}
                  className="btn-secondary text-sm mt-4 inline-block"
                >
                  {pdfOpening ? 'Opening...' : 'Open PDF'}
                </button>
              )}
            </div>
          </div>
        </div>

        {/* Right: Form */}
        <div className="col-span-2 space-y-6">
          {/* Header Info */}
          <div className="card">
            <h3 className="font-semibold mb-4">PO Information</h3>
            <div className="grid grid-cols-2 gap-4">
              <FormField label="PO Number" required labelClassName="block text-sm font-medium text-slate-300 mb-1">
                {field => (
                  <>
                    <input
                      {...field}
                      type="text"
                      value={formData.po_number}
                      onChange={e => setFormData({ ...formData, po_number: e.target.value })}
                      className={`input w-full ${
                        extractionResult?.po_number_exists || poNumberDuplicateInBatch ? 'border-red-500/40' : ''
                      }`}
                    />
                    {extractionResult?.po_number_exists && (
                      <p className="text-xs text-red-600 mt-1">This PO number already exists</p>
                    )}
                    {poNumberDuplicateInBatch && (
                      <p className="text-xs text-red-600 mt-1">This PO number was already created in this batch</p>
                    )}
                    {extractionResult?.document_type === 'quote' && extractionResult?.quote_number && (
                      <p className="text-xs text-slate-400 mt-1">Quote #: {extractionResult.quote_number}</p>
                    )}
                  </>
                )}
              </FormField>
              <FormField label="Order Date" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                {field => (
                  <input
                    {...field}
                    type="date"
                    value={formData.order_date}
                    onChange={e => setFormData({ ...formData, order_date: e.target.value })}
                    className="input w-full"
                  />
                )}
              </FormField>
              <FormField label="Required Date" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                {field => (
                  <input
                    {...field}
                    type="date"
                    value={formData.required_date}
                    onChange={e => setFormData({ ...formData, required_date: e.target.value })}
                    className="input w-full"
                  />
                )}
              </FormField>
              <FormField label="Expected Delivery" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                {field => (
                  <input
                    {...field}
                    type="date"
                    value={formData.expected_date}
                    onChange={e => setFormData({ ...formData, expected_date: e.target.value })}
                    className="input w-full"
                  />
                )}
              </FormField>
            </div>
          </div>

          {/* Vendor Section */}
          <div className="card">
            <h3 className="font-semibold mb-4">Vendor</h3>

            {extractionResult?.vendor_match?.matched && !formData.create_vendor ? (
              <div className="bg-green-500/10 border border-green-500/30 rounded-lg p-4 mb-4">
                <div className="flex justify-between items-center">
                  <div>
                    <p className="font-medium text-emerald-300">Matched: {extractionResult.vendor_match.match_name}</p>
                    <p className="text-sm text-green-600">
                      Confidence: {formatPercent(extractionResult.vendor_match.confidence)}
                    </p>
                  </div>
                  <button
                    onClick={() => setFormData({ ...formData, vendor_id: null })}
                    className="text-sm text-emerald-400 hover:underline"
                  >
                    Change
                  </button>
                </div>
              </div>
            ) : !formData.create_vendor ? (
              <div className="space-y-3">
                <p className="text-sm text-amber-600">
                  Extracted vendor: "{extractionResult?.vendor?.name}" - No exact match found
                </p>

                {/* Suggestions */}
                {extractionResult?.vendor_match?.suggestions &&
                  extractionResult.vendor_match.suggestions.length > 0 && (
                    <div className="space-y-2">
                      <p className="text-sm font-medium">Suggestions:</p>
                      {extractionResult.vendor_match.suggestions.map(sug => (
                        <button
                          key={sug.id}
                          onClick={() => setFormData({ ...formData, vendor_id: sug.id })}
                          className="block w-full text-left px-3 py-2 rounded-lg border hover:bg-slate-800"
                        >
                          <span className="font-medium">{sug.name}</span>
                          <span className="text-slate-400 text-sm ml-2">({sug.code})</span>
                          <span className="text-xs text-slate-400 ml-2">{formatPercent(sug.score)} match</span>
                        </button>
                      ))}
                    </div>
                  )}

                {/* Search */}
                <div className="relative">
                  <input
                    type="text"
                    aria-label="Search vendors"
                    value={vendorSearch}
                    onChange={e => setVendorSearch(e.target.value)}
                    placeholder="Search vendors..."
                    className="input w-full"
                  />
                  {vendorResults.length > 0 && (
                    <div className="absolute z-10 w-full mt-1 bg-fd-panel border rounded-lg shadow-lg max-h-48 overflow-y-auto">
                      {vendorResults.map(v => (
                        <button
                          key={v.id}
                          onClick={() => {
                            setFormData({ ...formData, vendor_id: v.id });
                            setVendorSearch('');
                            setVendorResults([]);
                          }}
                          className="block w-full text-left px-4 py-2 hover:bg-slate-800"
                        >
                          {v.name} ({v.code})
                        </button>
                      ))}
                    </div>
                  )}
                </div>

                <button
                  onClick={() => setFormData({ ...formData, create_vendor: true })}
                  className="text-sm text-werco-primary hover:underline"
                >
                  + Create new vendor
                </button>
              </div>
            ) : (
              <div className="space-y-3 bg-blue-500/10 border border-blue-500/30 rounded-lg p-4">
                <div className="flex justify-between items-center">
                  <p className="font-medium text-blue-300">Create New Vendor</p>
                  <button
                    onClick={() => setFormData({ ...formData, create_vendor: false })}
                    className="text-sm text-blue-400 hover:underline"
                  >
                    Cancel
                  </button>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <FormField label="Name" required labelClassName="block text-sm font-medium text-slate-300 mb-1">
                    {field => (
                      <input
                        {...field}
                        type="text"
                        value={formData.new_vendor_name}
                        onChange={e => setFormData({ ...formData, new_vendor_name: e.target.value })}
                        className="input w-full"
                      />
                    )}
                  </FormField>
                  <FormField label="Code" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                    {field => (
                      <input
                        {...field}
                        type="text"
                        value={formData.new_vendor_code}
                        onChange={e => setFormData({ ...formData, new_vendor_code: e.target.value })}
                        className="input w-full"
                        placeholder="Auto-generated if blank"
                      />
                    )}
                  </FormField>
                </div>
                <FormField label="Address" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                  {field => (
                    <input
                      {...field}
                      type="text"
                      value={formData.new_vendor_address}
                      onChange={e => setFormData({ ...formData, new_vendor_address: e.target.value })}
                      className="input w-full"
                    />
                  )}
                </FormField>
              </div>
            )}

            {formData.vendor_id && !formData.create_vendor && (
              <div className="mt-3 text-sm text-green-600">Vendor selected (ID: {formData.vendor_id})</div>
            )}
          </div>

          {/* Line Items */}
          <div className="card">
            <h3 className="font-semibold mb-4">Line Items ({lineItems.length})</h3>
            <div className="space-y-4">
              {lineItems.map((item, idx) => (
                <div
                  key={idx}
                  className={`border rounded-xl p-4 ${
                    item.confidence === 'low' ? 'border-amber-500/40 bg-amber-500/10' : 'border-slate-700'
                  }`}
                >
                  <div className="flex justify-between items-start mb-3">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium text-slate-400">Line {item.line_number}</span>
                      <span className={`px-2 py-0.5 rounded text-xs ${getConfidenceBadge(item.confidence)}`}>
                        {item.confidence}
                      </span>
                    </div>
                    <div className="flex items-start gap-3">
                      <div className="text-right">
                        <p className="font-semibold">
                          {formatCurrency(item.line_total || item.qty_ordered * item.unit_price || 0)}
                        </p>
                        <p className="text-xs text-slate-400">
                          {item.qty_ordered} x {formatCurrency(item.unit_price)}
                        </p>
                      </div>
                      <button
                        type="button"
                        onClick={() => removeLine(item.uid)}
                        disabled={lineItems.length === 1}
                        aria-label={`Remove line ${item.line_number}`}
                        title={
                          lineItems.length === 1
                            ? 'A purchase order needs at least one line item'
                            : `Remove line ${item.line_number}`
                        }
                        className="text-slate-400 hover:text-red-400 disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:text-slate-400"
                      >
                        <TrashIcon className="h-4 w-4" />
                      </button>
                    </div>
                  </div>

                  <div className="grid grid-cols-3 gap-4 mb-3">
                    <FormField label="Part Number" labelClassName="block text-xs text-slate-400 mb-1">
                      {field => (
                        <input
                          {...field}
                          type="text"
                          value={item.part_number}
                          onChange={e =>
                            setLineItems(prev =>
                              prev.map((it, i) => (i === idx ? { ...it, part_number: e.target.value } : it))
                            )
                          }
                          className="input w-full text-sm"
                        />
                      )}
                    </FormField>
                    <FormField label="Quantity" labelClassName="block text-xs text-slate-400 mb-1">
                      {field => (
                        <input
                          {...field}
                          type="number"
                          value={item.qty_ordered}
                          onChange={e =>
                            setLineItems(prev =>
                              prev.map((it, i) =>
                                i === idx ? { ...it, qty_ordered: parseFloat(e.target.value) || 0 } : it
                              )
                            )
                          }
                          className="input w-full text-sm"
                        />
                      )}
                    </FormField>
                    <FormField label="Unit Price" labelClassName="block text-xs text-slate-400 mb-1">
                      {field => (
                        <input
                          {...field}
                          type="number"
                          step="0.01"
                          value={item.unit_price}
                          onChange={e =>
                            setLineItems(prev =>
                              prev.map((it, i) =>
                                i === idx ? { ...it, unit_price: parseFloat(e.target.value) || 0 } : it
                              )
                            )
                          }
                          className="input w-full text-sm"
                        />
                      )}
                    </FormField>
                  </div>

                  <div className="mb-3">
                    <FormField label="Description" labelClassName="block text-xs text-slate-400 mb-1">
                      {field => (
                        <input
                          {...field}
                          type="text"
                          value={item.description}
                          onChange={e =>
                            setLineItems(prev =>
                              prev.map((it, i) => (i === idx ? { ...it, description: e.target.value } : it))
                            )
                          }
                          className="input w-full text-sm"
                        />
                      )}
                    </FormField>
                  </div>

                  {/* Part Matching */}
                  <div className="bg-slate-800 rounded-lg p-3">
                    {item.selected_part_id ? (
                      <div className="flex justify-between items-center">
                        <span className="text-sm text-green-600">Matched to part ID: {item.selected_part_id}</span>
                        <button
                          onClick={() =>
                            setLineItems(prev =>
                              prev.map((it, i) => (i === idx ? { ...it, selected_part_id: null } : it))
                            )
                          }
                          className="text-xs text-slate-400 hover:underline"
                        >
                          Change
                        </button>
                      </div>
                    ) : item.create_new_part ? (
                      <div className="space-y-2">
                        <div className="flex justify-between items-center">
                          <span className="text-sm text-blue-600">
                            Will create new part: {effectivePartNumber(item)}
                          </span>
                          <button
                            onClick={() => toggleCreatePart(item.uid)}
                            className="text-xs text-slate-400 hover:underline"
                          >
                            Cancel
                          </button>
                        </div>
                        {item.suggested_part_number && item.part_number !== item.suggested_part_number && (
                          <div className="flex items-center justify-between text-xs text-slate-400">
                            <span>Suggested Werco #:</span>
                            <button
                              type="button"
                              onClick={() =>
                                setLineItems(prev =>
                                  prev.map((it, i) =>
                                    i === idx
                                      ? { ...it, part_number: item.suggested_part_number || it.part_number }
                                      : it
                                  )
                                )
                              }
                              className="text-werco-primary hover:underline"
                            >
                              {item.suggested_part_number}
                            </button>
                          </div>
                        )}
                        <div className="flex items-center gap-4">
                          <span className="text-xs text-slate-400">Part Type:</span>
                          <label className="flex items-center gap-1.5 cursor-pointer">
                            <input
                              type="radio"
                              aria-label="Purchased Part"
                              name={`part-type-${idx}`}
                              checked={item.new_part_type === 'purchased'}
                              onChange={() => setPartType(item.uid, 'purchased')}
                              className="text-werco-navy-600"
                            />
                            <span className="text-sm">Purchased Part</span>
                          </label>
                          <label className="flex items-center gap-1.5 cursor-pointer">
                            <input
                              type="radio"
                              aria-label="Raw Material"
                              name={`part-type-${idx}`}
                              checked={item.new_part_type === 'raw_material'}
                              onChange={() => setPartType(item.uid, 'raw_material')}
                              className="text-werco-navy-600"
                            />
                            <span className="text-sm">Raw Material</span>
                          </label>
                          <label className="flex items-center gap-1.5 cursor-pointer">
                            <input
                              type="radio"
                              aria-label="Hardware"
                              name={`part-type-${idx}`}
                              checked={item.new_part_type === 'hardware'}
                              onChange={() => setPartType(item.uid, 'hardware')}
                              className="text-werco-navy-600"
                            />
                            <span className="text-sm">Hardware</span>
                          </label>
                          <label className="flex items-center gap-1.5 cursor-pointer">
                            <input
                              type="radio"
                              aria-label="Consumable"
                              name={`part-type-${idx}`}
                              checked={item.new_part_type === 'consumable'}
                              onChange={() => setPartType(item.uid, 'consumable')}
                              className="text-werco-navy-600"
                            />
                            <span className="text-sm">Consumable</span>
                          </label>
                        </div>
                      </div>
                    ) : (
                      <div className="space-y-2">
                        {coverageKeys.has(partNumberKey(item)) ? (
                          <div className="flex items-center gap-2">
                            <InformationCircleIcon className="h-4 w-4 text-blue-500" />
                            <span className="text-sm text-blue-600">
                              Same part as another line — will use new part{' '}
                              {(() => {
                                // Show the creator line's number verbatim — that exact
                                // form (first occurrence) is what gets created.
                                const key = partNumberKey(item);
                                const creator = lineItems.find(it => it.create_new_part && partNumberKey(it) === key);
                                return effectivePartNumber(creator || item);
                              })()}{' '}
                              (created once)
                            </span>
                          </div>
                        ) : (
                          <div className="flex items-center gap-2">
                            <ExclamationTriangleIcon className="h-4 w-4 text-amber-500" />
                            <span className="text-sm text-amber-600">Part not matched</span>
                          </div>
                        )}

                        {/* Suggestions */}
                        {item.part_match?.suggestions && item.part_match.suggestions.length > 0 && (
                          <div className="flex flex-wrap gap-2">
                            {item.part_match.suggestions.slice(0, 3).map(sug => (
                              <button
                                key={sug.id}
                                onClick={() => selectPartForLine(item.uid, sug.id, sug.part_number)}
                                className="text-xs px-2 py-1 rounded bg-fd-panel border hover:bg-slate-800"
                              >
                                {sug.part_number} ({formatPercent(sug.score)})
                              </button>
                            ))}
                          </div>
                        )}

                        {/* Search */}
                        <div className="relative">
                          <input
                            type="text"
                            aria-label="Search parts"
                            value={partSearches[item.uid] || ''}
                            onChange={e => searchParts(item.uid, e.target.value)}
                            placeholder="Search parts..."
                            className="input w-full text-sm"
                          />
                          {partResults[item.uid]?.length > 0 && (
                            <div className="absolute z-10 w-full mt-1 bg-fd-panel border rounded-lg shadow-lg max-h-32 overflow-y-auto">
                              {partResults[item.uid].map((p: any) => (
                                <button
                                  key={p.id}
                                  onClick={() => selectPartForLine(item.uid, p.id, p.part_number)}
                                  className="block w-full text-left px-3 py-2 text-sm hover:bg-slate-800"
                                >
                                  {p.part_number} - {p.name}
                                </button>
                              ))}
                            </div>
                          )}
                        </div>

                        <button
                          onClick={() => toggleCreatePart(item.uid)}
                          className="text-xs text-werco-primary hover:underline"
                        >
                          + Create as new part
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>

            {/* Totals */}
            <div className="mt-6 pt-4 border-t">
              <div className="flex justify-end">
                <div className="w-64 space-y-2">
                  <div className="flex justify-between text-sm">
                    <span>Subtotal:</span>
                    {/* Mirror the backend's per-line fallback (line_total or qty x price)
                        so the reviewed number matches the PO that gets created. */}
                    <span>
                      {formatCurrency(
                        lineItems.reduce(
                          (sum, item) => sum + (item.line_total || item.qty_ordered * item.unit_price || 0),
                          0
                        )
                      )}
                    </span>
                  </div>
                  {(extractionResult?.tax ?? 0) > 0 && (
                    <div className="flex justify-between text-sm">
                      <span>Tax:</span>
                      <span>{formatCurrency(extractionResult?.tax)}</span>
                    </div>
                  )}
                  <div className="flex justify-between font-semibold">
                    <span>Total:</span>
                    <span>
                      {formatCurrency(
                        lineItems.reduce(
                          (sum, item) => sum + (item.line_total || item.qty_ordered * item.unit_price || 0),
                          0
                        ) +
                          (extractionResult?.tax || 0) +
                          (extractionResult?.shipping_cost || 0)
                      )}
                    </span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* Notes */}
          <div className="card">
            <h3 className="font-semibold mb-4">Notes</h3>
            <textarea
              value={formData.notes}
              onChange={e => setFormData({ ...formData, notes: e.target.value })}
              className="input w-full"
              rows={3}
              aria-label="Notes"
              placeholder="Additional notes..."
            />
          </div>

          {/* Actions */}
          <div className="flex justify-between items-center">
            <button onClick={resetBatch} className="btn-secondary" disabled={creatingPO}>
              Start Over
            </button>
            <div className="flex items-center gap-3">
              <button onClick={handleSkipDocument} className="btn-secondary" disabled={creatingPO}>
                Skip Document
              </button>
              <LoadingButton loading={creatingPO} onClick={handleCreatePO} className="px-8 gap-2">
                <DocumentCheckIcon className="h-5 w-5" />
                Create Purchase Order
              </LoadingButton>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
