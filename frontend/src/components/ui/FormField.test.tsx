/**
 * FormField Component Tests
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import { FormField } from './FormField';

describe('FormField', () => {
  it('renders label with correct text', () => {
    render(
      <FormField label="Email" name="email">
        <input type="email" />
      </FormField>
    );
    expect(screen.getByText('Email')).toBeInTheDocument();
  });

  it('renders children (input element)', () => {
    render(
      <FormField label="Username" name="username">
        <input type="text" data-testid="username-input" />
      </FormField>
    );
    expect(screen.getByTestId('username-input')).toBeInTheDocument();
  });

  it('shows required asterisk when required is true', () => {
    render(
      <FormField label="Password" name="password" required>
        <input type="password" />
      </FormField>
    );
    expect(screen.getByText('*')).toBeInTheDocument();
    expect(screen.getByText('*')).toHaveClass('text-red-500');
  });

  it('does not show asterisk when required is false', () => {
    render(
      <FormField label="Optional Field" name="optional">
        <input type="text" />
      </FormField>
    );
    expect(screen.queryByText('*')).not.toBeInTheDocument();
  });

  it('displays string error message', () => {
    render(
      <FormField label="Email" name="email" error="Invalid email format">
        <input type="email" />
      </FormField>
    );
    expect(screen.getByText('Invalid email format')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toBeInTheDocument();
  });

  it('displays error from FieldError object', () => {
    const fieldError = { message: 'Field is required' };
    render(
      <FormField label="Name" name="name" error={fieldError}>
        <input type="text" />
      </FormField>
    );
    expect(screen.getByText('Field is required')).toBeInTheDocument();
  });

  it('displays error from nested error object', () => {
    const nestedError = { message: { message: 'Nested error message' } };
    render(
      <FormField label="Complex" name="complex" error={nestedError}>
        <input type="text" />
      </FormField>
    );
    expect(screen.getByText('Nested error message')).toBeInTheDocument();
  });

  it('does not display error when error is null', () => {
    render(
      <FormField label="Email" name="email" error={null}>
        <input type="email" />
      </FormField>
    );
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('does not display error when error is undefined', () => {
    render(
      <FormField label="Email" name="email" error={undefined}>
        <input type="email" />
      </FormField>
    );
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('applies custom className', () => {
    render(
      <FormField label="Custom" name="custom" className="mt-4">
        <input type="text" />
      </FormField>
    );
    expect(screen.getByTestId('form-field-custom')).toHaveClass('mt-4', 'mb-4');
  });

  it('associates label with input via htmlFor', () => {
    render(
      <FormField label="Email" name="email">
        <input type="email" id="email" />
      </FormField>
    );
    const label = screen.getByText('Email');
    expect(label).toHaveAttribute('for', 'email');
  });

  it('shows error icon when there is an error', () => {
    render(
      <FormField label="Email" name="email" error="Error">
        <input type="email" />
      </FormField>
    );
    expect(screen.getByTestId('error-icon')).toBeInTheDocument();
  });

  it('renders multiple children', () => {
    render(
      <FormField label="Combined" name="combined">
        <input type="text" data-testid="input1" />
        <span data-testid="helper">Helper text</span>
      </FormField>
    );
    expect(screen.getByTestId('input1')).toBeInTheDocument();
    expect(screen.getByTestId('helper')).toBeInTheDocument();
  });
});
