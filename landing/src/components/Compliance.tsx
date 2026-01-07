import React from 'react';

const compliance = [
  {
    standard: 'AS9100D',
    full: 'Quality Management System for Aviation, Space and Defense',
    description: 'Aerospace quality requirements built into every workflow',
    features: ['Document Control', 'Lot Traceability', 'First Article Inspection', 'Audit Trail']
  },
  {
    standard: 'ISO 9001',
    full: 'Quality Management System',
    description: 'International quality standards embedded in the platform',
    features: ['Process Control', 'Quality Records', 'Management Review', 'Continual Improvement']
  },
  {
    standard: 'CMMC',
    full: 'Cybersecurity Maturity Model Certification Level 2',
    description: 'Defense contractor cybersecurity requirements',
    features: ['Access Control', 'Audit Logging', 'Account Management', 'Incident Response']
  }
];

export default function Compliance() {
  return (
    <section id="compliance" className="bg-white section-padding border-t border-neutral-200">
      <div className="container-custom">
        <div className="text-center mb-12">
          <h2 className="headingxl text-neutral-900 mb-4">
            Compliance Built<br>
            <span className="text-primary-600">Into Every Workflow</span>
          </h2>
          <p className="text-subtle max-w-2xl mx-auto">
            Never worry about audit preparation again. Industry standards are embedded in every aspect of the platform.
          </p>
        </div>

        <div className="grid md:grid-cols-3 gap-6 mb-16">
          {compliance.map((c) => (
            <div key={c.standard} className="card group hover:scale-105 transition-transform duration-300">
              <div className="flex items-center space-x-3 mb-4">
                <div className="w-12 h-12 bg-primary-100 rounded-xl flex items-center justify-center">
                  <svg className="w-7 h-7 text-primary-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
                  </svg>
                </div>
                <div>
                  <div className="text-2xl font-bold text-primary-600">{c.standard}</div>
                  <div className="text-xs text-neutral-500">{c.full}</div>
                </div>
              </div>

              <p className="text-neutral-600 mb-4">{c.description}</p>

              <ul className="space-y-2">
                {c.features.map((f) => (
                  <li key={f} className="flex items-center space-x-2 text-sm text-neutral-700">
                    <svg className="w-4 h-4 text-green-500 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
                      <path fillRule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clipRule="evenodd" />
                    </svg>
                    <span>{f}</span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>

        <div className="bg-gradient-to-br from-primary-600 to-primary-800 rounded-2xl p-8 md:p-12 text-white">
          <div className="grid md:grid-cols-2 gap-8 items-center">
            <div>
              <h3 className="heading-lg mb-4">Audit-Ready Whenever They Come</h3>
              <p className="text-primary-100 mb-6 leading-relaxed">
                When auditors walk through the door, you won't scramble. Every transaction, every change,
                every user action is automatically logged and retrievable. Generate reports in minutes, not days.
              </p>
              <div className="flex items-center space-x-2 text-primary-200">
                <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                </svg>
                <span className="font-medium">Full traceability from raw material to finished product</span>
              </div>
            </div>
            <div className="bg-white/10 backdrop-blur-sm rounded-xl p-6 border border-white/20">
              <div className="space-y-4">
                <div className="flex items-center space-x-3">
                  <div className="w-10 h-10 rounded-lg bg-green-500/20 flex items-center justify-center">
                    <svg className="w-6 h-6 text-green-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                  </div>
                  <div>
                    <div className="font-semibold">Audit Trail</div>
                    <div className="text-sm text-primary-200">Automatic logging of all actions</div>
                  </div>
                </div>
                <div className="flex items-center space-x-3">
                  <div className="w-10 h-10 rounded-lg bg-blue-500/20 flex items-center justify-center">
                    <svg className="w-6 h-6 text-blue-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                    </svg>
                  </div>
                  <div>
                    <div className="font-semibold">Document Control</div>
                    <div className="text-sm text-primary-200">Revision tracking with history</div>
                  </div>
                </div>
                <div className="flex items-center space-x-3">
                  <div className="w-10 h-10 rounded-lg bg-purple-500/20 flex items-center justify-center">
                    <svg className="w-6 h-6 text-purple-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3.055 11H5a2 2 0 012 2v1a2 2 0 002 2 2 2 0 012 2v2.945M8 3.935V5.5A2.5 2.5 0 0010.5 8h.5a2 2 0 012 2 2 2 0 104 0 2 2 0 012-2h1.064M15 20.488V18a2 2 0 012-2h3.064M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                  </div>
                  <div>
                    <div className="font-semibold">Lot Traceability</div>
                    <div className="text-sm text-primary-200">Full genealogy tracking</div>
                  </div>
                </div>
                <div className="flex items-center space-x-3">
                  <div className="w-10 h-10 rounded-lg bg-amber-500/20 flex items-center justify-center">
                    <svg className="w-6 h-6 text-amber-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
                    </svg>
                  </div>
                  <div>
                    <div className="font-semibold">Role-Based Access</div>
                    <div className="text-sm text-primary-200">Granular permissions control</div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
