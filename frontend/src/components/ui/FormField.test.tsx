/**
 * FormField — label-association + accessible-error primitive.
 *
 * These lock the accessibility contract the app's forms rely on:
 *   - the <label> is programmatically tied to the control (htmlFor/id), so
 *     getByLabelText resolves it and clicking the label focuses it,
 *   - required fields render a visible asterisk + carry aria-required and a
 *     visually-hidden "required" hint,
 *   - errors render with role="alert", flip aria-invalid, and are linked via
 *     aria-describedby,
 *   - help text is linked via aria-describedby too.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { FormField } from './FormField';

describe('FormField', () => {
  it('associates the label with the control via htmlFor/id', () => {
    render(
      <FormField label="Customer Name">
        {(field) => <input {...field} type="text" />}
      </FormField>
    );

    // getByLabelText only resolves when label + control are associated.
    const input = screen.getByLabelText('Customer Name');
    expect(input.tagName).toBe('INPUT');

    // The label's htmlFor matches the control id.
    const label = screen.getByText('Customer Name').closest('label')!;
    expect(label).toHaveAttribute('for', input.id);
    expect(input.id).toBeTruthy();
  });

  it('focuses the control when its label is clicked', async () => {
    // The browser's implicit label→control focus behavior only fires when the
    // <label for> is wired to the control id — this is the user-facing proof of
    // the association, distinct from the getByLabelText query above. userEvent
    // emulates that label-click focus (plain fireEvent.click does not in jsdom).
    const user = userEvent.setup();
    render(
      <FormField label="Customer Name">
        {(field) => <input {...field} type="text" />}
      </FormField>
    );

    const input = screen.getByLabelText('Customer Name');
    expect(input).not.toHaveFocus();

    await user.click(screen.getByText('Customer Name'));

    expect(input).toHaveFocus();
  });

  it('marks required fields with an asterisk, aria-required, and a visually-hidden hint', () => {
    render(
      <FormField label="Customer Name" required>
        {(field) => <input {...field} type="text" />}
      </FormField>
    );

    const input = screen.getByLabelText(/Customer Name/);
    expect(input).toHaveAttribute('aria-required', 'true');

    // Visible asterisk is aria-hidden so it isn't read as content.
    const asterisk = screen.getByText('*');
    expect(asterisk).toHaveAttribute('aria-hidden', 'true');

    // Visually-hidden "(required)" hint is present for assistive tech.
    expect(screen.getByText('(required)')).toHaveClass('sr-only');
  });

  it('does not set aria-required when not required', () => {
    render(
      <FormField label="Notes">
        {(field) => <input {...field} type="text" />}
      </FormField>
    );
    expect(screen.getByLabelText('Notes')).not.toHaveAttribute('aria-required');
    expect(screen.queryByText('*')).not.toBeInTheDocument();
  });

  it('renders an error with role=alert, links it via aria-describedby, and sets aria-invalid', () => {
    render(
      <FormField label="Email" error="Enter a valid email address">
        {(field) => <input {...field} type="email" />}
      </FormField>
    );

    const input = screen.getByLabelText('Email');
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('Enter a valid email address');

    expect(input).toHaveAttribute('aria-invalid', 'true');
    // aria-describedby references the error node's id.
    expect(input.getAttribute('aria-describedby')).toContain(alert.id);
    expect(alert.id).toBeTruthy();
  });

  it('omits aria-invalid and the alert when there is no error', () => {
    render(
      <FormField label="Email">
        {(field) => <input {...field} type="email" />}
      </FormField>
    );
    expect(screen.getByLabelText('Email')).not.toHaveAttribute('aria-invalid');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('toggles aria-invalid with the error prop', () => {
    const { rerender } = render(
      <FormField label="Email">
        {(field) => <input {...field} type="email" />}
      </FormField>
    );
    expect(screen.getByLabelText('Email')).not.toHaveAttribute('aria-invalid');

    rerender(
      <FormField label="Email" error="Required">
        {(field) => <input {...field} type="email" />}
      </FormField>
    );
    expect(screen.getByLabelText('Email')).toHaveAttribute('aria-invalid', 'true');

    rerender(
      <FormField label="Email">
        {(field) => <input {...field} type="email" />}
      </FormField>
    );
    expect(screen.getByLabelText('Email')).not.toHaveAttribute('aria-invalid');
  });

  it('links help text via aria-describedby', () => {
    render(
      <FormField label="ZIP" help="5-digit US postal code">
        {(field) => <input {...field} type="text" />}
      </FormField>
    );

    const input = screen.getByLabelText('ZIP');
    const help = screen.getByText('5-digit US postal code');
    expect(input.getAttribute('aria-describedby')).toContain(help.id);
  });

  it('references both help and error in aria-describedby when both are present', () => {
    render(
      <FormField label="ZIP" help="5-digit US postal code" error="Invalid ZIP">
        {(field) => <input {...field} type="text" />}
      </FormField>
    );

    const input = screen.getByLabelText('ZIP');
    const help = screen.getByText('5-digit US postal code');
    const alert = screen.getByRole('alert');
    const describedBy = input.getAttribute('aria-describedby') || '';
    expect(describedBy).toContain(help.id);
    expect(describedBy).toContain(alert.id);
  });

  it('works with a native <select>', () => {
    render(
      <FormField label="Payment Terms">
        {(field) => (
          <select {...field}>
            <option value="net30">Net 30</option>
            <option value="net15">Net 15</option>
          </select>
        )}
      </FormField>
    );
    expect(screen.getByLabelText('Payment Terms').tagName).toBe('SELECT');
  });

  it('supports a plain-node child (non-render-prop) for label/error chrome', () => {
    render(
      <FormField label="Toggle" error="Bad">
        <input type="checkbox" aria-label="toggle-control" />
      </FormField>
    );
    expect(screen.getByRole('alert')).toHaveTextContent('Bad');
    expect(screen.getByLabelText('toggle-control')).toBeInTheDocument();
  });
});
