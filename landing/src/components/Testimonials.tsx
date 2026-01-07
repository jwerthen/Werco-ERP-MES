import React from 'react';

const testimonials = [
  {
    name: 'Sarah Martinez',
    role: 'Operations Manager',
    company: 'Precision Aerospace Manufacturing',
    quote: 'We went from manual spreadsheets to full visibility in 3 weeks. Audits that used to take days now take hours. The team loves the shop floor interface.',
    initial: 'SM'
  },
  {
    name: 'James Chen',
    role: 'Production Manager',
    company: 'TechFab Solutions',
    quote: 'The scheduling alone was worth the investment. We reduced bottlenecks by 40% and increased on-time deliveries significantly. Best decision we made.',
    initial: 'JC'
  },
  {
    name: 'Michael Roberts',
    role: 'Quality Director',
    company: 'Defense Components Inc.',
    quote: 'AS9100D compliance was a nightmare before. Now it\'s just built into our workflow. First Article Inspection, NCR tracking, traceability - all automated.',
    initial: 'MR'
  }
];

export default function Testimonials() {
  return (
    <section className="bg-neutral-900 text-white section-padding">
      <div className="container-custom">
        <div className="text-center mb-12">
          <h2 className="headingxl mb-4">
            Trusted by<br />
            <span className="text-primary-400">Manufacturers Like You</span>
          </h2>
          <p className="text-neutral-400 max-w-2xl mx-auto">
            See what industry leaders say about transforming their operations.
          </p>
        </div>

        <div className="grid md:grid-cols-3 gap-8">
          {testimonials.map((testimonial, index) => (
            <div
              key={testimonial.name}
              className={`card bg-white/10 backdrop-blur-sm border border-white/20 hover:scale-105 transition-transform duration-300 animate-fade-in`}
              style={{ animationDelay: `${index * 0.1}s` }}
            >
              <div className="flex items-center space-x-4 mb-6">
                <div className="w-14 h-14 bg-gradient-to-br from-primary-600 to-primary-800 rounded-full flex items-center justify-center text-white font-bold text-xl">
                  {testimonial.initial}
                </div>
                <div>
                  <div className="font-semibold text-white">{testimonial.name}</div>
                  <div className="text-sm text-neutral-400">{testimonial.role}</div>
                  <div className="text-sm text-primary-400">{testimonial.company}</div>
                </div>
              </div>

              <div className="mb-4">
                <svg className="w-8 h-8 text-primary-500/50" fill="currentColor" viewBox="0 0 24 24">
                  <path d="M14.017 21v-7.391c0-5.704 3.731-9.57 8.983-10.609l.995 2.151c-2.432.917-3.995 3.638-3.995 5.849h4v10h-9.983zm-14.017 0v-7.391c0-5.704 3.748-9.57 9-10.609l.996 2.151c-2.433.917-3.996 3.638-3.996 5.849h3.983v10h-9.983z" />
                </svg>
              </div>

              <p className="text-neutral-300 leading-relaxed">
                {testimonial.quote}
              </p>

              <div className="mt-6 flex items-center space-x-1">
                {[1, 2, 3, 4, 5].map((star) => (
                  <svg key={star} className="w-5 h-5 text-amber-400" fill="currentColor" viewBox="0 0 20 20">
                    <path d="M9.049 2.927c.3-.921 1.603-.921 1.902 0l1.07 3.292a1 1 0 00.95.69h3.462c.969 0 1.371 1.24.588 1.81l-2.8 2.034a1 1 0 00-.364 1.118l1.07 3.292c.3.921-.755 1.688-1.54 1.118l-2.8-2.034a1 1 0 00-1.175 0l-2.8 2.034c-.784.57-1.838-.197-1.539-1.118l1.07-3.292a1 1 0 00-.364-1.118L2.98 8.72c-.783-.57-.38-1.81.588-1.81h3.461a1 1 0 00.951-.69l1.07-3.292z" />
                  </svg>
                ))}
              </div>
            </div>
          ))}
        </div>

        <div className="mt-12 text-center">
          <button className="btn-secondary text-white hover:bg-white hover:text-primary-600">
            Read More Success Stories
          </button>
        </div>
      </div>
    </section>
  );
}
