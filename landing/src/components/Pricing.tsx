import React, { useState } from 'react';

const plans = [
  {
    name: 'Starter',
    description: 'Perfect for small job shops getting organized',
    price: 299,
    pricePeriod: 'month',
    features: [
      'Up to 5 users',
      'Work Order Management',
      Parts & BOM',
      'Shop Floor Tracking',
      'Basic Reports',
      'Email Support'
    ],
    buttonText: 'Start Free Trial',
    popular: false
  },
  {
    name: 'Professional',
    description: 'For growing manufacturers with compliance needs',
    price: 599,
    pricePeriod: 'month',
    features: [
      'Up to 20 users',
      'Everything in Starter',
      'Quality Management (NCR/CAR/FAI)',
      'Purchasing & Inventory',
      'MRP Planning',
      'Gantt Scheduling',
      'Custom Fields & Branding',
      'Priority Support'
    ],
    buttonText: 'Start Free Trial',
    popular: true
  },
  {
    name: 'Enterprise',
    description: 'For large organizations with complex needs',
    price: 'Custom',
    pricePeriod: '',
    features: [
      'Unlimited users',
      'Everything in Professional',
      'Multiple sites/facilities',
      'Advanced Analytics',
      'Custom Workflows',
      'API Access',
      'Dedicated Account Manager',
      'On-premise option'
    ],
    buttonText: 'Contact Sales',
    popular: false
  }
];

export default function Pricing() {
  const [annualBilling, setAnnualBilling] = useState(true);

  return (
    <section id="pricing" className="bg-white section-padding border-t border-neutral-200">
      <div className="container-custom">
        <div className="text-center mb-12">
          <h2 className="headingxl text-neutral-900 mb-4">
            Simple, Transparent<br />
            <span className="text-primary-600">Pricing</span>
          </h2>
          <p className="text-subtle max-w-2xl mx-auto mb-8">
            No hidden fees. No long-term contracts. Cancel anytime.
          </p>

          <div className="inline-flex items-center bg-neutral-100 rounded-full p-1 mb-8">
            <button
              onClick={() => setAnnualBilling(false)}
              className={`px-6 py-2 rounded-full font-medium transition-all ${
                !annualBilling
                  ? 'bg-white text-primary-600 shadow-md'
                  : 'text-neutral-600 hover:text-neutral-900'
              }`}
            >
              Monthly
            </button>
            <button
              onClick={() => setAnnualBilling(true)}
              className={`px-6 py-2 rounded-full font-medium transition-all ${
                annualBilling
                  ? 'bg-white text-primary-600 shadow-md'
                  : 'text-neutral-600 hover:text-neutral-900'
              }`}
            >
              Annual <span className="text-primary-600 font-bold ml-1">-20%</span>
            </button>
          </div>
        </div>

        <div className="grid md:grid-cols-3 gap-8">
          {plans.map((plan, index) => (
            <div
              key={plan.name}
              className={`card relative ${
                plan.popular
                  ? 'ring-2 ring-primary-500 scale-105 shadow-xl'
                  : 'border border-neutral-200'
              }`}
            >
              {plan.popular && (
                <div className="absolute top-0 left-1/2 transform -translate-x-1/2 -translate-y-1/2">
                  <span className="bg-primary-600 text-white text-sm font-semibold px-4 py-1 rounded-full">
                    Most Popular
                  </span>
                </div>
              )}

              <div className="text-center mb-8">
                <h3 className="heading-lg text-neutral-900 mb-2">{plan.name}</h3>
                <p className="text-neutral-600 text-sm mb-6">{plan.description}</p>

                <div className="mb-2">
                  {typeof plan.price === 'number' ? (
                    <div className="text-5xl font-bold text-neutral-900">
                      ${Math.round(plan.price * (annualBilling ? 0.8 : 1))}
                    </div>
                  ) : (
                    <div className="text-5xl font-bold text-neutral-900">{plan.price}</div>
                  )}
                </div>
                {plan.pricePeriod && (
                  <div className="text-neutral-600">
                    /{plan.pricePeriod}
                    {annualBilling && (
                      <span className="block text-sm text-primary-600 font-medium mt-1">
                        Billed annually
                      </span>
                    )}
                  </div>
                )}
              </div>

              <ul className="space-y-4 mb-8">
                {plan.features.map((feature) => (
                  <li key={feature} className="flex items-start space-x-3">
                    <svg className="w-5 h-5 text-green-500 flex-shrink-0 mt-0.5" fill="currentColor" viewBox="0 0 20 20">
                      <path fillRule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clipRule="evenodd" />
                    </svg>
                    <span className="text-neutral-700">{feature}</span>
                  </li>
                ))}
              </ul>

              <button
                className={`w-full py-3 rounded-lg font-semibold transition-all ${
                  plan.popular
                    ? 'btn-primary'
                    : 'bg-white border-2 border-primary-600 text-primary-600 hover:bg-primary-50'
                }`}
              >
                {plan.buttonText}
              </button>
            </div>
          ))}
        </div>

        <div className="mt-12 text-center">
          <div className="inline-flex items-center space-x-4 text-sm text-neutral-600">
            <div className="flex items-center space-x-2">
              <svg className="w-5 h-5 text-green-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
              </svg>
              <span>14-day free trial</span>
            </div>
            <div className="flex items-center space-x-2">
              <svg className="w-5 h-5 text-green-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
              </svg>
              <span>No credit card required</span>
            </div>
            <div className="flex items-center space-x-2">
              <svg className="w-5 h-5 text-green-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
              </svg>
              <span>Cancel anytime</span>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
