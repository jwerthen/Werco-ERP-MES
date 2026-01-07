import React from 'react';

const steps = [
  {
    number: '01',
    title: 'Configure Your Brand',
    description: 'Customize color themes, add your logo, and set up company terminology. Make it feel like your own system.',
    icon: (
      <svg className="w-8 h-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 21a4 4 0 01-4-4V5a2 2 0 012-2h4a2 2 0 012 2v12a4 4 0 01-4 4zm0 0h12a2 2 0 002-2v-4a2 2 0 00-2-2h-2.343M11 7.343l1.657-1.657a2 2 0 012.828 0l2.829 2.829a2 2 0 010 2.828l-8.486 8.485M7 17h.01" />
      </svg>
    )
  },
  {
    number: '02',
    title: 'Import Your Data',
    description: 'Easy import tools for parts, customers, BOMs, and existing data. Don't start from scratch.',
    icon: (
      <svg className="w-8 h-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
      </svg>
    )
  },
  {
    number: '03',
    title: 'Train Your Team',
    description: 'Built-in onboarding, video tutorials, and personalized training. Your team will be productive in days, not months.',
    icon: (
      <svg className="w-8 h-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253" />
      </svg>
    )
  },
  {
    number: '04',
    title: 'Go Live',
    description: 'Roll out to production with our migration support. Start seeing results immediately.',
    icon: (
      <svg className="w-8 h-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
      </svg>
    )
  }
];

export default function HowItWorks() {
  return (
    <section className="bg-white section-padding border-t border-neutral-200">
      <div className="container-custom">
        <div className="text-center mb-16">
          <h2 className="headingxl text-neutral-900 mb-4">
            Go Live in<br />
            <span className="text-primary-600">Weeks, Not Months</span>
          </h2>
          <p className="text-subtle max-w-2xl mx-auto">
            No complex infrastructure setup. No IT team required. We handle everything.
          </p>
        </div>

        <div className="grid md:grid-cols-2 lg:grid-cols-4 gap-8">
          {steps.map((step, index) => (
            <div key={step.number} className="relative group">
              <div className="card h-full relative overflow-hidden">
                <div className="absolute top-0 right-0 text-8xl font-bold text-primary-100 opacity-50 group-hover:text-primary-200 transition-colors">
                  {step.number}
                </div>

                <div className="relative z-10">
                  <div className="mb-6 bg-primary-100 rounded-xl p-4 group-hover:bg-primary-600 transition-colors duration-300 inline-block">
                    <div className="text-primary-600 group-hover:text-white transition-colors duration-300">
                      {step.icon}
                    </div>
                  </div>

                  <h3 className="heading-lg mb-3 text-neutral-900 group-hover:text-primary-600 transition-colors">
                    {step.title}
                  </h3>

                  <p className="text-neutral-600 leading-relaxed">
                    {step.description}
                  </p>
                </div>
              </div>

              {index < steps.length - 1 && (
                <div className="hidden lg:block absolute top-1/2 -right-4 transform -translate-y-1/2 z-20">
                  <div className="w-8 h-8 bg-primary-600 rounded-full flex items-center justify-center shadow-lg">
                    <svg className="w-4 h-4 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                    </svg>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>

        <div className="mt-16 bg-gradient-to-br from-primary-600 to-primary-800 rounded-2xl p-8 md:p-12 text-white">
          <div className="grid md:grid-cols-3 gap-8">
            <div className="text-center">
              <div className="text-5xl font-bold mb-2">2-4</div>
              <div className="text-primary-200">Weeks to Go Live</div>
            </div>
            <div className="text-center">
              <div className="text-5xl font-bold mb-2">&lt;1</div>
              <div className="text-primary-200">Hour per Day Training</div>
            </div>
            <div className="text-center">
              <div className="text-5xl font-bold mb-2">95%</div>
              <div className="text-primary-200">Adoption Rate</div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
