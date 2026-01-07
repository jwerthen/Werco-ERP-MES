import React, { useState } from 'react';

const painPoints = [
  {
    title: 'No Shop Floor Visibility',
    before: 'Walking the floor to check status, phone calls for updates, and manual status spreadsheets.',
    after: 'Real-time dashboard showing all work centers, active jobs, and production metrics.',
    icon: (
      <svg className="w-12 h-12" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
      </svg>
    )
  },
  {
    title: 'Fragmented Systems',
    before: 'Spreadsheets for tracking, email for work orders, paper travelers on the floor.',
    after: 'Single platform managing work orders, time tracking, quality, and inventory.',
    icon: (
      <svg className="w-12 h-12" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4m0 5c0 2.21-3.582 4-8 4s-8-1.79-8-4" />
      </svg>
    )
  },
  {
    title: 'Compliance Documentation',
    before: 'Manual audit trails, paper forms, and struggling to prove traceability for AS9100/ISO.',
    after: 'Built-in audit logging, lot traceability, and document control. Audit-ready always.',
    icon: (
      <svg className="w-12 h-12" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
      </svg>
    )
  },
  {
    title: 'Quality Challenges',
    before: 'Reworks discovered too late, non-conformances not tracked, recurring defects.',
    after: 'NCR/CAR workflow, First Article Inspection, lot tracking, and quality alerts.',
    icon: (
      <svg className="w-12 h-12" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
      </svg>
    )
  },
  {
    title: 'Inefficient Scheduling',
    before: 'Manual capacity planning, conflicts missed, bottlenecks discovered late.',
    after: 'Drag-and-drop Gantt scheduling, capacity visualization, and bottleneck alerts.',
    icon: (
      <svg className="w-12 h-12" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
      </svg>
    )
  },
  {
    title: 'Inventory & Purchasing',
    before: 'Stockouts, ordering late, no visibility into material requirements.',
    after: 'Real-time stock levels, MRP planning, PO management, and receiving workflow.',
    icon: (
      <svg className="w-12 h-12" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4" />
      </svg>
    )
  }
];

export default function PainPoints() {
  const [activeIndex, setActiveIndex] = useState(0);

  return (
    <section className="bg-neutral-900 text-white section-padding">
      <div className="container-custom">
        <div className="text-center mb-16">
          <h2 className="headingxl mb-4">
            The Reality of<br />
            <span className="text-primary-400">Manufacturing Chaos</span>
          </h2>
          <p className="text-neutral-400 max-w-2xl mx-auto">
            These challenges sound familiar? You're not alone.
            See how we transform them into competitive advantages.
          </p>
        </div>

        <div className="grid lg:grid-cols-3 gap-8">
          <div className="space-y-3">
            {painPoints.map((point, index) => (
              <button
                key={point.title}
                onClick={() => setActiveIndex(index)}
                className={`w-full text-left p-4 rounded-xl transition-all duration-300 ${
                  activeIndex === index
                    ? 'bg-primary-600 border-2 border-primary-400 scale-102'
                    : 'bg-neutral-800 border-2 border-transparent hover:border-neutral-600 hover:bg-neutral-700'
                }`}
              >
                <div className="flex items-center space-x-3">
                  <div className={`p-2 rounded-lg transition-colors ${
                    activeIndex === index ? 'bg-primary-500' : 'bg-neutral-700'
                  }`}>
                    {React.cloneElement(point.icon as React.ReactElement, {
                      className: 'w-5 h-5 text-white'
                    })}
                  </div>
                  <span className={`font-semibold ${activeIndex === index ? 'text-white' : 'text-neutral-300'}`}>
                    {point.title}
                  </span>
                </div>
              </button>
            ))}
          </div>

          <div className="lg:col-span-2">
            <div className="grid md:grid-cols-2 gap-6">
              <div className="bg-red-900/30 border border-red-700/50 rounded-xl p-6 animate-fade-in">
                <div className="flex items-center space-x-2 mb-4">
                  <svg className="w-6 h-6 text-red-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                  <h3 className="heading-lg text-red-400">Before</h3>
                </div>
                <p className="text-red-200 text-lg leading-relaxed">
                  {painPoints[activeIndex].before}
                </p>
              </div>

              <div className="bg-green-900/30 border border-green-700/50 rounded-xl p-6 animate-fade-in">
                <div className="flex items-center space-x-2 mb-4">
                  <svg className="w-6 h-6 text-green-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                  </svg>
                  <h3 className="heading-lg text-green-400">After</h3>
                </div>
                <p className="text-green-200 text-lg leading-relaxed">
                  {painPoints[activeIndex].after}
                </p>
              </div>
            </div>

            <div className="mt-8 bg-primary-900/50 border border-primary-700 rounded-xl p-6">
              <h4 className="font-semibold text-primary-200 mb-3">Real Results</h4>
              <div className="grid grid-cols-3 gap-4">
                <div className="text-center">
                  <div className="text-3xl font-bold text-white">40%</div>
                  <div className="text-sm text-neutral-400">Faster Cycle Times</div>
                </div>
                <div className="text-center">
                  <div className="text-3xl font-bold text-white">60%</div>
                  <div className="text-sm text-neutral-400">Less Rework</div>
                </div>
                <div className="text-center">
                  <div className="text-3xl font-bold text-white">2x</div>
                  <div className="text-sm text-neutral-400">Audit Readiness</div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
