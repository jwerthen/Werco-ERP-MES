/**
 * Button — the standard action-button primitive.
 *
 * Standardizes the ~92 raw inline `<button className="btn-primary ...">` action
 * buttons scattered across pages onto one typed component that maps to the
 * existing instrument-panel `.btn-*` classes in index.css.
 *
 * Relationship to LoadingButton: use `Button` for ordinary click actions; reach
 * for `LoadingButton` when the action is async and you need an in-flight spinner
 * / double-click guard. Both share the same `variant` / `size` vocabulary so you
 * can swap one for the other without re-learning the API.
 */

import React from 'react';

export type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'ghost';
export type ButtonSize = 'sm' | 'md';

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
}

const variantClass: Record<ButtonVariant, string> = {
  primary: 'btn-primary',
  secondary: 'btn-secondary',
  danger: 'btn-danger',
  ghost: 'btn-ghost',
};

const sizeClass: Record<ButtonSize, string> = {
  sm: 'btn-sm',
  md: '', // md is the default .btn-* sizing
};

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ variant = 'primary', size = 'md', type = 'button', className = '', children, ...rest }, ref) => {
    const classes = [variantClass[variant], sizeClass[size], className].filter(Boolean).join(' ');
    return (
      <button ref={ref} type={type} className={classes} {...rest}>
        {children}
      </button>
    );
  },
);

Button.displayName = 'Button';

export default Button;
