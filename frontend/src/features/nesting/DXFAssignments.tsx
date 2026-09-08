import React, { useId, useState } from 'react';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from './ui/dialog';
import { formatIn, inToMm, mmToIn, parseInches } from './lib/units';
import { catalogFamily, type MaterialBinding } from './lib/material-binding';
import type { NestingCatalogState } from './useNestingCatalog';

export type DXFFileAssignment = {
  material: string;
  thickness: number;
  materialBinding?: MaterialBinding;
  units?: 'in' | 'mm';
};

export default function DXFAssignments({
  files,
  initial,
  catalog,
  companyId,
  onClose,
  onConfirm,
}: {
  files: File[];
  initial: DXFFileAssignment;
  catalog?: NestingCatalogState;
  companyId?: number;
  onClose: () => void;
  onConfirm: (assignments: DXFFileAssignment[]) => void;
}) {
  const id = useId();
  const [assignments, setAssignments] = useState(() => files.map(() => ({ ...initial })));
  const [selected, setSelected] = useState(() => files.map(() => true));
  const [material, setMaterial] = useState(initial.material);
  const [binding, setBinding] = useState(initial.materialBinding);
  const [units, setUnits] = useState<'in' | 'mm'>(initial.units ?? 'in');
  const [thickness, setThickness] = useState(String(Number(mmToIn(initial.thickness).toFixed(6))));
  const [error, setError] = useState('');
  const count = selected.filter(Boolean).length;
  function apply() {
    const value = inToMm(parseInches(thickness));
    if (!count) return setError('Select at least one file to assign.');
    if (!Number.isFinite(value) || value <= 0 || value > 100)
      return setError('Enter a positive thickness, up to 3.937 inches.');
    setAssignments(current =>
      current.map((row, index) =>
        selected[index] ? { material, thickness: value, materialBinding: binding, units } : row
      )
    );
    setError('');
  }
  return (
    <Dialog
      open
      onOpenChange={open => {
        if (!open) onClose();
      }}
    >
      <DialogContent className="app-dialog assignment-dialog">
        <DialogHeader>
          <DialogTitle>Assign DXF materials and thicknesses</DialogTitle>
          <DialogDescription>
            Select file rows, apply their material and thickness, then import all files. Each material and thickness
            receives its own nest and sheet order. Different catalog materials stay separate. Declared DXF units take
            precedence over the fallback assigned below. File geometry is read after you confirm.
          </DialogDescription>
        </DialogHeader>
        <div className="assignment-controls">
          <label className="field-label" htmlFor={id + '-material'}>
            <span>Material for selected files</span>
            <select
              id={id + '-material'}
              className="assignment-select"
              value={material}
              onChange={event => {
                setMaterial(event.target.value);
                setBinding(undefined);
              }}
            >
              {['Carbon steel', 'Stainless steel', 'Aluminum'].map(value => (
                <option key={value}>{value}</option>
              ))}
            </select>
          </label>
          {catalog && (
            <label className="field-label" htmlFor={id + '-catalog'}>
              <span>Catalog material for selected files</span>
              <select
                id={id + '-catalog'}
                className="assignment-select"
                value={binding?.catalog.id ?? ''}
                disabled={!companyId || catalog.loading}
                onChange={event => {
                  const selectedMaterial = catalog.items.find(item => item.id === Number(event.target.value));
                  const family = selectedMaterial && catalogFamily(selectedMaterial.category);
                  if (selectedMaterial && family && companyId) {
                    setBinding({ companyId, catalog: selectedMaterial });
                    setMaterial(family);
                  } else setBinding(undefined);
                }}
              >
                <option value="">Family only · catalog review needed</option>
                {binding && !catalog.items.some(item => item.id === binding.catalog.id) && (
                  <option value={binding.catalog.id}>{binding.catalog.name} · saved selection</option>
                )}
                {catalog.items
                  .filter(item => catalogFamily(item.category))
                  .map(item => (
                    <option key={item.id} value={item.id}>
                      {item.name} · ID {item.id}
                    </option>
                  ))}
              </select>
            </label>
          )}
          <label className="field-label" htmlFor={id + '-thickness'}>
            <span>
              Thickness for selected files <small>in</small>
            </span>
            <input
              id={id + '-thickness'}
              className="assignment-input"
              inputMode="decimal"
              value={thickness}
              placeholder="Decimal or fraction"
              onChange={event => setThickness(event.target.value)}
            />
          </label>
          <label className="field-label" htmlFor={id + '-units'}>
            <span>Units for unitless DXFs</span>
            <select
              id={id + '-units'}
              className="assignment-select"
              value={units}
              onChange={event => setUnits(event.target.value as 'in' | 'mm')}
            >
              <option value="in">Inches</option>
              <option value="mm">Millimeters</option>
            </select>
          </label>
          <button className="secondary" onClick={apply}>
            Apply to {count} selected
          </button>
        </div>
        {catalog?.error && (
          <p className="inline-error" role="alert">
            {catalog.error} Assign family and thickness now, then resolve the catalog source later.
          </p>
        )}
        {!!catalog && catalog.items.length < catalog.total && (
          <button type="button" className="text-button" disabled={catalog.loading} onClick={catalog.loadMore}>
            Load more catalog materials
          </button>
        )}
        <div className="assignment-selection">
          <button className="text-button" onClick={() => setSelected(files.map(() => true))}>
            Select all files
          </button>
          <button className="text-button" onClick={() => setSelected(files.map(() => false))}>
            Clear selection
          </button>
          <span>
            {count} selected · {files.length} files to import
          </span>
        </div>
        {error && (
          <p className="inline-error" role="alert">
            {error}
          </p>
        )}
        <div className="assignment-files" role="region" aria-label="Material assignments for selected DXF files">
          {files.map((file, index) => (
            <div className="assignment-row" key={index}>
              <label>
                <input
                  type="checkbox"
                  checked={selected[index]}
                  aria-label={`Select file ${index + 1}: ${file.name}`}
                  onChange={event =>
                    setSelected(current => current.map((value, i) => (i === index ? event.target.checked : value)))
                  }
                />
                <span>
                  {index + 1}. {file.name}
                </span>
              </label>
              <span>
                {assignments[index].materialBinding?.catalog.name ?? assignments[index].material}
                <small>Unitless: {assignments[index].units ?? 'in'}</small>
              </span>
              <strong>{formatIn(assignments[index].thickness)} in</strong>
            </div>
          ))}
        </div>
        <p className="helper inset-free">
          Unchecked rows are still imported with the values shown. Check rows to change their assignment.
        </p>
        <div className="assignment-actions">
          <button className="secondary" onClick={onClose}>
            Cancel
          </button>
          <button
            className="primary"
            onClick={() => {
              if (assignments.some(row => !Number.isFinite(row.thickness) || row.thickness <= 0 || row.thickness > 100))
                return setError('Assign a valid material thickness to every file before importing.');
              onConfirm(assignments);
            }}
          >
            Import all {files.length} files
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
