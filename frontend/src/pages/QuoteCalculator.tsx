import React, { useEffect, useState, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../services/api';
import DXFViewer from '../components/DXFViewer';
import {
  CalculatorIcon,
  CubeIcon,
  Square3Stack3DIcon,
  CogIcon,
  PlusIcon,
  DocumentArrowUpIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  ClockIcon,
  CurrencyDollarIcon,
  SparklesIcon,
  BoltIcon,
} from '@heroicons/react/24/outline';

interface Material {
  id: number;
  name: string;
  category: string;
  sheet_pricing?: Record<string, number>;
}

interface Finish {
  id: number;
  name: string;
  category: string;
  price_per_part: number;
  price_per_sqft: number;
  minimum_charge: number;
  additional_days: number;
}

interface QuoteResult {
  material_cost: number;
  cutting_cost: number;
  machining_cost: number;
  setup_cost: number;
  bending_cost: number;
  hardware_cost: number;
  finish_cost: number;
  unit_cost: number;
  subtotal: number;
  markup_amount: number;
  quantity_discount: number;
  rush_charge: number;
  total: number;
  unit_price: number;
  estimated_hours: number;
  lead_time_days: number;
  details: Record<string, any>;
}

type CalcType = 'cnc' | 'sheet_metal';

interface DXFAnalysis {
  min_x: number;
  max_x: number;
  min_y: number;
  max_y: number;
  flat_length: number;
  flat_width: number;
  total_cut_length: number;
  num_holes: number;
  num_slots: number;
  num_bends: number;
  hole_diameters: number[];
  bend_lengths: number[];
  layers: string[];
  warnings: string[];
}

const thicknessOptions = [
  { value: '1.000', label: '1" (1.000")' },
  { value: '0.750', label: '3/4" (0.750")' },
  { value: '0.625', label: '5/8" (0.625")' },
  { value: '0.500', label: '1/2" (0.500")' },
  { value: '0.375', label: '3/8" (0.375")' },
  { value: '0.250', label: '1/4" (0.250")' },
  { value: '0.1875', label: '3/16" (0.1875")' },
  { value: '7ga', label: '7ga (0.1793")' },
  { value: '0.125', label: '1/8" (0.125")' },
  { value: '10ga', label: '10ga (0.1345")' },
  { value: '11ga', label: '11ga (0.1196")' },
  { value: '12ga', label: '12ga (0.1046")' },
  { value: '14ga', label: '14ga (0.0747")' },
  { value: '16ga', label: '16ga (0.0598")' },
  { value: '18ga', label: '18ga (0.0478")' },
  { value: '20ga', label: '20ga (0.0359")' },
  { value: '22ga', label: '22ga (0.0299")' },
  { value: '24ga', label: '24ga (0.0239")' },
];

export default function QuoteCalculator() {
  const navigate = useNavigate();
  const [calcType, setCalcType] = useState<CalcType>('cnc');
  const [materials, setMaterials] = useState<Material[]>([]);
  const [finishes, setFinishes] = useState<Finish[]>([]);
  const [loading, setLoading] = useState(true);
  const [calculating, setCalculating] = useState(false);
  const [result, setResult] = useState<QuoteResult | null>(null);
  const [error, setError] = useState('');
  const [dxfFile, setDxfFile] = useState<File | null>(null);
  const [dxfAnalysis, setDxfAnalysis] = useState<DXFAnalysis | null>(null);
  const [analyzingDxf, setAnalyzingDxf] = useState(false);

  // CNC Form
  const [cncForm, setCncForm] = useState({
    length: 4,
    width: 3,
    height: 1,
    material_id: 0,
    num_setups: 1,
    complexity: 'medium',
    num_holes: 0,
    num_tapped_holes: 0,
    num_pockets: 0,
    num_slots: 0,
    tightest_tolerance: 'standard',
    surface_finish: 'as_machined',
    finish_ids: [] as number[],
    quantity: 1,
    rush: false
  });

  // Sheet Metal Form
  const [sheetForm, setSheetForm] = useState({
    flat_length: 12,
    flat_width: 8,
    material_id: 0,
    gauge: '16ga',
    cut_perimeter: 40,
    num_holes: 4,
    num_slots: 0,
    num_bends: 2,
    num_unique_bends: 1,
    num_pem_inserts: 0,
    num_weld_nuts: 0,
    finish_ids: [] as number[],
    quantity: 1,
    rush: false
  });

  const loadData = useCallback(async () => {
    try {
      const [materialsRes, finishesRes] = await Promise.all([
        api.getQuoteMaterials(),
        api.getQuoteFinishes()
      ]);
      setMaterials(materialsRes);
      setFinishes(finishesRes);
      
      if (materialsRes.length > 0) {
        setCncForm(f => ({ ...f, material_id: materialsRes[0].id }));
        setSheetForm(f => ({ ...f, material_id: materialsRes[0].id }));
      }
    } catch (err: any) {
      if (err.response?.status === 404) {
        try {
          await api.seedQuoteDefaults();
          // Reload after seeding
          const [mats, fins] = await Promise.all([
            api.getQuoteMaterials(),
            api.getQuoteFinishes()
          ]);
          setMaterials(mats);
          setFinishes(fins);
          return;
        } catch (e) {
          console.error('Failed to seed defaults:', e);
        }
      }
      setError('Failed to load configuration. Please seed default data.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const handleDxfUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    
    setDxfFile(file);
    setAnalyzingDxf(true);
    setError('');
    setDxfAnalysis(null);
    
    try {
      const analysis = await api.analyzeDXF(file);
      setDxfAnalysis(analysis);
      
      setSheetForm(prev => ({
        ...prev,
        flat_length: Math.round(analysis.flat_length * 100) / 100,
        flat_width: Math.round(analysis.flat_width * 100) / 100,
        cut_perimeter: Math.round(analysis.total_cut_length * 100) / 100,
        num_holes: analysis.num_holes,
        num_slots: analysis.num_slots,
        num_bends: analysis.num_bends,
        num_unique_bends: Math.max(1, Math.ceil(analysis.num_bends / 2)),
      }));
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to analyze DXF file');
      setDxfFile(null);
    } finally {
      setAnalyzingDxf(false);
    }
  };

  const clearDxf = () => {
    setDxfFile(null);
    setDxfAnalysis(null);
  };

  const calculateQuote = async () => {
    setCalculating(true);
    setError('');
    setResult(null);

    try {
      let response;
      if (calcType === 'cnc') {
        response = await api.calculateCNCQuote(cncForm);
      } else {
        response = await api.calculateSheetMetalQuote(sheetForm);
      }
      setResult(response);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Calculation failed');
    } finally {
      setCalculating(false);
    }
  };

  const toggleFinish = (finishId: number) => {
    if (calcType === 'cnc') {
      const current = cncForm.finish_ids;
      if (current.includes(finishId)) {
        setCncForm({ ...cncForm, finish_ids: current.filter(id => id !== finishId) });
      } else {
        setCncForm({ ...cncForm, finish_ids: [...current, finishId] });
      }
    } else {
      const current = sheetForm.finish_ids;
      if (current.includes(finishId)) {
        setSheetForm({ ...sheetForm, finish_ids: current.filter(id => id !== finishId) });
      } else {
        setSheetForm({ ...sheetForm, finish_ids: [...current, finishId] });
      }
    }
  };

  const createQuoteFromResult = () => {
    navigate('/quotes');
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-center">
          <div className="spinner h-12 w-12 mx-auto mb-4"></div>
          <p className="text-slate-500">Loading calculator...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-800 flex items-center gap-3">
            <div className="p-2 rounded-xl bg-gradient-to-br from-rose-500 to-red-600 text-white">
              <CalculatorIcon className="h-6 w-6" />
            </div>
            Instant Quote Calculator
          </h1>
          <p className="text-slate-500 mt-1">Generate accurate quotes in seconds with AI-powered pricing</p>
        </div>
        <button
          onClick={() => navigate('/quote-config')}
          className="btn-secondary"
        >
          <CogIcon className="h-5 w-5 mr-2" />
          Configure Pricing
        </button>
      </div>

      {/* Calculator Type Selector */}
      <div className="grid grid-cols-2 gap-4">
        <button
          onClick={() => { setCalcType('cnc'); setResult(null); }}
          className={`group relative p-6 rounded-2xl border-2 transition-all duration-300 overflow-hidden ${
            calcType === 'cnc'
              ? 'border-rose-500 bg-gradient-to-br from-rose-50 to-red-50'
              : 'border-slate-200 bg-white hover:border-slate-300 hover:shadow-lg'
          }`}
        >
          {calcType === 'cnc' && (
            <div className="absolute top-0 left-0 right-0 h-1 bg-gradient-to-r from-rose-500 to-red-600" />
          )}
          <div className={`w-14 h-14 mx-auto mb-3 rounded-2xl flex items-center justify-center transition-all duration-300 ${
            calcType === 'cnc' 
              ? 'bg-gradient-to-br from-rose-500 to-red-600 text-white shadow-lg' 
              : 'bg-slate-100 text-slate-400 group-hover:bg-slate-200'
          }`}>
            <CubeIcon className="h-7 w-7" />
          </div>
          <h3 className={`text-lg font-semibold text-center transition-colors ${
            calcType === 'cnc' ? 'text-rose-700' : 'text-slate-700'
          }`}>
            CNC Machining
          </h3>
          <p className="text-sm text-slate-500 text-center mt-1">Mills, Lathes, 3/4/5-axis</p>
        </button>

        <button
          onClick={() => { setCalcType('sheet_metal'); setResult(null); }}
          className={`group relative p-6 rounded-2xl border-2 transition-all duration-300 overflow-hidden ${
            calcType === 'sheet_metal'
              ? 'border-rose-500 bg-gradient-to-br from-rose-50 to-red-50'
              : 'border-slate-200 bg-white hover:border-slate-300 hover:shadow-lg'
          }`}
        >
          {calcType === 'sheet_metal' && (
            <div className="absolute top-0 left-0 right-0 h-1 bg-gradient-to-r from-rose-500 to-red-600" />
          )}
          <div className={`w-14 h-14 mx-auto mb-3 rounded-2xl flex items-center justify-center transition-all duration-300 ${
            calcType === 'sheet_metal' 
              ? 'bg-gradient-to-br from-rose-500 to-red-600 text-white shadow-lg' 
              : 'bg-slate-100 text-slate-400 group-hover:bg-slate-200'
          }`}>
            <Square3Stack3DIcon className="h-7 w-7" />
          </div>
          <h3 className={`text-lg font-semibold text-center transition-colors ${
            calcType === 'sheet_metal' ? 'text-rose-700' : 'text-slate-700'
          }`}>
            Sheet Metal
          </h3>
          <p className="text-sm text-slate-500 text-center mt-1">Laser, Brake, Hardware</p>
        </button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Input Form */}
        <div className="card">
          <h2 className="text-lg font-semibold mb-6 flex items-center text-slate-800">
            <div className="p-1.5 rounded-lg bg-rose-100 text-rose-600 mr-2">
              <CalculatorIcon className="h-5 w-5" />
            </div>
            {calcType === 'cnc' ? 'CNC Part Details' : 'Sheet Metal Details'}
          </h2>

          {calcType === 'cnc' ? (
            <div className="space-y-5">
              {/* Part Dimensions */}
              <div>
                <label className="label">Part Dimensions (inches)</label>
                <div className="grid grid-cols-3 gap-3">
                  <div>
                    <input
                      type="number"
                      value={cncForm.length}
                      onChange={(e) => setCncForm({ ...cncForm, length: parseFloat(e.target.value) || 0 })}
                      className="input text-center"
                      step="0.1"
                    />
                    <span className="text-xs text-slate-500 block text-center mt-1">Length</span>
                  </div>
                  <div>
                    <input
                      type="number"
                      value={cncForm.width}
                      onChange={(e) => setCncForm({ ...cncForm, width: parseFloat(e.target.value) || 0 })}
                      className="input text-center"
                      step="0.1"
                    />
                    <span className="text-xs text-slate-500 block text-center mt-1">Width</span>
                  </div>
                  <div>
                    <input
                      type="number"
                      value={cncForm.height}
                      onChange={(e) => setCncForm({ ...cncForm, height: parseFloat(e.target.value) || 0 })}
                      className="input text-center"
                      step="0.1"
                    />
                    <span className="text-xs text-slate-500 block text-center mt-1">Height</span>
                  </div>
                </div>
              </div>

              {/* Material */}
              <div>
                <label className="label">Material</label>
                <select
                  value={cncForm.material_id}
                  onChange={(e) => setCncForm({ ...cncForm, material_id: parseInt(e.target.value) })}
                  className="input"
                >
                  {materials.filter(m => !m.sheet_pricing || Object.keys(m.sheet_pricing).length === 0 || m.category !== 'steel').map(m => (
                    <option key={m.id} value={m.id}>{m.name}</option>
                  ))}
                </select>
              </div>

              {/* Complexity */}
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Complexity</label>
                  <select
                    value={cncForm.complexity}
                    onChange={(e) => setCncForm({ ...cncForm, complexity: e.target.value })}
                    className="input"
                  >
                    <option value="simple">Simple (basic shapes)</option>
                    <option value="medium">Medium (typical part)</option>
                    <option value="complex">Complex (many features)</option>
                    <option value="very_complex">Very Complex (5-axis)</option>
                  </select>
                </div>
                <div>
                  <label className="label"># of Setups</label>
                  <input
                    type="number"
                    value={cncForm.num_setups}
                    onChange={(e) => setCncForm({ ...cncForm, num_setups: parseInt(e.target.value) || 1 })}
                    className="input"
                    min={1}
                    max={6}
                  />
                </div>
              </div>

              {/* Features */}
              <div>
                <label className="label">Features</label>
                <div className="grid grid-cols-4 gap-3">
                  <div>
                    <input
                      type="number"
                      value={cncForm.num_holes}
                      onChange={(e) => setCncForm({ ...cncForm, num_holes: parseInt(e.target.value) || 0 })}
                      className="input text-center"
                      min={0}
                    />
                    <span className="text-xs text-slate-500 block text-center mt-1">Holes</span>
                  </div>
                  <div>
                    <input
                      type="number"
                      value={cncForm.num_tapped_holes}
                      onChange={(e) => setCncForm({ ...cncForm, num_tapped_holes: parseInt(e.target.value) || 0 })}
                      className="input text-center"
                      min={0}
                    />
                    <span className="text-xs text-slate-500 block text-center mt-1">Tapped</span>
                  </div>
                  <div>
                    <input
                      type="number"
                      value={cncForm.num_pockets}
                      onChange={(e) => setCncForm({ ...cncForm, num_pockets: parseInt(e.target.value) || 0 })}
                      className="input text-center"
                      min={0}
                    />
                    <span className="text-xs text-slate-500 block text-center mt-1">Pockets</span>
                  </div>
                  <div>
                    <input
                      type="number"
                      value={cncForm.num_slots}
                      onChange={(e) => setCncForm({ ...cncForm, num_slots: parseInt(e.target.value) || 0 })}
                      className="input text-center"
                      min={0}
                    />
                    <span className="text-xs text-slate-500 block text-center mt-1">Slots</span>
                  </div>
                </div>
              </div>

              {/* Tolerance & Surface */}
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Tightest Tolerance</label>
                  <select
                    value={cncForm.tightest_tolerance}
                    onChange={(e) => setCncForm({ ...cncForm, tightest_tolerance: e.target.value })}
                    className="input"
                  >
                    <option value="standard">Standard (+/-.005)</option>
                    <option value="tight">Tight (+/-.002)</option>
                    <option value="precision">Precision (+/-.001)</option>
                    <option value="ultra">Ultra (+/-.0005)</option>
                  </select>
                </div>
                <div>
                  <label className="label">Surface Finish</label>
                  <select
                    value={cncForm.surface_finish}
                    onChange={(e) => setCncForm({ ...cncForm, surface_finish: e.target.value })}
                    className="input"
                  >
                    <option value="as_machined">As Machined</option>
                    <option value="light_deburr">Light Deburr</option>
                    <option value="smooth">Smooth (125 Ra)</option>
                    <option value="mirror">Mirror (32 Ra)</option>
                  </select>
                </div>
              </div>

              {/* Quantity & Rush */}
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Quantity</label>
                  <input
                    type="number"
                    value={cncForm.quantity}
                    onChange={(e) => setCncForm({ ...cncForm, quantity: parseInt(e.target.value) || 1 })}
                    className="input"
                    min={1}
                  />
                </div>
                <div className="flex items-end pb-1">
                  <label className="relative flex items-center cursor-pointer group">
                    <input
                      type="checkbox"
                      checked={cncForm.rush}
                      onChange={(e) => setCncForm({ ...cncForm, rush: e.target.checked })}
                      className="sr-only peer"
                    />
                    <div className="w-11 h-6 bg-slate-200 peer-focus:outline-none peer-focus:ring-4 peer-focus:ring-rose-300 rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-gradient-to-r peer-checked:from-amber-500 peer-checked:to-orange-500"></div>
                    <span className="ml-3 text-sm font-medium text-slate-700 flex items-center gap-1.5">
                      <BoltIcon className="h-4 w-4 text-amber-500" />
                      Rush Order (1.5x)
                    </span>
                  </label>
                </div>
              </div>
            </div>
          ) : (
            /* Sheet Metal Form */
            <div className="space-y-5">
              {/* DXF Upload */}
              <div className={`border-2 border-dashed rounded-2xl p-5 text-center transition-all duration-300 ${
                dxfFile ? 'border-rose-300 bg-rose-50/50' : 'border-slate-300 hover:border-rose-400 hover:bg-rose-50/30'
              }`}>
                {!dxfFile ? (
                  <label className="cursor-pointer block">
                    <input
                      type="file"
                      accept=".dxf,.DXF"
                      onChange={handleDxfUpload}
                      className="hidden"
                    />
                    <div className="w-14 h-14 mx-auto mb-3 rounded-2xl bg-gradient-to-br from-slate-100 to-slate-200 flex items-center justify-center">
                      <DocumentArrowUpIcon className="h-7 w-7 text-slate-400" />
                    </div>
                    <p className="text-sm font-medium text-slate-700">Upload DXF File</p>
                    <p className="text-xs text-slate-500 mt-1">Auto-extract cut length, holes, bends</p>
                  </label>
                ) : analyzingDxf ? (
                  <div className="py-4">
                    <div className="spinner h-8 w-8 mx-auto"></div>
                    <p className="text-sm text-slate-600 mt-3">Analyzing DXF...</p>
                  </div>
                ) : dxfAnalysis ? (
                  <div className="text-left">
                    <div className="flex items-center justify-between mb-3">
                      <div className="flex items-center text-rose-600">
                        <CheckCircleIcon className="h-5 w-5 mr-2" />
                        <span className="font-medium">{dxfFile.name}</span>
                      </div>
                      <button onClick={clearDxf} className="text-red-500 text-sm hover:underline font-medium">Clear</button>
                    </div>
                    <div className="mb-3">
                      <DXFViewer 
                        file={dxfFile} 
                        analysis={{
                          min_x: dxfAnalysis.min_x || 0,
                          max_x: dxfAnalysis.max_x || dxfAnalysis.flat_length,
                          min_y: dxfAnalysis.min_y || 0,
                          max_y: dxfAnalysis.max_y || dxfAnalysis.flat_width,
                          flat_length: dxfAnalysis.flat_length,
                          flat_width: dxfAnalysis.flat_width
                        }}
                      />
                    </div>
                    <div className="bg-gradient-to-br from-rose-50 to-red-50 rounded-xl p-4 border border-rose-200">
                      <p className="font-medium text-rose-800 mb-2 flex items-center gap-2">
                        <SparklesIcon className="h-4 w-4" />
                        Extracted from DXF
                      </p>
                      <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm text-rose-700">
                        <span>Flat Size: {dxfAnalysis.flat_length}" x {dxfAnalysis.flat_width}"</span>
                        <span>Cut Length: {dxfAnalysis.total_cut_length}"</span>
                        <span>Holes: {dxfAnalysis.num_holes}</span>
                        <span>Slots: {dxfAnalysis.num_slots}</span>
                        <span>Bends: {dxfAnalysis.num_bends}</span>
                        <span>Layers: {dxfAnalysis.layers.length}</span>
                      </div>
                      {dxfAnalysis.warnings.length > 0 && (
                        <div className="mt-3 text-amber-600 flex items-start bg-amber-50 rounded-lg p-2">
                          <ExclamationTriangleIcon className="h-4 w-4 mr-1.5 flex-shrink-0 mt-0.5" />
                          <span className="text-xs">{dxfAnalysis.warnings.join('; ')}</span>
                        </div>
                      )}
                    </div>
                  </div>
                ) : null}
              </div>

              {/* Flat Pattern */}
              <div>
                <label className="label flex items-center gap-2">
                  Flat Pattern Size (inches)
                  {dxfAnalysis && <span className="text-xs text-rose-600 bg-rose-100 px-2 py-0.5 rounded-full">from DXF</span>}
                </label>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <input
                      type="number"
                      value={sheetForm.flat_length}
                      onChange={(e) => setSheetForm({ ...sheetForm, flat_length: parseFloat(e.target.value) || 0 })}
                      className="input text-center"
                      step="0.1"
                    />
                    <span className="text-xs text-slate-500 block text-center mt-1">Length</span>
                  </div>
                  <div>
                    <input
                      type="number"
                      value={sheetForm.flat_width}
                      onChange={(e) => setSheetForm({ ...sheetForm, flat_width: parseFloat(e.target.value) || 0 })}
                      className="input text-center"
                      step="0.1"
                    />
                    <span className="text-xs text-slate-500 block text-center mt-1">Width</span>
                  </div>
                </div>
              </div>

              {/* Material & Thickness */}
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Material</label>
                  <select
                    value={sheetForm.material_id}
                    onChange={(e) => setSheetForm({ ...sheetForm, material_id: parseInt(e.target.value) })}
                    className="input"
                  >
                    {materials.map(m => (
                      <option key={m.id} value={m.id}>{m.name}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="label">Thickness</label>
                  <select
                    value={sheetForm.gauge}
                    onChange={(e) => setSheetForm({ ...sheetForm, gauge: e.target.value })}
                    className="input"
                  >
                    {thicknessOptions.map(t => (
                      <option key={t.value} value={t.value}>{t.label}</option>
                    ))}
                  </select>
                </div>
              </div>

              {/* Cutting */}
              <div>
                <label className="label flex items-center gap-2">
                  Cutting
                  {dxfAnalysis && <span className="text-xs text-rose-600 bg-rose-100 px-2 py-0.5 rounded-full">from DXF</span>}
                </label>
                <div className="grid grid-cols-3 gap-3">
                  <div>
                    <input
                      type="number"
                      value={sheetForm.cut_perimeter}
                      onChange={(e) => setSheetForm({ ...sheetForm, cut_perimeter: parseFloat(e.target.value) || 0 })}
                      className="input text-center"
                      step="1"
                    />
                    <span className="text-xs text-slate-500 block text-center mt-1">Cut Length (in)</span>
                  </div>
                  <div>
                    <input
                      type="number"
                      value={sheetForm.num_holes}
                      onChange={(e) => setSheetForm({ ...sheetForm, num_holes: parseInt(e.target.value) || 0 })}
                      className="input text-center"
                      min={0}
                    />
                    <span className="text-xs text-slate-500 block text-center mt-1">Holes</span>
                  </div>
                  <div>
                    <input
                      type="number"
                      value={sheetForm.num_slots}
                      onChange={(e) => setSheetForm({ ...sheetForm, num_slots: parseInt(e.target.value) || 0 })}
                      className="input text-center"
                      min={0}
                    />
                    <span className="text-xs text-slate-500 block text-center mt-1">Slots</span>
                  </div>
                </div>
              </div>

              {/* Bending */}
              <div>
                <label className="label flex items-center gap-2">
                  Bending
                  {dxfAnalysis && <span className="text-xs text-rose-600 bg-rose-100 px-2 py-0.5 rounded-full">from DXF</span>}
                </label>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <input
                      type="number"
                      value={sheetForm.num_bends}
                      onChange={(e) => setSheetForm({ ...sheetForm, num_bends: parseInt(e.target.value) || 0 })}
                      className="input text-center"
                      min={0}
                    />
                    <span className="text-xs text-slate-500 block text-center mt-1">Total Bends</span>
                  </div>
                  <div>
                    <input
                      type="number"
                      value={sheetForm.num_unique_bends}
                      onChange={(e) => setSheetForm({ ...sheetForm, num_unique_bends: parseInt(e.target.value) || 0 })}
                      className="input text-center"
                      min={0}
                    />
                    <span className="text-xs text-slate-500 block text-center mt-1">Unique Bends</span>
                  </div>
                </div>
              </div>

              {/* Hardware */}
              <div>
                <label className="label">Hardware Insertion</label>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <input
                      type="number"
                      value={sheetForm.num_pem_inserts}
                      onChange={(e) => setSheetForm({ ...sheetForm, num_pem_inserts: parseInt(e.target.value) || 0 })}
                      className="input text-center"
                      min={0}
                    />
                    <span className="text-xs text-slate-500 block text-center mt-1">PEM Inserts</span>
                  </div>
                  <div>
                    <input
                      type="number"
                      value={sheetForm.num_weld_nuts}
                      onChange={(e) => setSheetForm({ ...sheetForm, num_weld_nuts: parseInt(e.target.value) || 0 })}
                      className="input text-center"
                      min={0}
                    />
                    <span className="text-xs text-slate-500 block text-center mt-1">Weld Nuts</span>
                  </div>
                </div>
              </div>

              {/* Quantity & Rush */}
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Quantity</label>
                  <input
                    type="number"
                    value={sheetForm.quantity}
                    onChange={(e) => setSheetForm({ ...sheetForm, quantity: parseInt(e.target.value) || 1 })}
                    className="input"
                    min={1}
                  />
                </div>
                <div className="flex items-end pb-1">
                  <label className="relative flex items-center cursor-pointer group">
                    <input
                      type="checkbox"
                      checked={sheetForm.rush}
                      onChange={(e) => setSheetForm({ ...sheetForm, rush: e.target.checked })}
                      className="sr-only peer"
                    />
                    <div className="w-11 h-6 bg-slate-200 peer-focus:outline-none peer-focus:ring-4 peer-focus:ring-rose-300 rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-gradient-to-r peer-checked:from-amber-500 peer-checked:to-orange-500"></div>
                    <span className="ml-3 text-sm font-medium text-slate-700 flex items-center gap-1.5">
                      <BoltIcon className="h-4 w-4 text-amber-500" />
                      Rush Order (1.5x)
                    </span>
                  </label>
                </div>
              </div>
            </div>
          )}

          {/* Finishes */}
          <div className="mt-6 pt-6 border-t border-slate-100">
            <label className="label">Finishing (optional)</label>
            <div className="flex flex-wrap gap-2">
              {finishes.map(f => {
                const isSelected = (calcType === 'cnc' ? cncForm.finish_ids : sheetForm.finish_ids).includes(f.id);
                return (
                  <button
                    key={f.id}
                    type="button"
                    onClick={() => toggleFinish(f.id)}
                    className={`px-4 py-2 rounded-xl text-sm font-medium border-2 transition-all duration-200 ${
                      isSelected
                        ? 'border-rose-500 bg-gradient-to-r from-rose-500 to-red-600 text-white shadow-md'
                        : 'border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50'
                    }`}
                  >
                    {f.name}
                  </button>
                );
              })}
            </div>
          </div>

          {/* Calculate Button */}
          <button
            onClick={calculateQuote}
            disabled={calculating}
            className="btn-primary w-full mt-6 py-4 text-lg"
          >
            {calculating ? (
              <span className="flex items-center justify-center gap-3">
                <div className="spinner h-5 w-5 border-white/30 border-t-white"></div>
                Calculating...
              </span>
            ) : (
              <span className="flex items-center justify-center gap-2">
                <CalculatorIcon className="h-5 w-5" />
                Calculate Quote
              </span>
            )}
          </button>

          {error && (
            <div className="mt-4 bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-xl flex items-center gap-2">
              <ExclamationTriangleIcon className="h-5 w-5 flex-shrink-0" />
              {error}
            </div>
          )}
        </div>

        {/* Results */}
        <div className="card">
          <h2 className="text-lg font-semibold mb-6 flex items-center text-slate-800">
            <div className="p-1.5 rounded-lg bg-rose-100 text-rose-600 mr-2">
              <CurrencyDollarIcon className="h-5 w-5" />
            </div>
            Quote Result
          </h2>
          
          {!result ? (
            <div className="text-center py-16">
              <div className="w-20 h-20 mx-auto mb-4 rounded-2xl bg-gradient-to-br from-slate-100 to-slate-200 flex items-center justify-center">
                <CalculatorIcon className="h-10 w-10 text-slate-300" />
              </div>
              <p className="text-slate-500 font-medium">Enter part details and click Calculate</p>
              <p className="text-slate-400 text-sm mt-1">Your instant quote will appear here</p>
            </div>
          ) : (
            <div className="space-y-5">
              {/* Big Price Card */}
              <div className="relative overflow-hidden rounded-2xl p-6 text-center">
                <div className="absolute inset-0 bg-gradient-to-br from-slate-900 via-slate-800 to-blue-900"></div>
                <div className="absolute inset-0 opacity-30">
                  <svg className="w-full h-full" xmlns="http://www.w3.org/2000/svg">
                    <defs>
                      <pattern id="price-pattern" width="40" height="40" patternUnits="userSpaceOnUse">
                        <path d="M20 0L40 20L20 40L0 20Z" fill="none" stroke="currentColor" strokeWidth="0.5" className="text-rose-400"/>
                      </pattern>
                    </defs>
                    <rect width="100%" height="100%" fill="url(#price-pattern)" />
                  </svg>
                </div>
                <div className="relative z-10">
                  <p className="text-rose-400 text-sm font-medium">Total Quote</p>
                  <p className="text-5xl font-bold text-white mt-2">${result.total.toLocaleString(undefined, { minimumFractionDigits: 2 })}</p>
                  <p className="text-slate-400 mt-3">
                    <span className="text-rose-400 font-semibold">${result.unit_price.toFixed(2)}</span> per unit x {calcType === 'cnc' ? cncForm.quantity : sheetForm.quantity}
                  </p>
                </div>
              </div>

              {/* Lead Time */}
              <div className="flex items-center justify-between p-4 bg-gradient-to-r from-slate-50 to-rose-50 rounded-xl border border-slate-200">
                <div className="flex items-center gap-3">
                  <div className="p-2 rounded-lg bg-rose-100">
                    <ClockIcon className="h-5 w-5 text-rose-600" />
                  </div>
                  <span className="font-medium text-slate-700">Estimated Lead Time</span>
                </div>
                <span className="text-xl font-bold text-rose-600">{result.lead_time_days} days</span>
              </div>

              {/* Cost Breakdown */}
              <div className="rounded-xl border border-slate-200 overflow-hidden">
                <div className="bg-gradient-to-r from-slate-100 to-slate-50 px-4 py-3 font-semibold text-slate-700">
                  Cost Breakdown
                </div>
                <div className="divide-y divide-slate-100">
                  {result.material_cost > 0 && (
                    <div className="flex justify-between px-4 py-3 text-sm">
                      <span className="text-slate-600">Material</span>
                      <span className="font-medium text-slate-800">${result.material_cost.toFixed(2)}</span>
                    </div>
                  )}
                  {result.cutting_cost > 0 && (
                    <div className="flex justify-between px-4 py-3 text-sm">
                      <span className="text-slate-600">Laser Cutting</span>
                      <span className="font-medium text-slate-800">${result.cutting_cost.toFixed(2)}</span>
                    </div>
                  )}
                  {result.machining_cost > 0 && (
                    <div className="flex justify-between px-4 py-3 text-sm">
                      <span className="text-slate-600">Machining</span>
                      <span className="font-medium text-slate-800">${result.machining_cost.toFixed(2)}</span>
                    </div>
                  )}
                  {result.setup_cost > 0 && (
                    <div className="flex justify-between px-4 py-3 text-sm">
                      <span className="text-slate-600">Setup</span>
                      <span className="font-medium text-slate-800">${result.setup_cost.toFixed(2)}</span>
                    </div>
                  )}
                  {result.bending_cost > 0 && (
                    <div className="flex justify-between px-4 py-3 text-sm">
                      <span className="text-slate-600">Bending</span>
                      <span className="font-medium text-slate-800">${result.bending_cost.toFixed(2)}</span>
                    </div>
                  )}
                  {result.hardware_cost > 0 && (
                    <div className="flex justify-between px-4 py-3 text-sm">
                      <span className="text-slate-600">Hardware</span>
                      <span className="font-medium text-slate-800">${result.hardware_cost.toFixed(2)}</span>
                    </div>
                  )}
                  {result.finish_cost > 0 && (
                    <div className="flex justify-between px-4 py-3 text-sm">
                      <span className="text-slate-600">Finishing</span>
                      <span className="font-medium text-slate-800">${result.finish_cost.toFixed(2)}</span>
                    </div>
                  )}
                  <div className="flex justify-between px-4 py-3 bg-slate-50">
                    <span className="font-semibold text-slate-700">Subtotal</span>
                    <span className="font-semibold text-slate-800">${result.subtotal.toFixed(2)}</span>
                  </div>
                  <div className="flex justify-between px-4 py-3 text-sm">
                    <span className="text-slate-600">Markup (25%)</span>
                    <span className="font-medium text-slate-800">${result.markup_amount.toFixed(2)}</span>
                  </div>
                  {result.quantity_discount > 0 && (
                    <div className="flex justify-between px-4 py-3 text-sm bg-emerald-50">
                      <span className="text-emerald-700">Quantity Discount</span>
                      <span className="font-medium text-emerald-700">-${result.quantity_discount.toFixed(2)}</span>
                    </div>
                  )}
                  {result.rush_charge > 0 && (
                    <div className="flex justify-between px-4 py-3 text-sm bg-amber-50">
                      <span className="text-amber-700 flex items-center gap-1">
                        <BoltIcon className="h-4 w-4" />
                        Rush Charge
                      </span>
                      <span className="font-medium text-amber-700">+${result.rush_charge.toFixed(2)}</span>
                    </div>
                  )}
                </div>
              </div>

              {/* Details */}
              {result.details && Object.keys(result.details).length > 0 && (
                <div className="text-sm text-slate-500 space-y-1.5 bg-slate-50 rounded-xl p-4">
                  <p className="font-medium text-slate-700 mb-2">Calculation Details</p>
                  {Object.entries(result.details).map(([key, value]) => (
                    <p key={key} className="flex justify-between">
                      <span className="capitalize">{key.replace(/_/g, ' ')}</span>
                      <span className="text-slate-600">{typeof value === 'number' ? value.toFixed(2) : value}</span>
                    </p>
                  ))}
                </div>
              )}

              {/* Actions */}
              <div className="flex gap-3 pt-4 border-t border-slate-100">
                <button onClick={createQuoteFromResult} className="btn-primary flex-1">
                  <PlusIcon className="h-5 w-5 mr-2" />
                  Create Quote
                </button>
                <button 
                  onClick={() => window.print()}
                  className="btn-secondary"
                >
                  Print
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
