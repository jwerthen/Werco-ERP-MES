/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./src/**/*.{js,jsx,ts,tsx}', './public/index.html'],
  theme: {
    // Custom breakpoints for better mobile support
    screens: {
      xs: '375px', // Small phones
      sm: '640px', // Large phones / landscape
      md: '768px', // Tablets
      lg: '1024px', // Small laptops
      xl: '1280px', // Desktops
      '2xl': '1536px', // Large desktops
    },
    extend: {
      colors: {
        // Werco brand navy - matches wercomfg.com deep navy
        'werco-navy': {
          50: '#eef3ff',
          100: '#dae4ff',
          200: '#bccfff',
          300: '#8eafff',
          400: '#5985ff',
          500: '#3361ff',
          600: '#1B4D9C', // Primary brand blue
          700: '#163d7d',
          800: '#123266',
          900: '#0f2952',
          950: '#0a1628', // Deep navy from website
        },
        // Werco brand blue - bright accent from wercomfg.com CTAs
        werco: {
          50: '#eef4ff',
          100: '#d9e6ff',
          200: '#bcd3ff',
          300: '#8eb8ff',
          400: '#5990ff',
          500: '#3366ff', // Bright blue accent
          600: '#1B4D9C', // Primary brand
          700: '#1a3f7a',
          800: '#1b3664',
          900: '#1c3154',
          950: '#131f35',
        },
        // Accent - precision red for alerts/critical actions
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
        // Steel gray - body text from wercomfg.com
        steel: {
          50: '#f8fafc',
          100: '#f1f5f9',
          200: '#e2e8f0',
          300: '#cbd5e1',
          400: '#94a3b8',
          500: '#64748b',
          600: '#475569',
          700: '#334155',
          800: '#1e293b',
          900: '#0f172a',
          950: '#020617',
        },
        // Surface colors - Foundry near-black instrument-panel palette.
        // 50-300 are background/border surfaces; 400-950 are text (inverted for dark).
        surface: {
          50: '#1b2330',   // raised surface / nested tiles
          100: '#141b26',  // card/panel backgrounds
          200: '#243042',  // hairline borders, dividers
          300: '#32415a',  // heavier / emphasis borders
          400: '#8a98ab',  // muted text / labels
          500: '#b7c3d4',  // secondary / body text
          600: '#c3cedd',  // body text
          700: '#dbe2ec',  // primary text
          800: '#f0f4f9',  // headings
          900: '#f6f8fb',  // strongest text
          950: '#ffffff',  // maximum contrast text
        },
        // Foundry tactical accents (instrument-panel status palette)
        fd: {
          canvas: '#0d1117',
          panel: '#141b26',
          raised: '#1b2330',
          sunken: '#0a0e15',
          line: '#243042',
          'line-bright': '#32415a',
          ink: '#f0f4f9',
          body: '#b7c3d4',
          mute: '#8a98ab',
          faint: '#616f82',
          blue: '#2f81f7',
          red: '#f04438',
          amber: '#d29922',
          green: '#3fb950',
          cyan: '#39c5cf',
        },
        // Status colors - high contrast for quick scanning (Foundry tactical)
        status: {
          success: '#3fb950',
          'success-light': '#d1fae5',
          warning: '#d29922',
          'warning-light': '#fef3c7',
          danger: '#f04438',
          'danger-light': '#fee2e2',
          info: '#2f81f7',
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
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['JetBrains Mono', 'Fira Code', 'Consolas', 'Monaco', 'monospace'],
        display: ['Inter', 'system-ui', 'sans-serif'],
      },
      fontSize: {
        // Slightly larger base for shop floor readability
        xs: ['0.75rem', { lineHeight: '1rem' }],
        sm: ['0.875rem', { lineHeight: '1.25rem' }],
        base: ['1rem', { lineHeight: '1.5rem' }],
        lg: ['1.125rem', { lineHeight: '1.75rem' }],
        xl: ['1.25rem', { lineHeight: '1.75rem' }],
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
        11: '2.75rem',
        13: '3.25rem',
        15: '3.75rem',
        18: '4.5rem',
        22: '5.5rem',
        30: '7.5rem',
      },
      // Foundry: sharp corners everywhere (3-4px workhorse). rounded-full stays full.
      borderRadius: {
        sm: '2px',
        DEFAULT: '3px',
        md: '3px',
        lg: '4px',
        xl: '4px',
        '2xl': '6px',
        '3xl': '8px',
      },
      // Foundry: near-flat elevation; depth comes from hairline borders, not shadow.
      boxShadow: {
        sm: '0 1px 2px 0 rgb(0 0 0 / 0.4)',
        DEFAULT: '0 1px 3px 0 rgb(0 0 0 / 0.5)',
        md: '0 2px 8px -2px rgb(0 0 0 / 0.5)',
        lg: '0 8px 24px -6px rgb(0 0 0 / 0.55)',
        xl: '0 16px 40px -10px rgb(0 0 0 / 0.6)',
        glow: '0 0 20px rgb(47 129 247 / 0.3)',
        'glow-blue': '0 0 20px rgb(47 129 247 / 0.35)',
        'glow-accent': '0 0 20px rgb(240 68 56 / 0.35)',
        'inner-glow': 'inset 0 1px 3px 0 rgb(0 0 0 / 0.4)',
        card: '0 1px 3px 0 rgb(0 0 0 / 0.5)',
        'card-hover': '0 2px 10px -2px rgb(0 0 0 / 0.55)',
      },
      animation: {
        'fade-in': 'fadeIn 0.2s ease-out',
        'slide-up': 'slideUp 0.3s ease-out',
        'slide-down': 'slideDown 0.3s ease-out',
        'scale-in': 'scaleIn 0.2s ease-out',
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        shimmer: 'shimmer 2s linear infinite',
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
        250: '250ms',
        350: '350ms',
      },
    },
  },
  plugins: [],
};
