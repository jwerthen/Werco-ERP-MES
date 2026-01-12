/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./src/**/*.{js,jsx,ts,tsx}",
    "./public/index.html"
  ],
  theme: {
    // Custom breakpoints for better mobile support
    screens: {
      'xs': '375px',      // Small phones
      'sm': '640px',      // Large phones / landscape
      'md': '768px',      // Tablets
      'lg': '1024px',     // Small laptops
      'xl': '1280px',     // Desktops
      '2xl': '1536px',    // Large desktops
      // Touch-specific breakpoints
      'touch': { 'raw': '(hover: none) and (pointer: coarse)' },
      'stylus': { 'raw': '(hover: none) and (pointer: fine)' },
      'mouse': { 'raw': '(hover: hover) and (pointer: fine)' },
      // Orientation
      'portrait': { 'raw': '(orientation: portrait)' },
      'landscape': { 'raw': '(orientation: landscape)' },
    },
    extend: {
      colors: {
        // Primary brand colors - deep industrial blue
        werco: {
          50: '#eef4ff',
          100: '#d9e6ff',
          200: '#bcd3ff',
          300: '#8eb8ff',
          400: '#5990ff',
          500: '#3366ff',
          600: '#1B4D9C', // Primary brand
          700: '#1a3f7a',
          800: '#1b3664',
          900: '#1c3154',
          950: '#131f35',
        },
        // Accent - precision red for alerts/actions
        accent: {
          50: '#fef2f2',
          100: '#fee2e2',
          200: '#fecaca',
          300: '#fca5a5',
          400: '#f87171',
          500: '#C8352B', // Brand red
          600: '#b91c1c',
          700: '#991b1b',
          800: '#7f1d1d',
          900: '#450a0a',
        },
        // Surface colors - dark mode inspired for industrial feel
        surface: {
          50: '#fafafa',
          100: '#f4f4f5',
          200: '#e4e4e7',
          300: '#d4d4d8',
          400: '#a1a1aa',
          500: '#71717a',
          600: '#52525b',
          700: '#3f3f46',
          800: '#27272a',
          900: '#18181b',
          950: '#09090b',
        },
        // Status colors - high contrast for quick scanning
        status: {
          success: '#10b981',
          'success-light': '#d1fae5',
          warning: '#f59e0b',
          'warning-light': '#fef3c7',
          danger: '#ef4444',
          'danger-light': '#fee2e2',
          info: '#3b82f6',
          'info-light': '#dbeafe',
        },
        // Work order status specific colors
        wo: {
          draft: '#6b7280',
          released: '#3b82f6',
          'in-progress': '#10b981',
          'on-hold': '#f59e0b',
          complete: '#059669',
          closed: '#9ca3af',
          cancelled: '#ef4444',
        }
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['JetBrains Mono', 'Fira Code', 'Consolas', 'Monaco', 'monospace'],
        display: ['Inter', 'system-ui', 'sans-serif'],
      },
      fontSize: {
        // Slightly larger base for shop floor readability
        'xs': ['0.75rem', { lineHeight: '1rem' }],
        'sm': ['0.875rem', { lineHeight: '1.25rem' }],
        'base': ['1rem', { lineHeight: '1.5rem' }],
        'lg': ['1.125rem', { lineHeight: '1.75rem' }],
        'xl': ['1.25rem', { lineHeight: '1.75rem' }],
        '2xl': ['1.5rem', { lineHeight: '2rem' }],
        '3xl': ['1.875rem', { lineHeight: '2.25rem' }],
        '4xl': ['2.25rem', { lineHeight: '2.5rem' }],
        '5xl': ['3rem', { lineHeight: '1.16' }],
        // Display sizes for dashboards
        'display-sm': ['2rem', { lineHeight: '2.5rem', letterSpacing: '-0.02em' }],
        'display-md': ['2.5rem', { lineHeight: '3rem', letterSpacing: '-0.02em' }],
        'display-lg': ['3rem', { lineHeight: '3.5rem', letterSpacing: '-0.02em' }],
        'display-xl': ['3.75rem', { lineHeight: '4rem', letterSpacing: '-0.02em' }],
      },
      spacing: {
        // Touch-friendly spacing (44px minimum touch targets)
        '11': '2.75rem',
        '13': '3.25rem',
        '15': '3.75rem',
        '18': '4.5rem',
        '22': '5.5rem',
        '30': '7.5rem',
      },
      borderRadius: {
        'sm': '0.25rem',
        'DEFAULT': '0.5rem',
        'md': '0.5rem',
        'lg': '0.75rem',
        'xl': '1rem',
        '2xl': '1.25rem',
        '3xl': '1.5rem',
      },
      boxShadow: {
        'sm': '0 1px 2px 0 rgb(0 0 0 / 0.05)',
        'DEFAULT': '0 1px 3px 0 rgb(0 0 0 / 0.1), 0 1px 2px -1px rgb(0 0 0 / 0.1)',
        'md': '0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -2px rgb(0 0 0 / 0.1)',
        'lg': '0 10px 15px -3px rgb(0 0 0 / 0.1), 0 4px 6px -4px rgb(0 0 0 / 0.1)',
        'xl': '0 20px 25px -5px rgb(0 0 0 / 0.1), 0 8px 10px -6px rgb(0 0 0 / 0.1)',
        'glow': '0 0 20px rgb(27 77 156 / 0.3)',
        'glow-accent': '0 0 20px rgb(200 53 43 / 0.3)',
        'inner-glow': 'inset 0 2px 4px 0 rgb(0 0 0 / 0.05)',
        'card': '0 2px 8px -2px rgb(0 0 0 / 0.1), 0 2px 4px -2px rgb(0 0 0 / 0.06)',
        'card-hover': '0 8px 24px -4px rgb(0 0 0 / 0.12), 0 4px 8px -2px rgb(0 0 0 / 0.08)',
      },
      animation: {
        'fade-in': 'fadeIn 0.2s ease-out',
        'slide-up': 'slideUp 0.3s ease-out',
        'slide-down': 'slideDown 0.3s ease-out',
        'scale-in': 'scaleIn 0.2s ease-out',
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'shimmer': 'shimmer 2s linear infinite',
      },
      keyframes: {
        fadeIn: {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        slideUp: {
          '0%': { transform: 'translateY(10px)', opacity: '0' },
          '100%': { transform: 'translateY(0)', opacity: '1' },
        },
        slideDown: {
          '0%': { transform: 'translateY(-10px)', opacity: '0' },
          '100%': { transform: 'translateY(0)', opacity: '1' },
        },
        scaleIn: {
          '0%': { transform: 'scale(0.95)', opacity: '0' },
          '100%': { transform: 'scale(1)', opacity: '1' },
        },
        shimmer: {
          '0%': { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
      },
      backdropBlur: {
        xs: '2px',
      },
      transitionDuration: {
        '250': '250ms',
        '350': '350ms',
      },
    },
  },
  plugins: [],
}
