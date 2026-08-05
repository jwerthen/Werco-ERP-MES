/**
 * ComboBox — the hand-rolled searchable single-select.
 *
 * What is pinned here is the contract the laser-nest sheet-part picker (and any
 * future caller) leans on, in the order it matters:
 *
 *  - TYPE-AHEAD is multi-term and searches the hint as well as the label, so
 *    "a36 60x96" narrows a 500-row material list the way a planner types it
 *    rather than being matched as one literal string;
 *  - KEYBOARD is complete without a mouse — ArrowUp/Down/Home/End move the
 *    active option, Enter commits it — and DOM focus never leaves the input, so
 *    `aria-activedescendant` is the only thing pointing at the active row;
 *  - ESCAPE closes the popup and STOPS THERE. This one is load-bearing: the
 *    first caller lives inside a `<Modal>`, and an Escape that closed both would
 *    throw away a review grid the planner has been correcting;
 *  - CLEARING is reachable both ways (the clear affordance and the empty option)
 *    and reports `''`, never `undefined`;
 *  - GROUP HEADERS are emitted from the options' own order (never sorted here)
 *    and are not themselves options — a header must never be selectable;
 *  - the FOOTER slot renders inside the popup and survives being clicked, which
 *    is what lets SheetPartPicker put its "show all materials" toggle there.
 */

import React, { useState } from 'react';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { ComboBox, ComboBoxOption } from './ComboBox';
import { Modal } from './Modal';
import {
  comboBoxListbox,
  comboBoxOptions,
  expectComboBoxOptions,
  openComboBox,
  typeInComboBox,
} from '../../test-utils/comboBox';

// Sizes deliberately share no digit run with any hint, so a match on "12" can
// only have come from the hint — otherwise "60X120" would satisfy it silently
// and the hint-matching test would prove nothing.
const STOCK: ComboBoxOption[] = [
  { value: '31', label: 'PL-0.188-72X144 — A36 plate', hint: '12 EA on hand' },
  { value: '32', label: 'PL-0.250-60X96 — A36 plate', hint: '0 EA on hand' },
  { value: '33', label: 'SH-10GA-48X96 — CS sheet', hint: '4 EA on hand' },
];

interface HarnessProps {
  options?: ComboBoxOption[];
  /** Drop the empty option entirely, making a choice mandatory. */
  mandatory?: boolean;
  initialValue?: string;
  onChange?: (value: string) => void;
  footer?: React.ReactNode;
  noResultsLabel?: string;
}

/** Controlled wrapper — the component is a controlled input, like its callers. */
function Harness({ options = STOCK, mandatory, initialValue = '', onChange, footer, noResultsLabel }: HarnessProps) {
  const [value, setValue] = useState(initialValue);
  return (
    <ComboBox
      options={options}
      value={value}
      onChange={(next) => {
        setValue(next);
        onChange?.(next);
      }}
      emptyOptionLabel={mandatory ? undefined : '(none — untied)'}
      ariaLabel="Sheet part"
      footer={footer}
      noResultsLabel={noResultsLabel}
    />
  );
}

const trigger = () => screen.getByLabelText('Sheet part');

const isClosed = (input: HTMLElement) => {
  expect(input).toHaveAttribute('aria-expanded', 'false');
  expect(input).not.toHaveAttribute('aria-controls');
};

/** The option `aria-activedescendant` currently points at. */
const activeOption = (input: HTMLElement): HTMLElement | null => {
  const id = input.getAttribute('aria-activedescendant');
  return id ? document.getElementById(id) : null;
};

describe('opening and closing', () => {
  it('opens on click and advertises the listbox it controls', () => {
    render(<Harness />);
    const input = trigger();

    isClosed(input);

    const list = openComboBox(input);
    expect(input).toHaveAttribute('aria-expanded', 'true');
    expect(list).toHaveAttribute('role', 'listbox');
    // Portaled out of the caller's DOM (the first caller's popup would be
    // clipped by an overflow-auto table otherwise) — so the ARIA wiring, not
    // containment, is what ties the two together.
    expect(list.closest('[role="combobox"]')).toBeNull();
  });

  it('opens on focus, so tabbing into it does not dead-end', () => {
    render(<Harness />);
    fireEvent.focus(trigger());
    expect(comboBoxOptions(trigger())).toHaveLength(4); // (none) + 3
  });

  it('closes on an outside pointer press without committing anything', () => {
    const onChange = jest.fn();
    render(<Harness onChange={onChange} />);
    openComboBox(trigger());

    fireEvent.mouseDown(document.body);

    isClosed(trigger());
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe('type-ahead filtering', () => {
  it('narrows on a single term, case-insensitively', () => {
    render(<Harness />);
    const input = trigger();

    typeInComboBox(input, 'cs sheet');

    expectComboBoxOptions(input, ['SH-10GA-48X96 — CS sheet 4 EA on hand']);
  });

  it('requires EVERY whitespace-separated term, not the literal string', () => {
    render(<Harness />);
    const input = trigger();

    // Terms out of order and non-adjacent: a literal-substring match would find
    // nothing here, which is exactly the behavior a planner would call broken.
    typeInComboBox(input, '60x96 a36');

    expectComboBoxOptions(input, ['PL-0.250-60X96 — A36 plate 0 EA on hand']);
  });

  it('matches the hint as well as the label', () => {
    render(<Harness />);
    const input = trigger();

    typeInComboBox(input, '12 ea');

    expectComboBoxOptions(input, ['PL-0.188-72X144 — A36 plate 12 EA on hand']);
  });

  it('filters the empty option out too, and shows the no-results copy', () => {
    render(<Harness noResultsLabel="No sheet stock matches" />);
    const input = trigger();

    typeInComboBox(input, 'titanium');

    expectComboBoxOptions(input, []);
    expect(within(comboBoxListbox(input)).getByText('No sheet stock matches')).toBeInTheDocument();
  });

  it('shows the query while typing and the committed label at rest', () => {
    render(<Harness />);
    const input = trigger();

    typeInComboBox(input, 'cs');
    expect(input).toHaveValue('cs');

    fireEvent.click(within(comboBoxListbox(input)).getByRole('option', { name: /SH-10GA-48X96/ }));

    // The query is discarded on commit — the cell has to stay readable at rest
    // in a table of 40 rows.
    expect(input).toHaveValue('SH-10GA-48X96 — CS sheet');
    isClosed(input);
  });

  it('starts each reopen from the full list rather than the last query', () => {
    render(<Harness />);
    const input = trigger();

    typeInComboBox(input, 'titanium');
    fireEvent.keyDown(input, { key: 'Escape' });
    openComboBox(input);

    expect(comboBoxOptions(input)).toHaveLength(4);
  });
});

describe('keyboard navigation', () => {
  it('opens on ArrowDown and walks the list without moving DOM focus', () => {
    render(<Harness />);
    const input = trigger();
    // Real focus (not just a focus event) so the DOM-focus assertion below is
    // meaningful; focusing opens the list, so start from a known closed state.
    act(() => input.focus());
    fireEvent.keyDown(input, { key: 'Escape' });
    isClosed(input);

    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(input).toHaveAttribute('aria-expanded', 'true');
    expect(activeOption(input)).toHaveAccessibleName('(none — untied)');

    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(activeOption(input)).toHaveAccessibleName('PL-0.188-72X144 — A36 plate 12 EA on hand');

    fireEvent.keyDown(input, { key: 'ArrowUp' });
    expect(activeOption(input)).toHaveAccessibleName('(none — untied)');

    // DOM focus never leaves the input — that is what makes Escape and Tab
    // behave, and why aria-activedescendant has to carry the active row.
    expect(document.activeElement).toBe(input);
  });

  it('clamps at both ends and jumps with Home/End', () => {
    render(<Harness />);
    const input = trigger();
    openComboBox(input);

    fireEvent.keyDown(input, { key: 'ArrowUp' });
    expect(activeOption(input)).toHaveAccessibleName('(none — untied)');

    fireEvent.keyDown(input, { key: 'End' });
    expect(activeOption(input)).toHaveAccessibleName('SH-10GA-48X96 — CS sheet 4 EA on hand');

    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(activeOption(input)).toHaveAccessibleName('SH-10GA-48X96 — CS sheet 4 EA on hand');

    fireEvent.keyDown(input, { key: 'Home' });
    expect(activeOption(input)).toHaveAccessibleName('(none — untied)');
  });

  it('Enter commits the active option and closes', () => {
    const onChange = jest.fn();
    render(<Harness onChange={onChange} />);
    const input = trigger();
    openComboBox(input);

    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.keyDown(input, { key: 'Enter' });

    expect(onChange).toHaveBeenCalledWith('32');
    expect(input).toHaveValue('PL-0.250-60X96 — A36 plate');
    isClosed(input);
  });

  it('Enter commits the option the FILTERED list is pointing at', () => {
    const onChange = jest.fn();
    render(<Harness onChange={onChange} />);
    const input = trigger();

    typeInComboBox(input, 'plate');
    // Filtering resets the active option to the top of the narrowed list, so
    // Enter cannot commit a row the planner can no longer see.
    fireEvent.keyDown(input, { key: 'Enter' });

    expect(onChange).toHaveBeenCalledWith('31');
  });

  it('Enter on a closed control does nothing (it must not commit a stale pick)', () => {
    const onChange = jest.fn();
    render(<Harness onChange={onChange} />);

    fireEvent.keyDown(trigger(), { key: 'Enter' });

    expect(onChange).not.toHaveBeenCalled();
  });

  it('Tab closes the popup and lets focus move on', () => {
    render(<Harness />);
    const input = trigger();
    openComboBox(input);

    fireEvent.keyDown(input, { key: 'Tab' });

    isClosed(input);
  });
});

describe('Escape inside a Modal', () => {
  it('closes only the popup, leaving the enclosing dialog open', () => {
    const onClose = jest.fn();
    render(
      <Modal open onClose={onClose}>
        <Harness />
      </Modal>
    );
    const input = trigger();
    openComboBox(input);

    fireEvent.keyDown(input, { key: 'Escape' });

    isClosed(input);
    // The whole point: one Escape must not discard the review grid behind the
    // picker.
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('lets a second Escape — with the popup already closed — reach the dialog', () => {
    const onClose = jest.fn();
    render(
      <Modal open onClose={onClose}>
        <Harness />
      </Modal>
    );
    const input = trigger();
    openComboBox(input);

    fireEvent.keyDown(input, { key: 'Escape' });
    fireEvent.keyDown(input, { key: 'Escape' });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('restores the committed label rather than keeping the abandoned query', () => {
    render(<Harness initialValue="31" />);
    const input = trigger();

    typeInComboBox(input, 'zzz');
    fireEvent.keyDown(input, { key: 'Escape' });

    expect(input).toHaveValue('PL-0.188-72X144 — A36 plate');
  });
});

describe('clearing', () => {
  it('offers a clear affordance only while something is selected', () => {
    render(<Harness initialValue="31" />);
    expect(screen.getByLabelText('Clear selection')).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('Clear selection'));

    expect(screen.queryByLabelText('Clear selection')).not.toBeInTheDocument();
  });

  it('clears to the empty string, not undefined', () => {
    const onChange = jest.fn();
    render(<Harness initialValue="31" onChange={onChange} />);

    fireEvent.click(screen.getByLabelText('Clear selection'));

    expect(onChange).toHaveBeenCalledWith('');
    expect(trigger()).toHaveValue('');
  });

  it('clears through the empty option as well, so keyboard users reach it', () => {
    const onChange = jest.fn();
    render(<Harness initialValue="31" onChange={onChange} />);
    const input = trigger();

    const list = openComboBox(input);
    fireEvent.click(within(list).getByRole('option', { name: '(none — untied)' }));

    expect(onChange).toHaveBeenCalledWith('');
    expect(input).toHaveValue('');
  });

  it('omits both the empty option and the clear affordance when a choice is mandatory', () => {
    render(<Harness mandatory initialValue="31" />);
    const input = trigger();

    expect(screen.queryByLabelText('Clear selection')).not.toBeInTheDocument();
    openComboBox(input);
    expectComboBoxOptions(input, [
      'PL-0.188-72X144 — A36 plate 12 EA on hand',
      'PL-0.250-60X96 — A36 plate 0 EA on hand',
      'SH-10GA-48X96 — CS sheet 4 EA on hand',
    ]);
  });

  it('marks exactly the committed option as selected', () => {
    render(<Harness initialValue="32" />);
    const input = trigger();
    openComboBox(input);

    const selected = comboBoxOptions(input).filter((o) => o.getAttribute('aria-selected') === 'true');
    expect(selected).toHaveLength(1);
    expect(selected[0]).toHaveAccessibleName('PL-0.250-60X96 — A36 plate 0 EA on hand');
  });
});

describe('group headers', () => {
  const GROUPED: ComboBoxOption[] = [
    { value: '31', label: 'PL-0.188-72X144', group: 'Sheet & plate' },
    { value: '32', label: 'PL-0.250-60X96', group: 'Sheet & plate' },
    { value: '40', label: 'ANG-A36-1.5X1.5X.25', group: 'Other materials' },
  ];

  it('emits one header per group, in the order the options were given', () => {
    render(<Harness options={GROUPED} mandatory />);
    const list = openComboBox(trigger());

    expect(within(list).getAllByText('Sheet & plate')).toHaveLength(1);
    expect(within(list).getByText('Other materials')).toBeInTheDocument();

    // Header order follows the option order — the component never sorts.
    const text = (list.textContent ?? '').replace(/\s+/g, ' ');
    expect(text.indexOf('Sheet & plate')).toBeLessThan(text.indexOf('Other materials'));
  });

  it('never makes a header selectable', () => {
    render(<Harness options={GROUPED} mandatory />);
    const input = trigger();

    openComboBox(input);
    expectComboBoxOptions(input, ['PL-0.188-72X144', 'PL-0.250-60X96', 'ANG-A36-1.5X1.5X.25']);
  });

  it('drops a header whose whole group is filtered away', () => {
    render(<Harness options={GROUPED} mandatory />);
    const input = trigger();

    typeInComboBox(input, 'ang-');

    const list = comboBoxListbox(input);
    expect(within(list).queryByText('Sheet & plate')).not.toBeInTheDocument();
    expect(within(list).getByText('Other materials')).toBeInTheDocument();
  });
});

describe('footer slot', () => {
  it('renders inside the popup and stays open when it is used', () => {
    const onToggle = jest.fn();
    render(
      <Harness
        footer={
          <button type="button" onClick={onToggle}>
            Show all materials (7 more)
          </button>
        }
      />
    );
    const input = trigger();
    const list = openComboBox(input);

    const footerButton = screen.getByRole('button', { name: /show all materials/i });
    // In the popup, but outside the listbox — a footer control is not an option.
    expect(list.contains(footerButton)).toBe(false);
    expect(list.parentElement?.contains(footerButton)).toBe(true);

    fireEvent.mouseDown(footerButton);
    fireEvent.click(footerButton);

    expect(onToggle).toHaveBeenCalledTimes(1);
    // The popup counts as "inside" for the outside-press handler, so using the
    // toggle does not close the list the planner is about to read.
    expect(input).toHaveAttribute('aria-expanded', 'true');
  });

  it('renders no footer chrome when the caller passes none', () => {
    render(<Harness />);
    const list = openComboBox(trigger());

    expect(within(list.parentElement as HTMLElement).queryByRole('button', { name: /show all/i })).toBeNull();
  });
});

describe('disabled', () => {
  it('does not open', () => {
    render(
      <ComboBox
        options={STOCK}
        value=""
        onChange={jest.fn()}
        ariaLabel="Sheet part"
        disabled
        emptyOptionLabel="(none)"
      />
    );
    const input = trigger();

    fireEvent.click(input);

    expect(input).toBeDisabled();
    isClosed(input);
  });
});
