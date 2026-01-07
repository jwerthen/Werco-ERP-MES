import React from 'react';

const industries = [
  {
    name: 'Aerospace & Defense',
    icon: (
      <svg className="w-8 h-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
      </svg>
    ),
    useCases: ['AS9100D Compliance', 'CMMC Level 2', 'First Article Inspection', 'Lot Traceability']
  },
  {
    name: 'Precision Manufacturing',
    icon: (
      <svg className="w-8 h-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z" />
      </svg>
    ),
    useCases: ['Tight Tolerance Control', 'Quality Inspection', 'Tool Management', 'Job Costing']
  },
  {
    name: 'Medical Devices',
    icon: (
      <svg className="w-8 h-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4.318 6.318a4.5 4.5 0 000 6.364L12 20.364l7.682-7.682a4.5 4.5 0 00-6.364-6.364L12 7.636l-1.318-1.318a4.5 4.5 0 00-6.364 0z" />
      </svg>
    ),
    useCases: ['ISO 13485 Ready', 'Device History Record', 'Validation & Verification', 'Regulatory Compliance']
  },
  {
    name: 'Automotive',
    icon: (
      <svg className="w-8 h-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
      </svg>
    ),
    useCases: ['IATF 16949 Support', 'PPAP Management', 'Supplier Quality', 'Parts per Million Tracking']
  },
  {
    name: 'Electronics',
    icon: (
      <svg className="w-8 h-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z" />
      </svg>
    ),
    useCases: ['PCB Assembly Tracking', 'Component Traceability', 'ESD Control', 'Yield Management']
  },
  {
    name: 'Custom Fabrication',
    icon: (
      <svg className="w-8 h-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
      </svg>
    ),
    useCases: ['Job Shop Scheduling', 'Quote to Cash', 'Cost Tracking', 'Customer Portal']
  }
];

export default function Industries() {
  return (
    <section id="industries" className="bg-neutral-50 section-padding">
      <div className="container-custom">
        <div className="text-center mb-12">
          <h2 className="headingxl text-neutral-900 mb-4">
            Built for<br />
            <span className="text-primary-600">Manufacturing Excellence</span>
          </h2>
          <p className="text-subtle max-w-2xl mx-auto">
            From precision machining to complex assembly, manufacturers across industries trust our platform.
          </p>
        </div>

        <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-6">
          {industries.map((industry) => (
            <div key={industry.name} className="card group">
              <div className="flex items-center space-x-3 mb-4">
                <div className={`p-3 rounded-xl bg-primary-100 group-hover:bg-primary-600 transition-colors duration-300`}>
                  <div className={`text-primary-600 group-hover:text-white transition-colors duration-300`}>
                    {industry.icon}
                  </div>
                </div>
                <h3 className="heading-md text-neutral-900 group-hover:text-primary-600 transition-colors">
                  {industry.name}
                </h3>
              </div>

              <ul className="space-y-2">
                {industry.useCases.map((useCase) => (
                  <li key={useCase} className="flex items-center space-x-2 text-sm text-neutral-600">
                    <svg className="w-4 h-4 text-primary-500 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
                      <path fillRule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clipRule="evenodd" />
                    </svg>
                    <span>{useCase}</span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>

        <div className="mt-12 text-center">
          <div className="inline-flex items-center space-x-2 bg-white rounded-full px-6 py-3 shadow-card">
            <span className="text-neutral-600">Don't see your industry?</span>
            <a href="#" className="text-primary-600 font-semibold hover:text-primary-700 flex items-center space-x-1">
              <span>Contact us</span>
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
              </svg>
            </a>
          </div>
        </div>
      </div>
    </section>
  );
}
