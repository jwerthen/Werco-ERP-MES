/**
 * SelectField — the custom portaled select primitive.
 *
 * Covers the interaction surface call sites rely on: open-on-click + mouse
 * selection, keyboard navigation from the trigger button (Arrow/Enter/Escape),
 * and — the load-bearing regression — keyboard navigation from the SEARCH INPUT
 * of a searchable select. The menu (and its search input) is portaled to
 * document.body, so keystrokes typed there never bubble to the trigger button;
 * the bug this guards against was the keydown handler living only on the trigger,
 * leaving Arrow/Enter/Escape dead on the focused search input.
 */

import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SelectField, SelectOption } from './SelectField';

const OPTIONS: SelectOption<string>[] = [
  { value: 'apple', label: 'Apple' },
  { value: 'banana', label: 'Banana' },
  { value: 'cherry', label: 'Cherry' },
  { value: 'blueberry', label: 'Blueberry' },
];

function getTrigger() {
  return screen.getByRole('button', { name: /select an option/i });
}

describe('SelectField', () => {
  describe('mouse interaction', () => {
    it('opens the listbox on trigger click, renders options, and selects on click', async () => {
      const user = userEvent.setup();
      const onChange = jest.fn();
      render(
        <SelectField
          value=""
          options={OPTIONS}
          onChange={onChange}
          ariaLabel="Select an option"
        />,
      );

      // Closed by default — no listbox rendered.
      expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
      expect(getTrigger()).toHaveAttribute('aria-expanded', 'false');

      await user.click(getTrigger());

      // Listbox is open with one option per entry.
      expect(screen.getByRole('listbox')).toBeInTheDocument();
      expect(getTrigger()).toHaveAttribute('aria-expanded', 'true');
      expect(screen.getAllByRole('option')).toHaveLength(OPTIONS.length);

      // Clicking an option fires onChange with that option's value and closes.
      await user.click(screen.getByRole('option', { name: 'Banana' }));
      expect(onChange).toHaveBeenCalledTimes(1);
      expect(onChange).toHaveBeenCalledWith('banana');
      expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    });
  });

  describe('keyboard navigation from the trigger button', () => {
    it('ArrowDown/ArrowUp move the highlight, Enter selects, Escape closes', () => {
      const onChange = jest.fn();
      render(
        <SelectField
          value=""
          options={OPTIONS}
          onChange={onChange}
          ariaLabel="Select an option"
        />,
      );

      const trigger = getTrigger();
      trigger.focus();

      // First ArrowDown opens the menu (highlight starts at index 0 = Apple).
      fireEvent.keyDown(trigger, { key: 'ArrowDown' });
      expect(screen.getByRole('listbox')).toBeInTheDocument();

      // ArrowDown -> index 1 (Banana), ArrowDown -> index 2 (Cherry).
      fireEvent.keyDown(trigger, { key: 'ArrowDown' });
      fireEvent.keyDown(trigger, { key: 'ArrowDown' });
      // ArrowUp -> back to index 1 (Banana).
      fireEvent.keyDown(trigger, { key: 'ArrowUp' });

      // Enter selects the highlighted option (Banana).
      fireEvent.keyDown(trigger, { key: 'Enter' });
      expect(onChange).toHaveBeenCalledTimes(1);
      expect(onChange).toHaveBeenCalledWith('banana');
      expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    });

    it('Escape closes the menu without selecting', () => {
      const onChange = jest.fn();
      render(
        <SelectField
          value=""
          options={OPTIONS}
          onChange={onChange}
          ariaLabel="Select an option"
        />,
      );

      const trigger = getTrigger();
      trigger.focus();
      fireEvent.keyDown(trigger, { key: 'ArrowDown' });
      expect(screen.getByRole('listbox')).toBeInTheDocument();

      fireEvent.keyDown(trigger, { key: 'Escape' });
      expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
      expect(onChange).not.toHaveBeenCalled();
    });
  });

  describe('searchable: keyboard navigation from the portaled search input (regression)', () => {
    // The search input is portaled to document.body and receives focus on open;
    // keystrokes there do not bubble to the trigger button. These tests fire
    // keydown ON THE SEARCH INPUT and assert nav still works — they fail if the
    // keydown handler is only wired to the trigger (the original bug).

    it('focuses the search input on open', async () => {
      jest.useFakeTimers();
      try {
        render(
          <SelectField
            value=""
            options={OPTIONS}
            onChange={jest.fn()}
            searchable
            ariaLabel="Select an option"
          />,
        );

        fireEvent.click(getTrigger());
        // Focus move is deferred a tick via setTimeout(…, 0).
        jest.advanceTimersByTime(0);

        const searchInput = screen.getByPlaceholderText('Search...');
        expect(document.activeElement).toBe(searchInput);
      } finally {
        jest.useRealTimers();
      }
    });

    it('ArrowDown then Enter on the search input selects the highlighted filtered option', () => {
      const onChange = jest.fn();
      render(
        <SelectField
          value=""
          options={OPTIONS}
          onChange={onChange}
          searchable
          ariaLabel="Select an option"
        />,
      );

      fireEvent.click(getTrigger());
      const searchInput = screen.getByPlaceholderText('Search...');

      // Filter down to two options (Banana, Blueberry both start with "b").
      fireEvent.change(searchInput, { target: { value: 'b' } });
      const filtered = screen.getAllByRole('option');
      expect(filtered).toHaveLength(2);
      expect(filtered.map((el) => el.textContent)).toEqual(['Banana', 'Blueberry']);

      // ArrowDown on the SEARCH INPUT moves highlight 0 -> 1 (Blueberry).
      fireEvent.keyDown(searchInput, { key: 'ArrowDown' });
      // Enter on the SEARCH INPUT selects the highlighted filtered option.
      fireEvent.keyDown(searchInput, { key: 'Enter' });

      expect(onChange).toHaveBeenCalledTimes(1);
      expect(onChange).toHaveBeenCalledWith('blueberry');
      expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    });

    it('Enter on the search input with no nav selects the first filtered option', () => {
      // Proves Enter is handled on the input at all — not a no-op — even without
      // an ArrowDown first (highlight defaults to index 0 of the filtered list).
      const onChange = jest.fn();
      render(
        <SelectField
          value=""
          options={OPTIONS}
          onChange={onChange}
          searchable
          ariaLabel="Select an option"
        />,
      );

      fireEvent.click(getTrigger());
      const searchInput = screen.getByPlaceholderText('Search...');
      fireEvent.change(searchInput, { target: { value: 'cher' } });

      fireEvent.keyDown(searchInput, { key: 'Enter' });
      expect(onChange).toHaveBeenCalledTimes(1);
      expect(onChange).toHaveBeenCalledWith('cherry');
    });

    it('Escape on the search input closes the menu', () => {
      const onChange = jest.fn();
      render(
        <SelectField
          value=""
          options={OPTIONS}
          onChange={onChange}
          searchable
          ariaLabel="Select an option"
        />,
      );

      fireEvent.click(getTrigger());
      const searchInput = screen.getByPlaceholderText('Search...');
      expect(screen.getByRole('listbox')).toBeInTheDocument();

      fireEvent.keyDown(searchInput, { key: 'Escape' });
      expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
      expect(onChange).not.toHaveBeenCalled();
    });
  });
});
