import React, { useId, useState } from 'react';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from './ui/dialog';
import { formatIn, inToMm, mmToIn, parseInches } from './lib/units';

export type DXFFileAssignment = { material: string; thickness: number };

export default function DXFAssignments({
  files,
  initial,
  onClose,
  onConfirm,
}: {
  files: File[];
  initial: DXFFileAssignment;
  onClose: () => void;
  onConfirm: (assignments: DXFFileAssignment[]) => void;
}) {
  const id = useId();
  const [assignments, setAssignments] = useState(() => files.map(() => ({ ...initial })));
  const [selected, setSelected] = useState(() => files.map(() => true));
  const [material, setMaterial] = useState(initial.material);
  const [thickness, setThickness] = useState(String(Number(mmToIn(initial.thickness).toFixed(6))));
  const [error, setError] = useState('');
  const count = selected.filter(Boolean).length;
  function apply() {
    const value = inToMm(parseInches(thickness));
    if (!count) return setError('Select at least one file to assign.');
    if (!Number.isFinite(value) || value <= 0 || value > 100)
      return setError('Enter a positive thickness, up to 3.937 inches.');
    setAssignments(current => current.map((row, index) => (selected[index] ? { material, thickness: value } : row)));
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
            receives its own nest and sheet order. File geometry is read after you confirm.
          </DialogDescription>
        </DialogHeader>
        <div className="assignment-controls">
          <label className="field-label" htmlFor={id + '-material'}>
            <span>Material for selected files</span>
            <select
              id={id + '-material'}
              className="assignment-select"
              value={material}
              onChange={event => setMaterial(event.target.value)}
            >
              {['Carbon steel', 'Stainless steel', 'Aluminum'].map(value => (
                <option key={value}>{value}</option>
              ))}
            </select>
          </label>
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
          <button className="secondary" onClick={apply}>
            Apply to {count} selected
          </button>
        </div>
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
              <span>{assignments[index].material}</span>
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
