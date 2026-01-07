import React from 'react';

const features = [
  {
    title: 'Shop Floor Control',
    description: 'Real-time work center management, time tracking, and job queue visibility. Operators clock in/out, report production, and see their next task.',
    icon: (
      <svg className="w-8 h-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4" />
      </svg>
    ),
    color: 'bg-blue-500',
    detail: ['Work Center Status', 'Operator Time Clock', 'Production Reporting', 'Job Queue Prioritization']
  },
  {
    title: 'Work Order Management',
    description: 'Complete work order lifecycle management with routing, operation sequencing, priority scheduling, and drag-and-drop Gantt scheduling.',
    icon: (
      <svg className="w-8 h-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" />
      </svg>
    ),
    color: 'bg-green-500',
    detail: ['Work Order Routing', 'Gantt Scheduling', 'Priority Management', 'Traveler Printing']
  },
  {
    title: 'Parts & BOM',
    description: 'Multi-level BOM support, make vs. buy classification, revision control, and critical characteristic flagging. Full parts master management.',
    icon: (
      <svg className="w-8 h-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4" />
      </svg>
    ),
    color: 'bg-purple-500',
    detail: ['Multi-Level BOMs', 'Make vs. Buy', 'Revision Control', 'Critical Features']
  },
  {
    title: 'Quality Management',
    description: 'Non-conformance reporting (NCR), corrective actions (CAR), First Article Inspection (FAI), and comprehensive lot traceability.',
    icon: (
      <svg className="w-8 h-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
      </svg>
    ),
    color: 'bg-amber-500',
    detail: ['NCR & CAR Tracking', 'First Article Inspection', 'Lot Traceability', 'Quality Holds']
  },
  {
    title: 'Purchasing & Inventory',
    description: 'PO management, receiving with inspection requirements, stock levels, and Material Requirements Planning (MRP) with reorder points.',
    icon: (
      <svg className="w-8 h-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4" />
      </svg>
    ),
    color: 'bg-orange-500',
    detail: ['PO Management', 'Receiving with QC', 'Stock Levels', 'MRP Planning']
  },
  {
    title: 'Analytics & Reporting',
    description: 'Real-time dashboards, production analytics, schedule visualization, and comprehensive reporting with export capabilities.',
    icon: (
      <svg className="w-8 h-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
      </svg>
    ),
    color: 'bg-cyan-500',
    detail: ['Real-time Dashboards', 'Production Analytics', 'Schedule Reports', 'Custom Reporting']
  }
];

export default function Features() {
  const [activeFeature, setActiveFeature] = useState(0);

  return (
    <section id="features" className="bg-neutral-50 section-padding">
      <div className="container-custom">
        <div className="text-center mb-16">
          <h2 className="headingxl text-neutral-900 mb-4">
            Everything You Need<br />
            <span className="text-primary-600">to Run Shop Floor</span>
          </h2>
          <p className="text-subtle max-w-2xl mx-auto">
            A complete ERP & MES platform designed for manufacturing excellence.
            From work order creation to shipping, everything in one place.
          </p>
        </div>

        <div className="grid lg:grid-cols-2 gap-12">
          <div className="space-y-4">
            {features.map((feature, index) => (
              <button
                key={feature.title}
                onClick={() => setActiveFeature(index)}
                className={`w-full text-left p-6 rounded-xl transition-all duration-300 ${
                  activeFeature === index
                    ? 'bg-white shadow-lg border-2 border-primary-500 scale-102'
                    : 'bg-white border border-neutral-200 hover:border-primary-300 hover:shadow-md'
                }`}
              >
                <div className="flex items-start space-x-4">
                  <div className={`p-3 rounded-lg ${feature.color} text-white flex-shrink-0`}>
                    {feature.icon}
                  </div>
                  <div className="flex-1">
                    <h3 className="heading-md mb-2 text-neutral-900">{feature.title}</h3>
                    <p className="text-neutral-600">{feature.description}</p>
                  </div>
                  {activeFeature === index && (
                    <div className="text-primary-600 flex-shrink-0">
                      <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
                        <path fillRule="evenodd" d="M10.293 3.293a1 1 0 011.414 0l6 6a1 1 0 010 1.414l-6 6a1 1 0 01-1.414-1.414L14.586 11H3a1 1 0 110-2h11.586l-4.293-4.293a1 1 0 010-1.414z" clipRule="evenodd" />
                      </svg>
                    </div>
                  )}
                </div>
              </button>
            ))}
          </div>

          <div className="relative">
            <div className="sticky top-24">
              <div className="bg-gradient-to-br from-primary-600 to-primary-800 rounded-2xl p-8 text-white shadow-xl animate-fade-in">
                <div className="flex items-center space-x-3 mb-6">
                  <div className={`p-4 rounded-xl ${features[activeFeature].color}`}>
                    <div className="text-white">
                      {React.cloneElement(features[activeFeature].icon as React.ReactElement, {
                        className: 'w-12 h-12'
                      })}
                    </div>
                  </div>
                  <div>
                    <h3 className="heading-lg mb-1">{features[activeFeature].title}</h3>
                    <p className="text-primary-100">{features[activeFeature].description}</p>
                  </div>
                </div>

                <div className="space-y-4">
                  <h4 className="font-semibold text-primary-200">Key Capabilities</h4>
                  <ul className="space-y-3">
                    {features[activeFeature].detail.map((item) => (
                      <li key={item} className="flex items-center space-x-3">
                        <div className="w-6 h-6 rounded-full bg-white/20 flex items-center justify-center flex-shrink-0">
                          <svg className="w-4 h-4 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                          </svg>
                        </div>
                        <span className="text-white/90">{item}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
