import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { Modal } from './Modal';
import { SelectField } from './SelectField';
import { ConfirmDialog } from './ConfirmDialog';
import { Tabs } from './Tabs';
it('keeps a required picker above its owning modal and closes one Escape layer', () => {
  const close = jest.fn();
  render(
    <Modal open onClose={close}>
      <h2>Add visit</h2>
      <SelectField ariaLabel="Purpose" value="" onChange={jest.fn()} options={[{ value: 'visit', label: 'Visit' }]} />
    </Modal>
  );
  const trigger = screen.getByRole('combobox', { name: 'Purpose' });
  fireEvent.click(trigger);
  const list = screen.getByRole('listbox');
  expect(screen.getByRole('dialog', { name: 'Add visit' }).parentElement).toContainElement(list);
  expect(trigger).toHaveAttribute('aria-controls', list.id);
  expect(document.getElementById(trigger.getAttribute('aria-activedescendant')!)).toBeInTheDocument();
  fireEvent.keyDown(trigger, { key: 'Escape' });
  expect(close).not.toHaveBeenCalled();
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  fireEvent.keyDown(trigger, { key: 'Escape' });
  expect(close).toHaveBeenCalledTimes(1);
});
it('names confirmations and provides roving keyboard tabs', () => {
  const change = jest.fn();
  render(
    <>
      <ConfirmDialog open title="Delete this draft?" message="Draft only" onConfirm={jest.fn()} onCancel={jest.fn()} />
      <Tabs
        activeTab="a"
        onChange={change}
        tabs={[
          { id: 'a', label: 'First' },
          { id: 'b', label: 'Second' },
        ]}
      />
    </>
  );
  expect(screen.getByRole('dialog', { name: 'Delete this draft?' })).toBeInTheDocument();
  fireEvent.keyDown(screen.getByRole('tab', { name: 'First' }), { key: 'ArrowRight' });
  expect(change).toHaveBeenCalledWith('b');
  expect(screen.getByRole('tab', { name: 'Second' })).toHaveFocus();
});
