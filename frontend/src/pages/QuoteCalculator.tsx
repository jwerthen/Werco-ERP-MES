import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../services/api';
import DXFViewer from '../components/DXFViewer';
import {
  CalculatorIcon,
  CubeIcon,
  Square3Stack3DIcon,
  CogIcon,
  PlusIcon,
  ArrowRightIcon,
  DocumentArrowUpIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
} from '@heroicons/react/24/outline';

interface Material {
  id: number;
  name: string;
  category: string;
  sheet_pricing?: Record<string, number>;
}

interface Machine {
  id: number;
  name: string;
  machine_type: string;
  rate_per_hour: number;
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

  useEffect(() => {
    loadData();
  }, []);

  const loadData = async () => {
    try {
      const [materialsRes, finishesRes] = await Promise.all([
        api.getQuoteMaterials(),
        api.getQuoteFinishes()
      ]);
      setMaterials(materialsRes);
      setFinishes(finishesRes);
      
      // Set default material
      if (materialsRes.length > 0) {
        setCncForm(f => ({ ...f, material_id: materialsRes[0].id }));
        setSheetForm(f => ({ ...f, material_id: materialsRes[0].id }));
      }
    } catch (err: any) {
      // If no data, try seeding
      if (err.response?.status === 404 || materials.length === 0) {
        try {
          await api.seedQuoteDefaults();
          loadData();
          return;
        } catch (e) {
          console.error('Failed to seed defaults:', e);
        }
      }
      setError('Failed to load configuration. Please seed default data.');
    } finally {
      setLoading(false);
    }
  };

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
      
      // Auto-fill sheet metal form with extracted values
      setSheetForm(prev => ({
        ...prev,
        flat_length: Math.round(analysis.flat_length * 100) / 100,
        flat_width: Math.round(analysis.flat_width * 100) / 100,
        cut_perimeter: Math.round(analysis.total_cut_length * 100) / 100,
        num_holes: analysis.num_holes,
        num_slots: analysis.num_slots,
        num_bends: analysis.num_bends,
        num_unique_bends: Math.max(1, Math.ceil(analysis.num_bends / 2)), // Estimate
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
    // Navigate to quotes page with prefilled data
    // For now just navigate
    navigate('/quotes');
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-werco-primary"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Instant Quote Calculator</h1>
          <p className="text-gray-500">Generate accurate quotes in seconds</p>
        </div>
        <button
          onClick={() => navigate('/quote-config')}
          className="btn-secondary flex items-center"
        >
          <CogIcon className="h-5 w-5 mr-2" />
          Configure Pricing
        </button>
      </div>

      {/* Calculator Type Selector */}
      <div className="flex gap-4">
        <button
          onClick={() => { setCalcType('cnc'); setResult(null); }}
          className={`flex-1 p-6 rounded-lg border-2 transition-all ${
            calcType === 'cnc'
              ? 'border-werco-primary bg-blue-50'
              : 'border-gray-200 hover:border-gray-300'
          }`}
        >
          <CubeIcon className={`h-10 w-10 mx-auto mb-2 ${calcType === 'cnc' ? 'text-werco-primary' : 'text-gray-400'}`} />
          <h3 className={`text-lg font-semibold text-center ${calcType === 'cnc' ? 'text-werco-primary' : 'text-gray-700'}`}>
            CNC Machining
          </h3>
          <p className="text-sm text-gray-500 text-center mt-1">Mills, Lathes, 3/4/5-axis</p>
        </button>
        <button
          onClick={() => { setCalcType('sheet_metal'); setResult(null); }}
          className={`flex-1 p-6 rounded-lg border-2 transition-all ${
            calcType === 'sheet_metal'
              ? 'border-werco-primary bg-blue-50'
              : 'border-gray-200 hover:border-gray-300'
          }`}
        >
          <Square3Stack3DIcon className={`h-10 w-10 mx-auto mb-2 ${calcType === 'sheet_metal' ? 'text-werco-primary' : 'text-gray-400'}`} />
          <h3 className={`text-lg font-semibold text-center ${calcType === 'sheet_metal' ? 'text-werco-primary' : 'text-gray-700'}`}>
            Sheet Metal
          </h3>
          <p className="text-sm text-gray-500 text-center mt-1">Laser, Brake, Hardware</p>
        </button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Input Form */}
        <div className="card">
          <h2 className="text-lg font-semibold mb-4 flex items-center">
            <CalculatorIcon className="h-5 w-5 mr-2 text-werco-primary" />
            {calcType === 'cnc' ? 'CNC Part Details' : 'Sheet Metal Details'}
          </h2>

          {calcType === 'cnc' ? (
            <div className="space-y-4">
              {/* Part Dimensions */}
              <div>
                <label className="label">Part Dimensions (inches)</label>
                <div className="grid grid-cols-3 gap-2">
                  <div>
                    <input
                      type="number"
                      value={cncForm.length}
                      onChange={(e) => setCncForm({ ...cncForm, length: parseFloat(e.target.value) || 0 })}
                      className="input"
                      step="0.1"
                      placeholder="Length"
                    />
                    <span className="text-xs text-gray-500">Length</span>
                  </div>
                  <div>
                    <input
                      type="number"
                      value={cncForm.width}
                      onChange={(e) => setCncForm({ ...cncForm, width: parseFloat(e.target.value) || 0 })}
                      className="input"
                      step="0.1"
                      placeholder="Width"
                    />
                    <span className="text-xs text-gray-500">Width</span>
                  </div>
                  <div>
                    <input
                      type="number"
                      value={cncForm.height}
                      onChange={(e) => setCncForm({ ...cncForm, height: parseFloat(e.target.value) || 0 })}
                      className="input"
                      step="0.1"
                      placeholder="Height"
                    />
                    <span className="text-xs text-gray-500">Height</span>
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
                <div className="grid grid-cols-4 gap-2">
                  <div>
                    <input
                      type="number"
                      value={cncForm.num_holes}
                      onChange={(e) => setCncForm({ ...cncForm, num_holes: parseInt(e.target.value) || 0 })}
                      className="input text-center"
                      min={0}
                    />
                    <span className="text-xs text-gray-500 block text-center">Holes</span>
                  </div>
                  <div>
                    <input
                      type="number"
                      value={cncForm.num_tapped_holes}
                      onChange={(e) => setCncForm({ ...cncForm, num_tapped_holes: parseInt(e.target.value) || 0 })}
                      className="input text-center"
                      min={0}
                    />
                    <span className="text-xs text-gray-500 block text-center">Tapped</span>
                  </div>
                  <div>
                    <input
                      type="number"
                      value={cncForm.num_pockets}
                      onChange={(e) => setCncForm({ ...cncForm, num_pockets: parseInt(e.target.value) || 0 })}
                      className="input text-center"
                      min={0}
                    />
                    <span className="text-xs text-gray-500 block text-center">Pockets</span>
                  </div>
                  <div>
                    <input
                      type="number"
                      value={cncForm.num_slots}
                      onChange={(e) => setCncForm({ ...cncForm, num_slots: parseInt(e.target.value) || 0 })}
                      className="input text-center"
                      min={0}
                    />
                    <span className="text-xs text-gray-500 block text-center">Slots</span>
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
                <div className="flex items-end">
                  <label className="flex items-center">
                    <input
                      type="checkbox"
                      checked={cncForm.rush}
                      onChange={(e) => setCncForm({ ...cncForm, rush: e.target.checked })}
                      className="mr-2 h-5 w-5"
                    />
                    <span className="font-medium">Rush Order (1.5x)</span>
                  </label>
                </div>
              </div>
            </div>
          ) : (
            /* Sheet Metal Form */
            <div className="space-y-4">
              {/* DXF Upload */}
              <div className="border-2 border-dashed border-gray-300 rounded-lg p-4 text-center hover:border-werco-primary transition-colors">
                {!dxfFile ? (
                  <label className="cursor-pointer block">
                    <input
                      type="file"
                      accept=".dxf,.DXF"
                      onChange={handleDxfUpload}
                      className="hidden"
                    />
                    <DocumentArrowUpIcon className="h-10 w-10 mx-auto text-gray-400 mb-2" />
                    <p className="text-sm font-medium text-gray-700">Upload DXF File</p>
                    <p className="text-xs text-gray-500 mt-1">Auto-extract cut length, holes, bends</p>
                  </label>
                ) : analyzingDxf ? (
                  <div className="py-2">
                    <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-werco-primary mx-auto"></div>
                    <p className="text-sm text-gray-600 mt-2">Analyzing DXF...</p>
                  </div>
                ) : dxfAnalysis ? (
                  <div className="text-left">
                    <div className="flex items-center justify-between mb-2">
                      <div className="flex items-center text-green-600">
                        <CheckCircleIcon className="h-5 w-5 mr-2" />
                        <span className="font-medium">{dxfFile.name}</span>
                      </div>
                      <button onClick={clearDxf} className="text-red-500 text-sm hover:underline">Clear</button>
                    </div>
                    {/* DXF Preview */}
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
                    <div className="bg-green-50 rounded p-3 text-sm">
                      <p className="font-medium text-green-800 mb-1">Extracted from DXF:</p>
                      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-green-700">
                        <span>Flat Size: {dxfAnalysis.flat_length}" x {dxfAnalysis.flat_width}"</span>
                        <span>Cut Length: {dxfAnalysis.total_cut_length}"</span>
                        <span>Holes: {dxfAnalysis.num_holes}</span>
                        <span>Slots: {dxfAnalysis.num_slots}</span>
                        <span>Bends: {dxfAnalysis.num_bends}</span>
                        <span>Layers: {dxfAnalysis.layers.length}</span>
                      </div>
                      {dxfAnalysis.warnings.length > 0 && (
                        <div className="mt-2 text-orange-600 flex items-start">
                          <ExclamationTriangleIcon className="h-4 w-4 mr-1 flex-shrink-0 mt-0.5" />
                          <span className="text-xs">{dxfAnalysis.warnings.join('; ')}</span>
                        </div>
                      )}
                    </div>
                  </div>
                ) : null}
              </div>

              {/* Flat Pattern */}
              <div>
                <label className="label">Flat Pattern Size (inches) {dxfAnalysis && <span className="text-green-600 text-xs ml-1">(from DXF)</span>}</label>
                <div className="grid grid-cols-2 gap-2">
                  <div>
                    <input
                      type="number"
                      value={sheetForm.flat_length}
                      onChange={(e) => setSheetForm({ ...sheetForm, flat_length: parseFloat(e.target.value) || 0 })}
                      className="input"
                      step="0.1"
                    />
                    <span className="text-xs text-gray-500">Length</span>
                  </div>
                  <div>
                    <input
                      type="number"
                      value={sheetForm.flat_width}
                      onChange={(e) => setSheetForm({ ...sheetForm, flat_width: parseFloat(e.target.value) || 0 })}
                      className="input"
                      step="0.1"
                    />
                    <span className="text-xs text-gray-500">Width</span>
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
                <label className="label">Cutting {dxfAnalysis && <span className="text-green-600 text-xs ml-1">(from DXF)</span>}</label>
                <div className="grid grid-cols-3 gap-2">
                  <div>
                    <input
                      type="number"
                      value={sheetForm.cut_perimeter}
                      onChange={(e) => setSheetForm({ ...sheetForm, cut_perimeter: parseFloat(e.target.value) || 0 })}
                      className="input text-center"
                      step="1"
                    />
                    <span className="text-xs text-gray-500 block text-center">Cut Length (in)</span>
                  </div>
                  <div>
                    <input
                      type="number"
                      value={sheetForm.num_holes}
                      onChange={(e) => setSheetForm({ ...sheetForm, num_holes: parseInt(e.target.value) || 0 })}
                      className="input text-center"
                      min={0}
                    />
                    <span className="text-xs text-gray-500 block text-center">Holes</span>
                  </div>
                  <div>
                    <input
                      type="number"
                      value={sheetForm.num_slots}
                      onChange={(e) => setSheetForm({ ...sheetForm, num_slots: parseInt(e.target.value) || 0 })}
                      className="input text-center"
                      min={0}
                    />
                    <span className="text-xs text-gray-500 block text-center">Slots</span>
                  </div>
                </div>
              </div>

              {/* Bending */}
              <div>
                <label className="label">Bending {dxfAnalysis && <span className="text-green-600 text-xs ml-1">(from DXF)</span>}</label>
                <div className="grid grid-cols-2 gap-2">
                  <div>
                    <input
                      type="number"
                      value={sheetForm.num_bends}
                      onChange={(e) => setSheetForm({ ...sheetForm, num_bends: parseInt(e.target.value) || 0 })}
                      className="input text-center"
                      min={0}
                    />
                    <span className="text-xs text-gray-500 block text-center">Total Bends</span>
                  </div>
                  <div>
                    <input
                      type="number"
                      value={sheetForm.num_unique_bends}
                      onChange={(e) => setSheetForm({ ...sheetForm, num_unique_bends: parseInt(e.target.value) || 0 })}
                      className="input text-center"
                      min={0}
                    />
                    <span className="text-xs text-gray-500 block text-center">Unique Bends</span>
                  </div>
                </div>
              </div>

              {/* Hardware */}
              <div>
                <label className="label">Hardware Insertion</label>
                <div className="grid grid-cols-2 gap-2">
                  <div>
                    <input
                      type="number"
                      value={sheetForm.num_pem_inserts}
                      onChange={(e) => setSheetForm({ ...sheetForm, num_pem_inserts: parseInt(e.target.value) || 0 })}
                      className="input text-center"
                      min={0}
                    />
                    <span className="text-xs text-gray-500 block text-center">PEM Inserts</span>
                  </div>
                  <div>
                    <input
                      type="number"
                      value={sheetForm.num_weld_nuts}
                      onChange={(e) => setSheetForm({ ...sheetForm, num_weld_nuts: parseInt(e.target.value) || 0 })}
                      className="input text-center"
                      min={0}
                    />
                    <span className="text-xs text-gray-500 block text-center">Weld Nuts</span>
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
                <div className="flex items-end">
                  <label className="flex items-center">
                    <input
                      type="checkbox"
                      checked={sheetForm.rush}
                      onChange={(e) => setSheetForm({ ...sheetForm, rush: e.target.checked })}
                      className="mr-2 h-5 w-5"
                    />
                    <span className="font-medium">Rush Order (1.5x)</span>
                  </label>
                </div>
              </div>
            </div>
          )}

          {/* Finishes */}
          <div className="mt-4 pt-4 border-t">
            <label className="label">Finishing (optional)</label>
            <div className="flex flex-wrap gap-2">
              {finishes.map(f => (
                <button
                  key={f.id}
                  type="button"
                  onClick={() => toggleFinish(f.id)}
                  className={`px-3 py-1 rounded-full text-sm border transition-colors ${
                    (calcType === 'cnc' ? cncForm.finish_ids : sheetForm.finish_ids).includes(f.id)
                      ? 'bg-werco-primary text-white border-werco-primary'
                      : 'bg-white text-gray-700 border-gray-300 hover:border-gray-400'
                  }`}
                >
                  {f.name}
                </button>
              ))}
            </div>
          </div>

          {/* Calculate Button */}
          <button
            onClick={calculateQuote}
            disabled={calculating}
            className="btn-primary w-full mt-6 py-3 text-lg"
          >
            {calculating ? (
              <span className="flex items-center justify-center">
                <div className="animate-spin rounded-full h-5 w-5 border-b-2 border-white mr-2"></div>
                Calculating...
              </span>
            ) : (
              <span className="flex items-center justify-center">
                <CalculatorIcon className="h-5 w-5 mr-2" />
                Calculate Quote
              </span>
            )}
          </button>

          {error && (
            <div className="mt-4 bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg">
              {error}
            </div>
          )}
        </div>

        {/* Results */}
        <div className="card">
          <h2 className="text-lg font-semibold mb-4">Quote Result</h2>
          
          {!result ? (
            <div className="text-center py-12 text-gray-500">
              <CalculatorIcon className="h-16 w-16 mx-auto mb-4 text-gray-300" />
              <p>Enter part details and click Calculate</p>
            </div>
          ) : (
            <div className="space-y-4">
              {/* Big Price */}
              <div className="bg-gradient-to-br from-werco-primary to-blue-700 text-white rounded-xl p-6 text-center">
                <p className="text-blue-200 text-sm">Total Quote</p>
                <p className="text-4xl font-bold">${result.total.toLocaleString(undefined, { minimumFractionDigits: 2 })}</p>
                <p className="text-blue-200 mt-2">
                  ${result.unit_price.toFixed(2)} per unit x {calcType === 'cnc' ? cncForm.quantity : sheetForm.quantity}
                </p>
              </div>

              {/* Lead Time */}
              <div className="flex justify-between items-center p-4 bg-gray-50 rounded-lg">
                <span className="font-medium">Estimated Lead Time</span>
                <span className="text-lg font-bold text-werco-primary">{result.lead_time_days} days</span>
              </div>

              {/* Cost Breakdown */}
              <div className="border rounded-lg overflow-hidden">
                <div className="bg-gray-50 px-4 py-2 font-semibold">Cost Breakdown</div>
                <div className="divide-y">
                  {result.material_cost > 0 && (
                    <div className="flex justify-between px-4 py-2">
                      <span>Material</span>
                      <span>${result.material_cost.toFixed(2)}</span>
                    </div>
                  )}
                  {result.cutting_cost > 0 && (
                    <div className="flex justify-between px-4 py-2">
                      <span>Laser Cutting</span>
                      <span>${result.cutting_cost.toFixed(2)}</span>
                    </div>
                  )}
                  {result.machining_cost > 0 && (
                    <div className="flex justify-between px-4 py-2">
                      <span>Machining</span>
                      <span>${result.machining_cost.toFixed(2)}</span>
                    </div>
                  )}
                  {result.setup_cost > 0 && (
                    <div className="flex justify-between px-4 py-2">
                      <span>Setup</span>
                      <span>${result.setup_cost.toFixed(2)}</span>
                    </div>
                  )}
                  {result.bending_cost > 0 && (
                    <div className="flex justify-between px-4 py-2">
                      <span>Bending</span>
                      <span>${result.bending_cost.toFixed(2)}</span>
                    </div>
                  )}
                  {result.hardware_cost > 0 && (
                    <div className="flex justify-between px-4 py-2">
                      <span>Hardware</span>
                      <span>${result.hardware_cost.toFixed(2)}</span>
                    </div>
                  )}
                  {result.finish_cost > 0 && (
                    <div className="flex justify-between px-4 py-2">
                      <span>Finishing</span>
                      <span>${result.finish_cost.toFixed(2)}</span>
                    </div>
                  )}
                  <div className="flex justify-between px-4 py-2 bg-gray-50">
                    <span className="font-medium">Subtotal</span>
                    <span className="font-medium">${result.subtotal.toFixed(2)}</span>
                  </div>
                  <div className="flex justify-between px-4 py-2">
                    <span>Markup (25%)</span>
                    <span>${result.markup_amount.toFixed(2)}</span>
                  </div>
                  {result.quantity_discount > 0 && (
                    <div className="flex justify-between px-4 py-2 text-green-600">
                      <span>Quantity Discount</span>
                      <span>-${result.quantity_discount.toFixed(2)}</span>
                    </div>
                  )}
                  {result.rush_charge > 0 && (
                    <div className="flex justify-between px-4 py-2 text-orange-600">
                      <span>Rush Charge</span>
                      <span>+${result.rush_charge.toFixed(2)}</span>
                    </div>
                  )}
                </div>
              </div>

              {/* Details */}
              {result.details && Object.keys(result.details).length > 0 && (
                <div className="text-sm text-gray-500 space-y-1">
                  <p className="font-medium text-gray-700">Details:</p>
                  {Object.entries(result.details).map(([key, value]) => (
                    <p key={key}>
                      {key.replace(/_/g, ' ')}: {typeof value === 'number' ? value.toFixed(2) : value}
                    </p>
                  ))}
                </div>
              )}

              {/* Actions */}
              <div className="flex gap-3 pt-4 border-t">
                <button onClick={createQuoteFromResult} className="btn-primary flex-1 flex items-center justify-center">
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
