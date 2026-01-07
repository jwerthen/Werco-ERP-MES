import React, { useState } from 'react';

export default function Hero() {
  const [companyName, setCompanyName] = useState('Your Company');
  const [primaryColor, setPrimaryColor] = useState('#1B4D9C');
  const [accentColor, setAccentColor] = useState('#C8352B');
  const [showCustomizer, setShowCustomizer] = useState(false);

  const colorOptions = [
    { name: 'Classic Blue', primary: '#1B4D9C', accent: '#C8352B' },
    { name: 'Tech Blue', primary: '#2563EB', accent: '#DC2626' },
    { name: 'Deep Navy', primary: '#0F172A', accent: '#F59E0B' },
    { name: 'Emerald', primary: '#059669', accent: '#DC2626' },
    { name: 'Purple', primary: '#7C3AED', accent: '#EC4899' },
  ];

  return (
    <section className="relative bg-gradient-to-br from-neutral-900 via-primary-900 to-neutral-900 text-white overflow-hidden">
      <div className="absolute inset-0 bg-[linear-gradient(to_right,#80808012_1px,transparent_1px),linear-gradient(to_bottom,#80808012_1px,transparent_1px)] bg-[size:24px_24px]" />

      <div className="absolute top-0 left-1/4 w-96 h-96 bg-primary-500/20 rounded-full blur-3xl" />
      <div className="absolute bottom-0 right-1/4 w-96 h-96 bg-accent-500/10 rounded-full blur-3xl" />

      <div className="relative container-custom section-padding">
        <div className="text-center max-w-5xl mx-auto">
          <div className="inline-flex items-center space-x-2 bg-white/10 backdrop-blur-sm rounded-full px-4 py-2 mb-8 border border-white/20">
            <span className="w-2 h-2 bg-green-400 rounded-full animate-pulse" />
            <span className="text-sm font-medium">AS9100D • ISO 9001 • CMMC Level 2 Ready</span>
          </div>

          <h1 className="headingxl mb-6 animate-fade-in">
            Manufacturing Intelligence<br />
            <span className="text-transparent bg-clip-text bg-gradient-to-r from-primary-400 to-accent-400">
              Powered for Your Success
            </span>
          </h1>

          <p className="text-xl md:text-2xl text-neutral-300 mb-8 max-w-3xl mx-auto">
            A complete ERP & MES system that adapts to your manufacturing needs.
            Personalize it with your branding, customize workflows, and go live fast.
          </p>

          <div className="flex flex-col sm:flex-row items-center justify-center gap-4 mb-12">
            <button className="btn-primary !bg-accent-500 hover:!bg-accent-600 text-lg px-8 py-4 !min-w-[200px]">
              Start 14-Day Trial
            </button>
            <button className="btn-secondary !border-white/50 text-white hover:bg-white/10">
              Schedule Demo
            </button>
          </div>

          <button
            onClick={() => setShowCustomizer(!showCustomizer)}
            className="inline-flex items-center space-x-2 text-neutral-400 hover:text-white transition-colors mb-8"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
            </svg>
            <span>Customize Your Brand</span>
          </button>

          {showCustomizer && (
            <div className="bg-white/10 backdrop-blur-sm rounded-2xl p-6 mb-12 max-w-2xl mx-auto border border-white/20 animate-slide-up">
              <div className="grid md:grid-cols-2 gap-6">
                <div>
                  <label className="block text-sm font-medium mb-2">Company Name</label>
                  <input
                    type="text"
                    value={companyName}
                    onChange={(e) => setCompanyName(e.target.value)}
                    className="w-full bg-white/10 border border-white/20 rounded-lg px-4 py-3 text-white placeholder-white/50 focus:outline-none focus:ring-2 focus:ring-primary-400"
                    placeholder="Your Company Name"
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium mb-2">Color Theme</label>
                  <div className="grid grid-cols-2 gap-2">
                    {colorOptions.map((option) => (
                      <button
                        key={option.name}
                        onClick={() => {
                          setPrimaryColor(option.primary);
                          setAccentColor(option.accent);
                        }}
                        className={`flex items-center space-x-2 px-3 py-2 rounded-lg border transition-all ${
                          primaryColor === option.primary
                            ? 'border-white/50 bg-white/20'
                            : 'border-white/10 hover:border-white/30'
                        }`}
                      >
                        <div className="flex space-x-1">
                          <div className="w-4 h-4 rounded" style={{ backgroundColor: option.primary }} />
                          <div className="w-4 h-4 rounded" style={{ backgroundColor: option.accent }} />
                        </div>
                        <span className="text-sm">{option.name}</span>
                      </button>
                    ))}
                  </div>
                </div>
              </div>

              <div className="mt-4 flex justify-center space-x-2">
                <div className="bg-white/10 rounded-xl p-4 border border-white/20">
                  <p className="text-xs text-neutral-400 mb-1">Preview Label</p>
                  <div className="font-semibold text-lg">{companyName} ERP</div>
                </div>
              </div>
            </div>
          )}

          <div className="relative rounded-2xl bg-gradient-to-br from-white/10 to-white/5 backdrop-blur-sm border border-white/20 p-8 shadow-2xl">
            <div className="absolute inset-0 bg-[linear-gradient(to_right,#80808012_1px,transparent_1px),linear-gradient(to_bottom,#80808012_1px,transparent_1px)] bg-[size:20px_20px] opacity-50" />

            <div className="relative flex items-center justify-between mb-6">
              <div className="flex items-center space-x-3">
                <div className="w-8 h-8 rounded-lg flex items-center justify-center" style={{ backgroundColor: primaryColor }}>
                  <svg className="w-5 h-5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z" />
                  </svg>
                </div>
                <span className="font-semibold text-white">{companyName}</span>
              </div>

              <div className="flex space-x-2">
                <div className="w-3 h-3 rounded-full bg-red-500" />
                <div className="w-3 h-3 rounded-full bg-yellow-500" />
                <div className="w-3 h-3 rounded-full bg-green-500" />
              </div>
            </div>

            <div className="relative grid grid-cols-4 gap-3 mb-6">
              <div className="bg-white/10 rounded-lg p-3 border border-white/10">
                <div className="text-2xl font-bold text-white">12</div>
                <div className="text-xs text-neutral-400">Active WOs</div>
              </div>
              <div className="bg-white/10 rounded-lg p-3 border border-white/10">
                <div className="text-2xl font-bold text-green-400">8</div>
                <div className="text-xs text-neutral-400">On Time</div>
              </div>
              <div className="bg-white/10 rounded-lg p-3 border border-white/10">
                <div className="text-2xl font-bold text-white">24</div>
                <div className="text-xs text-neutral-400">Work Centers</div>
              </div>
              <div className="bg-white/10 rounded-lg p-3 border border-white/10">
                <div className="text-2xl font-bold" style={{ color: accentColor }}>2</div>
                <div className="text-xs text-neutral-400">Alerts</div>
              </div>
            </div>

            <div className="relative space-y-3">
              {[1, 2, 3].map((i) => (
                <div key={i} className="bg-white/10 rounded-lg p-3 border border-white/10 flex items-center justify-between">
                  <div className="flex items-center space-x-3">
                    <div className="w-10 h-10 rounded bg-white/10 flex items-center justify-center text-sm">
                      #{1000 + i}
                    </div>
                    <div>
                      <div className="font-medium text-white text-sm">Part Assembly-{i}</div>
                      <div className="text-xs text-neutral-400">Due in {2 + i} days</div>
                    </div>
                  </div>
                  <div className="flex items-center space-x-3">
                    <div className="w-24 h-2 bg-white/20 rounded-full overflow-hidden">
                      <div
                        className="h-full rounded-full"
                        style={{
                          width: `${70 - i * 10}%`,
                          backgroundColor: i === 2 ? accentColor : primaryColor
                        }}
                      />
                    </div>
                    <span className="text-sm">{70 - i * 10}%</span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      <div className="absolute bottom-0 left-0 right-0 h-32 bg-gradient-to-t from-white to-transparent" />
    </section>
  );
}
