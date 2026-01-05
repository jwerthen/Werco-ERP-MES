import React, { useEffect, useState } from 'react';
import api from '../services/api';
import { WorkCenter, QueueItem, ActiveJob } from '../types';
import { format } from 'date-fns';
import {
  PlayIcon,
  StopIcon,
  ClockIcon,
  CheckCircleIcon,
} from '@heroicons/react/24/solid';

export default function ShopFloor() {
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [selectedWorkCenter, setSelectedWorkCenter] = useState<number | null>(null);
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [activeJob, setActiveJob] = useState<ActiveJob | null>(null);
  const [loading, setLoading] = useState(true);
  const [clockOutModal, setClockOutModal] = useState(false);
  const [clockOutData, setClockOutData] = useState({ quantity_produced: 0, quantity_scrapped: 0, notes: '' });

  useEffect(() => {
    loadInitialData();
    const interval = setInterval(checkActiveJob, 10000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    if (selectedWorkCenter) {
      loadQueue(selectedWorkCenter);
    }
  }, [selectedWorkCenter]);

  const loadInitialData = async () => {
    try {
      const [wcResponse, activeResponse] = await Promise.all([
        api.getWorkCenters(),
        api.getMyActiveJob()
      ]);
      setWorkCenters(wcResponse);
      setActiveJob(activeResponse.active_job);
      if (wcResponse.length > 0) {
        setSelectedWorkCenter(wcResponse[0].id);
      }
    } catch (err) {
      console.error('Failed to load data:', err);
    } finally {
      setLoading(false);
    }
  };

  const checkActiveJob = async () => {
    try {
      const response = await api.getMyActiveJob();
      setActiveJob(response.active_job);
    } catch (err) {
      console.error('Failed to check active job:', err);
    }
  };

  const loadQueue = async (workCenterId: number) => {
    try {
      const response = await api.getWorkCenterQueue(workCenterId);
      setQueue(response.queue);
    } catch (err) {
      console.error('Failed to load queue:', err);
    }
  };

  const handleClockIn = async (item: QueueItem) => {
    if (activeJob) {
      alert('You are already clocked in to a job. Please clock out first.');
      return;
    }

    try {
      await api.clockIn({
        work_order_id: item.work_order_id,
        operation_id: item.operation_id,
        work_center_id: selectedWorkCenter!,
        entry_type: 'run'
      });
      await checkActiveJob();
      loadQueue(selectedWorkCenter!);
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to clock in');
    }
  };

  const handleClockOut = async () => {
    if (!activeJob) return;

    try {
      await api.clockOut(activeJob.time_entry_id, {
        quantity_produced: clockOutData.quantity_produced,
        quantity_scrapped: clockOutData.quantity_scrapped,
        notes: clockOutData.notes
      });
      setActiveJob(null);
      setClockOutModal(false);
      setClockOutData({ quantity_produced: 0, quantity_scrapped: 0, notes: '' });
      if (selectedWorkCenter) {
        loadQueue(selectedWorkCenter);
      }
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to clock out');
    }
  };

  const getElapsedTime = (clockIn: string) => {
    const start = new Date(clockIn);
    const now = new Date();
    const diff = now.getTime() - start.getTime();
    const hours = Math.floor(diff / 3600000);
    const minutes = Math.floor((diff % 3600000) / 60000);
    return `${hours}h ${minutes}m`;
  };

  const formatClockInTime = (clockIn: string) => {
    const date = new Date(clockIn);
    return format(date, 'h:mm a');
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-werco-primary"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-gray-900">Shop Floor</h1>

      {/* Active Job Banner */}
      {activeJob && (
        <div className="bg-green-50 border border-green-200 rounded-lg p-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center">
              <div className="animate-pulse mr-3">
                <div className="h-4 w-4 rounded-full bg-green-500"></div>
              </div>
              <div>
                <p className="font-medium text-green-800">Currently Working On</p>
                <p className="text-lg font-bold text-green-900">
                  {activeJob.work_order_number} - {activeJob.operation_name}
                </p>
                <p className="text-sm text-green-700">
                  {activeJob.part_number} - {activeJob.part_name}
                </p>
              </div>
            </div>
            <div className="text-right">
              <div className="text-sm text-green-600 mb-1">
                Started at {formatClockInTime(activeJob.clock_in)}
              </div>
              <div className="flex items-center text-green-700 mb-2">
                <ClockIcon className="h-5 w-5 mr-1" />
                <span className="font-mono text-lg">{getElapsedTime(activeJob.clock_in)}</span>
              </div>
              <button
                onClick={() => setClockOutModal(true)}
                className="btn-danger flex items-center"
              >
                <StopIcon className="h-5 w-5 mr-2" />
                Clock Out
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Work Center Selector */}
      <div className="flex flex-wrap gap-2">
        {workCenters.map((wc) => (
          <button
            key={wc.id}
            onClick={() => setSelectedWorkCenter(wc.id)}
            className={`px-4 py-2 rounded-lg font-medium transition-colors ${
              selectedWorkCenter === wc.id
                ? 'bg-werco-primary text-white'
                : 'bg-white text-gray-700 border border-gray-300 hover:bg-gray-50'
            }`}
          >
            {wc.name}
          </button>
        ))}
      </div>

      {/* Job Queue */}
      <div className="card">
        <h2 className="text-lg font-semibold text-gray-900 mb-4">
          Job Queue - {workCenters.find(wc => wc.id === selectedWorkCenter)?.name}
        </h2>
        
        {queue.length === 0 ? (
          <p className="text-gray-500 text-center py-8">No jobs in queue</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Priority</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Work Order</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Part</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Operation</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Qty</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Due Date</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Action</th>
                </tr>
              </thead>
              <tbody className="bg-white divide-y divide-gray-200">
                {queue.map((item) => (
                  <tr key={item.operation_id} className="hover:bg-gray-50">
                    <td className="px-4 py-4">
                      <span className={`inline-flex items-center justify-center w-8 h-8 rounded-full text-sm font-bold ${
                        item.priority <= 2 ? 'bg-red-100 text-red-800' :
                        item.priority <= 5 ? 'bg-yellow-100 text-yellow-800' :
                        'bg-gray-100 text-gray-800'
                      }`}>
                        {item.priority}
                      </span>
                    </td>
                    <td className="px-4 py-4">
                      <span className="font-medium text-werco-primary">{item.work_order_number}</span>
                    </td>
                    <td className="px-4 py-4">
                      <div>
                        <div className="font-medium">{item.part_number}</div>
                        <div className="text-sm text-gray-500">{item.part_name}</div>
                      </div>
                    </td>
                    <td className="px-4 py-4">
                      <div>
                        <div className="font-medium">{item.operation_number}</div>
                        <div className="text-sm text-gray-500">{item.operation_name}</div>
                      </div>
                    </td>
                    <td className="px-4 py-4">
                      <span className="font-medium">{item.quantity_complete}</span>
                      <span className="text-gray-500">/{item.quantity_ordered}</span>
                    </td>
                    <td className="px-4 py-4 text-sm">
                      {item.due_date ? format(new Date(item.due_date), 'MMM d, yyyy') : '-'}
                    </td>
                    <td className="px-4 py-4">
                      <span className={`inline-flex px-2 py-1 rounded-full text-xs font-medium ${
                        item.status === 'in_progress' ? 'bg-green-100 text-green-800' :
                        item.status === 'ready' ? 'bg-blue-100 text-blue-800' :
                        'bg-gray-100 text-gray-800'
                      }`}>
                        {item.status.replace('_', ' ')}
                      </span>
                    </td>
                    <td className="px-4 py-4">
                      {item.status === 'in_progress' ? (
                        <span className="text-green-600 flex items-center">
                          <CheckCircleIcon className="h-5 w-5 mr-1" />
                          In Progress
                        </span>
                      ) : (
                        <button
                          onClick={() => handleClockIn(item)}
                          disabled={!!activeJob}
                          className="btn-success flex items-center disabled:opacity-50"
                        >
                          <PlayIcon className="h-4 w-4 mr-1" />
                          Start
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Clock Out Modal */}
      {clockOutModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-md w-full mx-4">
            <h3 className="text-lg font-semibold mb-4">Clock Out</h3>
            
            <div className="space-y-4">
              <div>
                <label className="label">Quantity Produced</label>
                <input
                  type="number"
                  min="0"
                  value={clockOutData.quantity_produced}
                  onChange={(e) => setClockOutData({ ...clockOutData, quantity_produced: parseFloat(e.target.value) || 0 })}
                  className="input"
                />
              </div>
              
              <div>
                <label className="label">Quantity Scrapped</label>
                <input
                  type="number"
                  min="0"
                  value={clockOutData.quantity_scrapped}
                  onChange={(e) => setClockOutData({ ...clockOutData, quantity_scrapped: parseFloat(e.target.value) || 0 })}
                  className="input"
                />
              </div>
              
              <div>
                <label className="label">Notes</label>
                <textarea
                  value={clockOutData.notes}
                  onChange={(e) => setClockOutData({ ...clockOutData, notes: e.target.value })}
                  className="input"
                  rows={3}
                />
              </div>
            </div>
            
            <div className="flex justify-end gap-3 mt-6">
              <button
                onClick={() => setClockOutModal(false)}
                className="btn-secondary"
              >
                Cancel
              </button>
              <button
                onClick={handleClockOut}
                className="btn-primary"
              >
                Complete Clock Out
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
