import React, { useState } from 'react';

export default function InteractiveDemo() {
  const [activeView, setActiveView] = useState<'dashboard' | 'schedule' | 'queue'>('dashboard');

  const workCenters = [
    { name: 'CNC-01', status: 'running', progress: 65, currentJob: 'WO-1050' },
    { name: 'CNC-02', status: 'idle', progress: 0, currentJob: null },
    { name: 'Laser-01', status: 'running', progress: 40, currentJob: 'WO-1045' },
    { name: 'Weld-01', status: 'maintenance', progress: 0, currentJob: null },
    { name: 'Assembly-01', status: 'running', progress: 85, currentJob: 'WO-1048' },
    { name: 'Inspection-01', status: 'idle', progress: 0, currentJob: null },
  ];

  const jobs = [
    { id: 'WO-1050', part: 'ASM-2001', progress: 65, status: 'in-progress' },
    { id: 'WO-1048', part: 'ASM-2005', progress: 85, status: 'in-progress' },
    { id: 'WO-1045', part: 'PART-3012', progress: 40, status: 'in-progress' },
    { id: 'WO-1040', part: 'ASM-1998', progress: 100, status: 'complete' },
    { id: 'WO-1035', part: 'PART-2985', progress: 20, status: 'in-progress' },
  ];

  const statusColors = {
    running: 'bg-green-500',
    idle: 'bg-neutral-400',
    maintenance: 'bg-amber-500',
  };

  return (
    <section className="bg-white section-padding border-y border-neutral-200">
      <div className="container-custom">
        <div className="text-center mb-12">
          <h2 className="headingxl text-neutral-900 mb-4">
            See It In Action
          </h2>
          <p className="text-subtle max-w-2xl mx-auto">
            An intuitive interface designed for shop floor efficiency.
            Operators love it, managers trust it.
          </p>
        </div>

        <div className="flex flex-col lg:flex-row gap-8">
          <div className="flex-shrink-0 lg:w-64">
            <div className="bg-neutral-50 rounded-xl p-4 sticky top-24">
              <h3 className="font-semibold mb-4 text-neutral-900">Explore Views</h3>
              <div className="space-y-2">
                <button
                  onClick={() => setActiveView('dashboard')}
                  className={`w-full text-left px-4 py-3 rounded-lg transition-all ${
                    activeView === 'dashboard'
                      ? 'bg-primary-600 text-white shadow-md'
                      : 'bg-white hover:bg-neutral-100 text-neutral-700'
                  }`}
                >
                  Dashboard
                </button>
                <button
                  onClick={() => setActiveView('schedule')}
                  className={`w-full text-left px-4 py-3 rounded-lg transition-all ${
                    activeView === 'schedule'
                      ? 'bg-primary-600 text-white shadow-md'
                      : 'bg-white hover:bg-neutral-100 text-neutral-700'
                  }`}
                >
                  Gantt Schedule
                </button>
                <button
                  onClick={() => setActiveView('queue')}
                  className={`w-full text-left px-4 py-3 rounded-lg transition-all ${
                    activeView === 'queue'
                      ? 'bg-primary-600 text-white shadow-md'
                      : 'bg-white hover:bg-neutral-100 text-neutral-700'
                  }`}
                >
                  Work Queue
                </button>
              </div>

              <div className="mt-6 p-4 bg-primary-50 rounded-lg">
                <p className="text-sm text-primary-800">
                  <strong className="block font-semibold mb-1">Pro Tip</strong>
                  Operators can access all views from shop floor kiosks with touch-optimized navigation.
                </p>
              </div>
            </div>
          </div>

          <div className="flex-1">
            <div className="bg-neutral-900 rounded-2xl overflow-hidden shadow-2xl">
              <div className="bg-neutral-800 px-4 py-3 flex items-center justify-between">
                <div className="flex items-center space-x-2">
                  <div className="w-3 h-3 rounded-full bg-red-500" />
                  <div className="w-3 h-3 rounded-full bg-yellow-500" />
                  <div className="w-3 h-3 rounded-full bg-green-500" />
                </div>
                <div className="text-neutral-400 text-sm font-mono">
                  app.manufacturing-erp.com
                </div>
                <div className="flex items-center space-x-2">
                  <div className="w-8 h-8 rounded-full bg-primary-600 flex items-center justify-center text-white text-sm font-semibold">
                    JD
                  </div>
                </div>
              </div>

              <div className="bg-white min-h-[500px] p-6">
                {activeView === 'dashboard' && (
                  <div className="space-y-6 animate-fade-in">
                    <div className="grid grid-cols-3 gap-4">
                      <div className="bg-gradient-to-br from-blue-50 to-blue-100 rounded-xl p-4 border border-blue-200">
                        <div className="text-3xl font-bold text-blue-700">12</div>
                        <div className="text-sm text-blue-600 font-medium">Active Work Orders</div>
                      </div>
                      <div className="bg-gradient-to-br from-green-50 to-green-100 rounded-xl p-4 border border-green-200">
                        <div className="text-3xl font-bold text-green-700">8</div>
                        <div className="text-sm text-green-600 font-medium">On Schedule</div>
                      </div>
                      <div className="bg-gradient-to-br from-amber-50 to-amber-100 rounded-xl p-4 border border-amber-200">
                        <div className="text-3xl font-bold text-amber-700">2</div>
                        <div className="text-sm text-amber-600 font-medium">Attention Required</div>
                      </div>
                    </div>

                    <div>
                      <h3 className="font-semibold mb-3 text-neutral-900">Work Center Status</h3>
                      <div className="grid grid-cols-3 gap-3">
                        {workCenters.map((wc) => (
                          <div key={wc.name} className="bg-neutral-50 rounded-lg p-3 border border-neutral-200">
                            <div className="flex items-center justify-between mb-2">
                              <span className="font-medium text-sm text-neutral-900">{wc.name}</span>
                              <div className={`w-2 h-2 rounded-full ${statusColors[wc.status as keyof typeof statusColors]}`} />
                            </div>
                            {wc.currentJob && (
                              <div className="text-xs text-neutral-600 mb-2">{wc.currentJob}</div>
                            )}
                            {wc.progress > 0 && (
                              <div className="w-full h-1.5 bg-neutral-200 rounded-full overflow-hidden">
                                <div
                                  className="h-full bg-primary-600 transition-all duration-500"
                                  style={{ width: `${wc.progress}%` }}
                                />
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                )}

                {activeView === 'schedule' && (
                  <div className="animate-fade-in">
                    <div className="space-y-4">
                      {jobs.map((job) => (
                        <div key={job.id} className="bg-neutral-50 rounded-lg p-4 border border-neutral-200">
                          <div className="flex items-center justify-between mb-2">
                            <div>
                              <div className="font-semibold text-sm text-neutral-900">{job.id}</div>
                              <div className="text-xs text-neutral-600">{job.part}</div>
                            </div>
                            <div className="flex items-center space-x-3">
                              <div className="w-48 h-2 bg-neutral-200 rounded-full overflow-hidden">
                                <div
                                  className="h-full bg-primary-600 rounded-full"
                                  style={{ width: `${job.progress}%` }}
                                />
                              </div>
                              <span className="text-sm font-medium text-neutral-700">{job.progress}%</span>
                            </div>
                          </div>

                          <div className="flex items-center space-x-2 mt-3">
                            <div className="flex-1 bg-blue-100 rounded px-3 py-1.5 text-xs text-blue-800 font-medium">
                              Fabrication
                            </div>
                            <div className="flex-1 bg-green-100 rounded px-3 py-1.5 text-xs text-green-800 font-medium">
                              CNC
                            </div>
                            <div className="flex-1 bg-purple-100 rounded px-3 py-1.5 text-xs text-purple-800 font-medium">
                              Weld
                            </div>
                            <div className="flex-1 bg-amber-100 rounded px-3 py-1.5 text-xs text-amber-800 font-medium">
                              Paint
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {activeView === 'queue' && (
                  <div className="animate-fade-in">
                    <div className="space-y-3">
                      <div className="bg-primary-600 text-white rounded-lg p-3">
                        <h3 className="font-semibold text-sm">My Active Job</h3>
                        <div className="text-xs text-primary-100 mt-1">Started 2 hours ago</div>
                      </div>

                      {['WO-1052', 'WO-1051', 'WO-1050', 'WO-1049'].map((wo, i) => (
                        <div
                          key={wo}
                          className={`rounded-lg p-3 border transition-all ${
                            i === 0
                              ? 'border-primary-300 bg-primary-50'
                              : 'border-neutral-200 bg-white hover:border-primary-300'
                          }`}
                        >
                          <div className="flex items-center justify-between">
                            <div>
                              <div className={`font-semibold text-sm ${i === 0 ? 'text-primary-900' : 'text-neutral-900'}`}>
                                {wo}
                              </div>
                              <div className="text-xs text-neutral-600 mt-1">
                                {i === 0 ? 'Part Assembly-2001 - Priority: High' : 'Pending Assignment'}
                              </div>
                            </div>
                            {i === 0 ? (
                              <button className="btn-primary text-sm !px-4 !py-2">Continue</button>
                            ) : (
                              <div className="text-xs text-neutral-400">Queue Position: {i}</div>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </div>

            <div className="mt-6 flex items-center justify-center space-x-8 text-sm text-neutral-600">
              <div className="flex items-center space-x-2">
                <svg className="w-5 h-5 text-green-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                </svg>
                <span>Touch-compatible</span>
              </div>
              <div className="flex items-center space-x-2">
                <svg className="w-5 h-5 text-green-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                </svg>
                <span>Mobile responsive</span>
              </div>
              <div className="flex items-center space-x-2">
                <svg className="w-5 h-5 text-green-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                </svg>
                <span>Offline capable</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
