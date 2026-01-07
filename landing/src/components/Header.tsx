import React, { useState } from 'react';

interface HeaderProps {
  isScrolled: boolean;
}

export default function Header({ isScrolled }: HeaderProps) {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  return (
    <header
      className={`fixed top-0 left-0 right-0 z-50 transition-all duration-300 ${
        isScrolled ? 'bg-white/95 backdrop-blur-sm shadow-lg' : 'bg-transparent'
      }`}
    >
      <div className="container-custom">
        <nav className="flex items-center justify-between h-16 md:h-20">
          <div className="flex items-center space-x-2">
            <div className="w-10 h-10 bg-primary-600 rounded-lg flex items-center justify-center">
              <svg className="w-6 h-6 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z" />
              </svg>
            </div>
            <span className={`font-bold text-xl ${isScrolled ? 'text-primary-900' : 'text-white'}`}>
              Your ERP
            </span>
          </div>

          <div className="hidden md:flex items-center space-x-8">
            <a href="#features" className={`font-medium ${isScrolled ? 'text-neutral-700 hover:text-primary-600' : 'text-white/90 hover:text-white'}`}>
              Features
            </a>
            <a href="#compliance" className={`font-medium ${isScrolled ? 'text-neutral-700 hover:text-primary-600' : 'text-white/90 hover:text-white'}`}>
              Compliance
            </a>
            <a href="#industries" className={`font-medium ${isScrolled ? 'text-neutral-700 hover:text-primary-600' : 'text-white/90 hover:text-white'}`}>
              Industries
            </a>
            <a href="#pricing" className={`font-medium ${isScrolled ? 'text-neutral-700 hover:text-primary-600' : 'text-white/90 hover:text-white'}`}>
              Pricing
            </a>
            <button className={`btn-primary ${isScrolled ? '' : '!bg-accent-500 hover:!bg-accent-600'}`}>
              Request Demo
            </button>
          </div>

          <button
            className="md:hidden"
            onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
          >
            <svg
              className={`w-6 h-6 ${isScrolled ? 'text-neutral-800' : 'text-white'}`}
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
            >
              {mobileMenuOpen ? (
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              ) : (
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
              )}
            </svg>
          </button>
        </nav>
      </div>

      {mobileMenuOpen && (
        <div className="md:hidden bg-white border-t border-neutral-200 shadow-lg">
          <div className="px-4 py-4 space-y-3">
            <a href="#features" className="block py-2 text-neutral-700 font-medium hover:text-primary-600">
              Features
            </a>
            <a href="#compliance" className="block py-2 text-neutral-700 font-medium hover:text-primary-600">
              Compliance
            </a>
            <a href="#industries" className="block py-2 text-neutral-700 font-medium hover:text-primary-600">
              Industries
            </a>
            <a href="#pricing" className="block py-2 text-neutral-700 font-medium hover:text-primary-600">
              Pricing
            </a>
            <button className="btn-primary w-full">Request Demo</button>
          </div>
        </div>
      )}
    </header>
  );
}
