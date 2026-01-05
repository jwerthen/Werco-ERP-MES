import axios, { AxiosInstance, AxiosError } from 'axios';

const API_BASE_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000/api/v1';

class ApiService {
  private api: AxiosInstance;
  private token: string | null = null;

  constructor() {
    this.api = axios.create({
      baseURL: API_BASE_URL,
      headers: {
        'Content-Type': 'application/json',
      },
    });

    // Load token from localStorage
    this.token = localStorage.getItem('token');
    if (this.token) {
      this.api.defaults.headers.common['Authorization'] = `Bearer ${this.token}`;
    }

    // Response interceptor for error handling
    this.api.interceptors.response.use(
      (response) => response,
      (error: AxiosError) => {
        if (error.response?.status === 401) {
          this.logout();
          window.location.href = '/login';
        }
        return Promise.reject(error);
      }
    );
  }

  setToken(token: string) {
    this.token = token;
    localStorage.setItem('token', token);
    this.api.defaults.headers.common['Authorization'] = `Bearer ${token}`;
  }

  logout() {
    this.token = null;
    localStorage.removeItem('token');
    delete this.api.defaults.headers.common['Authorization'];
  }

  // Auth
  async login(email: string, password: string) {
    const formData = new URLSearchParams();
    formData.append('username', email);
    formData.append('password', password);
    
    const response = await this.api.post('/auth/login', formData, {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    });
    return response.data;
  }

  async register(userData: any) {
    const response = await this.api.post('/auth/register', userData);
    return response.data;
  }

  // Work Centers
  async getWorkCenters(activeOnly = true) {
    const response = await this.api.get('/work-centers/', { params: { active_only: activeOnly } });
    return response.data;
  }

  async createWorkCenter(data: any) {
    const response = await this.api.post('/work-centers/', data);
    return response.data;
  }

  async updateWorkCenter(id: number, data: any) {
    const response = await this.api.put(`/work-centers/${id}`, data);
    return response.data;
  }

  async updateWorkCenterStatus(id: number, status: string) {
    const response = await this.api.post(`/work-centers/${id}/status`, null, { params: { status } });
    return response.data;
  }

  // Parts
  async getParts(params?: { search?: string; part_type?: string; active_only?: boolean }) {
    const response = await this.api.get('/parts/', { params });
    return response.data;
  }

  async getPart(id: number) {
    const response = await this.api.get(`/parts/${id}`);
    return response.data;
  }

  async createPart(data: any) {
    const response = await this.api.post('/parts/', data);
    return response.data;
  }

  async updatePart(id: number, data: any) {
    const response = await this.api.put(`/parts/${id}`, data);
    return response.data;
  }

  // BOM (Bill of Materials)
  async getBOMs(params?: { status?: string; active_only?: boolean }) {
    const response = await this.api.get('/bom/', { params });
    return response.data;
  }

  async getBOM(id: number) {
    const response = await this.api.get(`/bom/${id}`);
    return response.data;
  }

  async getBOMByPart(partId: number) {
    const response = await this.api.get(`/bom/by-part/${partId}`);
    return response.data;
  }

  async createBOM(data: any) {
    const response = await this.api.post('/bom/', data);
    return response.data;
  }

  async updateBOM(id: number, data: any) {
    const response = await this.api.put(`/bom/${id}`, data);
    return response.data;
  }

  async releaseBOM(id: number) {
    const response = await this.api.post(`/bom/${id}/release`);
    return response.data;
  }

  async addBOMItem(bomId: number, data: any) {
    const response = await this.api.post(`/bom/${bomId}/items`, data);
    return response.data;
  }

  async updateBOMItem(itemId: number, data: any) {
    const response = await this.api.put(`/bom/items/${itemId}`, data);
    return response.data;
  }

  async deleteBOMItem(itemId: number) {
    const response = await this.api.delete(`/bom/items/${itemId}`);
    return response.data;
  }

  async explodeBOM(id: number, maxLevels = 10) {
    const response = await this.api.get(`/bom/${id}/explode`, { params: { max_levels: maxLevels } });
    return response.data;
  }

  async flattenBOM(id: number, maxLevels = 10) {
    const response = await this.api.get(`/bom/${id}/flatten`, { params: { max_levels: maxLevels } });
    return response.data;
  }

  async whereUsed(bomId: number) {
    const response = await this.api.get(`/bom/${bomId}/where-used`);
    return response.data;
  }

  // Work Orders
  async getWorkOrders(params?: { status?: string; search?: string }) {
    const response = await this.api.get('/work-orders/', { params });
    return response.data;
  }

  async getWorkOrder(id: number) {
    const response = await this.api.get(`/work-orders/${id}`);
    return response.data;
  }

  async createWorkOrder(data: any) {
    const response = await this.api.post('/work-orders/', data);
    return response.data;
  }

  async updateWorkOrder(id: number, data: any) {
    const response = await this.api.put(`/work-orders/${id}`, data);
    return response.data;
  }

  async releaseWorkOrder(id: number) {
    const response = await this.api.post(`/work-orders/${id}/release`);
    return response.data;
  }

  async startWorkOrder(id: number) {
    const response = await this.api.post(`/work-orders/${id}/start`);
    return response.data;
  }

  async completeWorkOrder(id: number, quantityComplete: number, quantityScrapped = 0) {
    const response = await this.api.post(`/work-orders/${id}/complete`, null, {
      params: { quantity_complete: quantityComplete, quantity_scrapped: quantityScrapped }
    });
    return response.data;
  }

  // Operations
  async addOperation(workOrderId: number, data: any) {
    const response = await this.api.post(`/work-orders/${workOrderId}/operations`, data);
    return response.data;
  }

  async updateOperation(operationId: number, data: any) {
    const response = await this.api.put(`/work-orders/operations/${operationId}`, data);
    return response.data;
  }

  async startOperation(operationId: number) {
    const response = await this.api.post(`/work-orders/operations/${operationId}/start`);
    return response.data;
  }

  async completeOperation(operationId: number, quantityComplete: number, quantityScrapped = 0) {
    const response = await this.api.post(`/work-orders/operations/${operationId}/complete`, null, {
      params: { quantity_complete: quantityComplete, quantity_scrapped: quantityScrapped }
    });
    return response.data;
  }

  // Shop Floor
  async getMyActiveJob() {
    const response = await this.api.get('/shop-floor/my-active-job');
    return response.data;
  }

  async clockIn(data: { work_order_id: number; operation_id: number; work_center_id: number; entry_type?: string; notes?: string }) {
    const response = await this.api.post('/shop-floor/clock-in', data);
    return response.data;
  }

  async clockOut(timeEntryId: number, data: { quantity_produced: number; quantity_scrapped?: number; scrap_reason?: string; notes?: string }) {
    const response = await this.api.post(`/shop-floor/clock-out/${timeEntryId}`, data);
    return response.data;
  }

  async getWorkCenterQueue(workCenterId: number) {
    const response = await this.api.get(`/shop-floor/work-center-queue/${workCenterId}`);
    return response.data;
  }

  async getDashboard() {
    const response = await this.api.get('/shop-floor/dashboard');
    return response.data;
  }

  async getActiveUsers() {
    const response = await this.api.get('/shop-floor/active-users');
    return response.data;
  }

  // MRP (Material Requirements Planning)
  async getMRPRuns(params?: { skip?: number; limit?: number }) {
    const response = await this.api.get('/mrp/runs', { params });
    return response.data;
  }

  async runMRP(data: { planning_horizon_days?: number; include_safety_stock?: boolean; include_allocated?: boolean }) {
    const response = await this.api.post('/mrp/runs', data);
    return response.data;
  }

  async getMRPRun(id: number) {
    const response = await this.api.get(`/mrp/runs/${id}`);
    return response.data;
  }

  async getMRPActions(runId: number, params?: { action_type?: string; unprocessed_only?: boolean }) {
    const response = await this.api.get(`/mrp/runs/${runId}/actions`, { params });
    return response.data;
  }

  async getMRPShortages() {
    const response = await this.api.get('/mrp/shortages');
    return response.data;
  }

  async processMRPAction(actionId: number, notes?: string) {
    const response = await this.api.post(`/mrp/actions/${actionId}/process`, null, { params: { notes } });
    return response.data;
  }

  // Custom Fields
  async getCustomFieldDefinitions(entityType?: string) {
    const params: any = { active_only: true };
    if (entityType) params.entity_type = entityType;
    const response = await this.api.get('/custom-fields/definitions', { params });
    return response.data;
  }

  async createCustomFieldDefinition(data: any) {
    const response = await this.api.post('/custom-fields/definitions', data);
    return response.data;
  }

  async updateCustomFieldDefinition(id: number, data: any) {
    const response = await this.api.put(`/custom-fields/definitions/${id}`, data);
    return response.data;
  }

  async deleteCustomFieldDefinition(id: number) {
    const response = await this.api.delete(`/custom-fields/definitions/${id}`);
    return response.data;
  }

  async getEntityCustomFields(entityType: string, entityId: number) {
    const response = await this.api.get(`/custom-fields/values/${entityType}/${entityId}`);
    return response.data;
  }

  async setCustomFieldValue(entityType: string, entityId: number, fieldKey: string, value: any) {
    const response = await this.api.post(`/custom-fields/values/${entityType}/${entityId}`, {
      field_key: fieldKey,
      value
    });
    return response.data;
  }

  async setBulkCustomFields(entityType: string, entityId: number, values: Record<string, any>) {
    const response = await this.api.post('/custom-fields/values/bulk', {
      entity_type: entityType,
      entity_id: entityId,
      values
    });
    return response.data;
  }

  // Routing
  async getRoutings(params?: { part_id?: number; status?: string; active_only?: boolean }) {
    const response = await this.api.get('/routing/', { params });
    return response.data;
  }

  async getRouting(id: number) {
    const response = await this.api.get(`/routing/${id}`);
    return response.data;
  }

  async getRoutingByPart(partId: number) {
    const response = await this.api.get(`/routing/by-part/${partId}`);
    return response.data;
  }

  async createRouting(data: { part_id: number; revision?: string; description?: string }) {
    const response = await this.api.post('/routing/', data);
    return response.data;
  }

  async updateRouting(id: number, data: any) {
    const response = await this.api.put(`/routing/${id}`, data);
    return response.data;
  }

  async releaseRouting(id: number) {
    const response = await this.api.post(`/routing/${id}/release`);
    return response.data;
  }

  async addRoutingOperation(routingId: number, data: any) {
    const response = await this.api.post(`/routing/${routingId}/operations`, data);
    return response.data;
  }

  async updateRoutingOperation(routingId: number, operationId: number, data: any) {
    const response = await this.api.put(`/routing/${routingId}/operations/${operationId}`, data);
    return response.data;
  }

  async deleteRoutingOperation(routingId: number, operationId: number) {
    const response = await this.api.delete(`/routing/${routingId}/operations/${operationId}`);
    return response.data;
  }

  // Quality Management
  async getNCRs(params?: { status?: string; part_id?: number }) {
    const response = await this.api.get('/quality/ncr', { params });
    return response.data;
  }

  async createNCR(data: any) {
    const response = await this.api.post('/quality/ncr', data);
    return response.data;
  }

  async updateNCR(id: number, data: any) {
    const response = await this.api.put(`/quality/ncr/${id}`, data);
    return response.data;
  }

  async getCARs(params?: { status?: string }) {
    const response = await this.api.get('/quality/car', { params });
    return response.data;
  }

  async createCAR(data: any) {
    const response = await this.api.post('/quality/car', data);
    return response.data;
  }

  async updateCAR(id: number, data: any) {
    const response = await this.api.put(`/quality/car/${id}`, data);
    return response.data;
  }

  async getFAIs(params?: { status?: string; part_id?: number }) {
    const response = await this.api.get('/quality/fai', { params });
    return response.data;
  }

  async createFAI(data: any) {
    const response = await this.api.post('/quality/fai', data);
    return response.data;
  }

  async updateFAI(id: number, data: any) {
    const response = await this.api.put(`/quality/fai/${id}`, data);
    return response.data;
  }

  async getQualitySummary() {
    const response = await this.api.get('/quality/summary');
    return response.data;
  }

  // Inventory
  async getInventory(params?: any) {
    const response = await this.api.get('/inventory/', { params });
    return response.data;
  }

  async getInventorySummary() {
    const response = await this.api.get('/inventory/summary');
    return response.data;
  }

  async getInventoryLocations(params?: any) {
    const response = await this.api.get('/inventory/locations', { params });
    return response.data;
  }

  async getLowStockAlerts() {
    const response = await this.api.get('/inventory/low-stock');
    return response.data;
  }

  async createInventoryLocation(data: any) {
    const response = await this.api.post('/inventory/locations', data);
    return response.data;
  }

  async receiveInventory(data: any) {
    const response = await this.api.post('/inventory/receive', data);
    return response.data;
  }

  async issueInventory(data: any) {
    const response = await this.api.post('/inventory/issue', data);
    return response.data;
  }

  async transferInventory(data: any) {
    const response = await this.api.post('/inventory/transfer', data);
    return response.data;
  }

  async adjustInventory(data: any) {
    const response = await this.api.post('/inventory/adjust', data);
    return response.data;
  }

  async getCycleCounts(params?: any) {
    const response = await this.api.get('/inventory/cycle-counts', { params });
    return response.data;
  }

  async createCycleCount(data: any) {
    const response = await this.api.post('/inventory/cycle-counts', data);
    return response.data;
  }

  // Purchasing
  async getVendors(params?: { active_only?: boolean; approved_only?: boolean }) {
    const response = await this.api.get('/purchasing/vendors', { params });
    return response.data;
  }

  async createVendor(data: any) {
    const response = await this.api.post('/purchasing/vendors', data);
    return response.data;
  }

  async updateVendor(id: number, data: any) {
    const response = await this.api.put(`/purchasing/vendors/${id}`, data);
    return response.data;
  }

  async getPurchaseOrders(params?: { status?: string; vendor_id?: number }) {
    const response = await this.api.get('/purchasing/purchase-orders', { params });
    return response.data;
  }

  async getPurchaseOrder(id: number) {
    const response = await this.api.get(`/purchasing/purchase-orders/${id}`);
    return response.data;
  }

  async createPurchaseOrder(data: any) {
    const response = await this.api.post('/purchasing/purchase-orders', data);
    return response.data;
  }

  async updatePurchaseOrder(id: number, data: any) {
    const response = await this.api.put(`/purchasing/purchase-orders/${id}`, data);
    return response.data;
  }

  async sendPurchaseOrder(id: number) {
    const response = await this.api.post(`/purchasing/purchase-orders/${id}/send`);
    return response.data;
  }

  async addPOLine(poId: number, data: any) {
    const response = await this.api.post(`/purchasing/purchase-orders/${poId}/lines`, data);
    return response.data;
  }

  async getReceivingQueue() {
    const response = await this.api.get('/purchasing/receiving/queue');
    return response.data;
  }

  async receiveMaterial(data: any) {
    const response = await this.api.post('/purchasing/receiving', data);
    return response.data;
  }

  async getPendingInspection() {
    const response = await this.api.get('/purchasing/receiving/pending-inspection');
    return response.data;
  }

  async inspectReceipt(receiptId: number, data: any) {
    const response = await this.api.post(`/purchasing/receiving/${receiptId}/inspect`, data);
    return response.data;
  }

  async getReceivingHistory(days = 30) {
    const response = await this.api.get('/purchasing/receiving/history', { params: { days } });
    return response.data;
  }

  // Scheduling
  async getScheduledJobs(params?: { start_date?: string; end_date?: string; work_center_id?: number }) {
    const response = await this.api.get('/scheduling/jobs', { params });
    return response.data;
  }

  async scheduleOperation(operationId: number, data: { scheduled_start: string; scheduled_end?: string | null }) {
    const response = await this.api.put(`/scheduling/operations/${operationId}/schedule`, data);
    return response.data;
  }

  async getCapacitySummary(startDate: string, endDate: string) {
    const response = await this.api.get('/scheduling/capacity', { params: { start_date: startDate, end_date: endDate } });
    return response.data;
  }

  async autoScheduleOperations(workCenterId?: number) {
    const response = await this.api.post('/scheduling/auto-schedule', null, { params: { work_center_id: workCenterId } });
    return response.data;
  }

  // Documents
  async getDocuments(params?: { part_id?: number; work_order_id?: number; document_type?: string; search?: string }) {
    const response = await this.api.get('/documents/', { params });
    return response.data;
  }

  async getDocumentTypes() {
    const response = await this.api.get('/documents/types/list');
    return response.data;
  }

  async uploadDocument(formData: FormData) {
    const response = await this.api.post('/documents/upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' }
    });
    return response.data;
  }

  async downloadDocument(documentId: number) {
    const response = await this.api.get(`/documents/${documentId}/download`, { responseType: 'blob' });
    return response.data;
  }

  async deleteDocument(documentId: number) {
    const response = await this.api.delete(`/documents/${documentId}`);
    return response.data;
  }

  // Reports
  async getProductionSummary(days = 30) {
    const response = await this.api.get('/reports/production-summary', { params: { days } });
    return response.data;
  }

  async getQualityMetrics(days = 30) {
    const response = await this.api.get('/reports/quality-metrics', { params: { days } });
    return response.data;
  }

  async getInventoryValue() {
    const response = await this.api.get('/reports/inventory-value');
    return response.data;
  }

  async getVendorPerformance(days = 90) {
    const response = await this.api.get('/reports/vendor-performance', { params: { days } });
    return response.data;
  }

  async getWorkCenterUtilization(days = 30) {
    const response = await this.api.get('/reports/work-center-utilization', { params: { days } });
    return response.data;
  }

  async getDailyOutput(days = 14) {
    const response = await this.api.get('/reports/daily-output', { params: { days } });
    return response.data;
  }

  // Shipping
  async getShipments(params?: { status?: string }) {
    const response = await this.api.get('/shipping/', { params });
    return response.data;
  }

  async getShipment(shipmentId: number) {
    const response = await this.api.get(`/shipping/${shipmentId}`);
    return response.data;
  }

  async getReadyToShip() {
    const response = await this.api.get('/shipping/ready-to-ship');
    return response.data;
  }

  async createShipment(data: any) {
    const response = await this.api.post('/shipping/', data);
    return response.data;
  }

  async markShipped(shipmentId: number, trackingNumber?: string) {
    const response = await this.api.post(`/shipping/${shipmentId}/ship`, null, {
      params: { tracking_number: trackingNumber }
    });
    return response.data;
  }

  // Quotes
  async getQuotes(params?: { status?: string; customer?: string }) {
    const response = await this.api.get('/quotes/', { params });
    return response.data;
  }

  async getQuote(id: number) {
    const response = await this.api.get(`/quotes/${id}`);
    return response.data;
  }

  async createQuote(data: any) {
    const response = await this.api.post('/quotes/', data);
    return response.data;
  }

  async sendQuote(quoteId: number) {
    const response = await this.api.post(`/quotes/${quoteId}/send`);
    return response.data;
  }

  async convertQuote(quoteId: number) {
    const response = await this.api.post(`/quotes/${quoteId}/convert`);
    return response.data;
  }

  // Users
  async getUsers(includeInactive = false) {
    const response = await this.api.get('/users/', { params: { include_inactive: includeInactive } });
    return response.data;
  }

  async createUser(data: any) {
    const response = await this.api.post('/users/', data);
    return response.data;
  }

  async updateUser(userId: number, data: any) {
    const response = await this.api.put(`/users/${userId}`, data);
    return response.data;
  }

  async resetUserPassword(userId: number, newPassword: string) {
    const response = await this.api.post(`/users/${userId}/reset-password`, { new_password: newPassword });
    return response.data;
  }

  async deactivateUser(userId: number) {
    const response = await this.api.delete(`/users/${userId}`);
    return response.data;
  }

  async activateUser(userId: number) {
    const response = await this.api.post(`/users/${userId}/activate`);
    return response.data;
  }

  // Customers
  async getCustomers(activeOnly = true, search?: string) {
    const response = await this.api.get('/customers/', { params: { active_only: activeOnly, search } });
    return response.data;
  }

  async getCustomerNames() {
    const response = await this.api.get('/customers/names');
    return response.data;
  }

  async createCustomer(data: any) {
    const response = await this.api.post('/customers/', data);
    return response.data;
  }

  async updateCustomer(customerId: number, data: any) {
    const response = await this.api.put(`/customers/${customerId}`, data);
    return response.data;
  }

  // Calibration
  async getEquipment(status?: string) {
    const response = await this.api.get('/calibration/equipment', { params: { status } });
    return response.data;
  }

  async createEquipment(data: any) {
    const response = await this.api.post('/calibration/equipment', data);
    return response.data;
  }

  async updateEquipment(equipmentId: number, data: any) {
    const response = await this.api.put(`/calibration/equipment/${equipmentId}`, data);
    return response.data;
  }

  async recordCalibration(equipmentId: number, data: any) {
    const response = await this.api.post(`/calibration/equipment/${equipmentId}/calibrate`, data);
    return response.data;
  }

  async getEquipmentDueSoon(days = 30) {
    const response = await this.api.get('/calibration/equipment/due-soon', { params: { days } });
    return response.data;
  }

  // Scanner / Supplier Mappings
  async scannerLookup(code: string) {
    const response = await this.api.post('/scanner/lookup', null, { params: { code } });
    return response.data;
  }

  async getSupplierMappings(search?: string, partId?: number, vendorId?: number) {
    const response = await this.api.get('/scanner/mappings', { 
      params: { search, part_id: partId, vendor_id: vendorId } 
    });
    return response.data;
  }

  async createSupplierMapping(data: any) {
    const response = await this.api.post('/scanner/mappings', data);
    return response.data;
  }

  async deleteSupplierMapping(mappingId: number) {
    const response = await this.api.delete(`/scanner/mappings/${mappingId}`);
    return response.data;
  }

  // Traceability
  async traceLot(lotNumber: string) {
    const response = await this.api.get(`/traceability/lot/${encodeURIComponent(lotNumber)}`);
    return response.data;
  }

  async traceSerial(serialNumber: string) {
    const response = await this.api.get(`/traceability/serial/${encodeURIComponent(serialNumber)}`);
    return response.data;
  }

  async searchLots(query: string) {
    const response = await this.api.get('/traceability/search', { params: { q: query } });
    return response.data;
  }

  // Costing & Time Reports
  async getWorkOrderCosting(workOrderId?: number, days = 90) {
    const response = await this.api.get('/reports/work-order-costing', { 
      params: { work_order_id: workOrderId, days } 
    });
    return response.data;
  }

  async getEmployeeTimeReport(startDate?: string, endDate?: string, userId?: number) {
    const response = await this.api.get('/reports/employee-time', { 
      params: { start_date: startDate, end_date: endDate, user_id: userId } 
    });
    return response.data;
  }

  // Audit Logs
  async getAuditLogs(params?: { action?: string; resource_type?: string; user_id?: number; search?: string; limit?: number }) {
    const response = await this.api.get('/audit/', { params });
    return response.data;
  }

  async getAuditSummary(days = 30) {
    const response = await this.api.get('/audit/summary', { params: { days } });
    return response.data;
  }

  async getAuditActions() {
    const response = await this.api.get('/audit/actions');
    return response.data;
  }

  async getAuditResourceTypes() {
    const response = await this.api.get('/audit/resource-types');
    return response.data;
  }

  // Quote Calculator
  async getQuoteMaterials(category?: string) {
    const response = await this.api.get('/quote-calc/materials', { params: { category } });
    return response.data;
  }

  async getQuoteMachines(machineType?: string) {
    const response = await this.api.get('/quote-calc/machines', { params: { machine_type: machineType } });
    return response.data;
  }

  async getQuoteFinishes() {
    const response = await this.api.get('/quote-calc/finishes');
    return response.data;
  }

  async getQuoteSettings() {
    const response = await this.api.get('/quote-calc/settings');
    return response.data;
  }

  async seedQuoteDefaults() {
    const response = await this.api.post('/quote-calc/seed-defaults');
    return response.data;
  }

  async calculateCNCQuote(data: any) {
    const response = await this.api.post('/quote-calc/cnc', data);
    return response.data;
  }

  async calculateSheetMetalQuote(data: any) {
    const response = await this.api.post('/quote-calc/sheet-metal', data);
    return response.data;
  }

  async createQuoteMaterial(data: any) {
    const response = await this.api.post('/quote-calc/materials', null, { params: data });
    return response.data;
  }

  async createQuoteMachine(data: any) {
    const response = await this.api.post('/quote-calc/machines', null, { params: data });
    return response.data;
  }

  async createQuoteFinish(data: any) {
    const response = await this.api.post('/quote-calc/finishes', null, { params: data });
    return response.data;
  }

  async updateQuoteSetting(key: string, value: string, settingType: string = 'text') {
    const response = await this.api.post(`/quote-calc/settings/${key}`, null, { 
      params: { value, setting_type: settingType } 
    });
    return response.data;
  }

  // DXF Parser
  async analyzeDXF(file: File, maxHoleDiameter: number = 2.0, units: string = 'inches') {
    const formData = new FormData();
    formData.append('file', file);
    const response = await this.api.post('/dxf-parser/analyze', formData, {
      params: { max_hole_diameter: maxHoleDiameter, units },
      headers: { 'Content-Type': 'multipart/form-data' }
    });
    return response.data;
  }
}

export const api = new ApiService();
export default api;
