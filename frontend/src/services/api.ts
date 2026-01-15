import axios, { AxiosInstance, AxiosError, AxiosRequestConfig } from 'axios';
import {
  LoginResponse,
  RefreshTokenResponse,
  UserCreate,
  UserUpdate,
  PartCreate,
  PartUpdate,
  PartListParams,
  WorkOrderCreate,
  WorkOrderUpdate,
  WorkOrderListParams,
  WorkCenterCreate,
  WorkCenterUpdate,
  BOMCreate,
  BOMUpdate,
  BOMResponse,
  InventoryTransaction,
  PurchaseOrderCreate,
  QuoteCreate,
  RoutingCreate,
  CustomerCreate,
  VendorCreate,
  ReportParams,
  GlobalSearchParams,
  getErrorMessage,
} from '../types/api';
import { User, Part, WorkOrder, WorkCenter } from '../types';

const API_BASE_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000/api/v1';

// ETag cache for conditional requests
interface CacheEntry {
  etag: string;
  data: unknown;
  timestamp: number;
}

// Global cache for ETag-based conditional requests
const etagCache = new Map<string, CacheEntry>();

// Cache TTL in milliseconds (5 minutes)
const CACHE_TTL = 5 * 60 * 1000;

class ApiService {
  private api: AxiosInstance;
  private token: string | null = null;
  private refreshToken: string | null = null;
  private isRefreshing = false;
  private refreshSubscribers: ((token: string) => void)[] = [];
  private tokenExpiresAt: number | null = null;

  constructor() {
    this.api = axios.create({
      baseURL: API_BASE_URL,
      headers: {
        'Content-Type': 'application/json',
        // CSRF defense: Custom header that cannot be set by cross-origin requests
        'X-Requested-With': 'XMLHttpRequest',
      },
    });

    // Load tokens from localStorage
    this.token = localStorage.getItem('token');
    this.refreshToken = localStorage.getItem('refreshToken');
    const expiresAt = localStorage.getItem('tokenExpiresAt');
    this.tokenExpiresAt = expiresAt ? parseInt(expiresAt, 10) : null;
    
    if (this.token) {
      this.api.defaults.headers.common['Authorization'] = `Bearer ${this.token}`;
    }

    // Request interceptor - check token expiration before requests
    this.api.interceptors.request.use(
      async (config) => {
        // Skip token refresh for auth endpoints
        if (config.url?.includes('/auth/login') || config.url?.includes('/auth/refresh')) {
          return config;
        }
        
        // Check if token is about to expire (within 60 seconds)
        if (this.tokenExpiresAt && Date.now() >= this.tokenExpiresAt - 60000) {
          if (this.refreshToken) {
            try {
              await this.refreshAccessToken();
              config.headers['Authorization'] = `Bearer ${this.token}`;
            } catch (error) {
              // Refresh failed, will get 401 and redirect to login
            }
          }
        }
        return config;
      },
      (error) => Promise.reject(error)
    );

    // Response interceptor for error handling and token refresh
    this.api.interceptors.response.use(
      (response) => response,
      async (error: AxiosError) => {
        const originalRequest = error.config as AxiosRequestConfig & { _retry?: boolean };
        
        // If 401 and we haven't already retried and have a refresh token
        if (error.response?.status === 401 && !originalRequest._retry && this.refreshToken) {
          if (this.isRefreshing) {
            // Wait for the ongoing refresh to complete
            return new Promise((resolve) => {
              this.refreshSubscribers.push((token: string) => {
                originalRequest.headers = originalRequest.headers || {};
                originalRequest.headers['Authorization'] = `Bearer ${token}`;
                resolve(this.api(originalRequest));
              });
            });
          }

          originalRequest._retry = true;
          
          try {
            await this.refreshAccessToken();
            originalRequest.headers = originalRequest.headers || {};
            originalRequest.headers['Authorization'] = `Bearer ${this.token}`;
            return this.api(originalRequest);
          } catch (refreshError) {
            // Refresh failed, logout and redirect
            this.logout();
            window.location.href = '/login';
            return Promise.reject(refreshError);
          }
        }
        
        if (error.response?.status === 401) {
          this.logout();
          window.location.href = '/login';
        }
        return Promise.reject(error);
      }
    );
  }

  private async refreshAccessToken(): Promise<void> {
    if (!this.refreshToken) {
      throw new Error('No refresh token available');
    }

    this.isRefreshing = true;
    
    try {
      const response = await axios.post(`${API_BASE_URL}/auth/refresh`, {
        refresh_token: this.refreshToken
      }, {
        headers: {
          'Content-Type': 'application/json',
          'X-Requested-With': 'XMLHttpRequest',
        }
      });
      
      const { access_token, refresh_token, expires_in } = response.data;
      
      this.setTokens(access_token, refresh_token, expires_in);
      
      // Notify all waiting requests
      this.refreshSubscribers.forEach(callback => callback(access_token));
      this.refreshSubscribers = [];
    } finally {
      this.isRefreshing = false;
    }
  }

  /**
   * Fetch with ETag-based conditional request support.
   * Returns cached data on 304 Not Modified, reducing bandwidth.
   * 
   * @param url - API endpoint path
   * @param config - Optional axios config
   * @returns Promise with response data and metadata
   */
  private async fetchWithCache(
    url: string, 
    config?: AxiosRequestConfig
  ): Promise<{ data: any; fromCache: boolean; changed: boolean }> {
    const cacheKey = url + (config?.params ? JSON.stringify(config.params) : '');
    const cached = etagCache.get(cacheKey);
    
    // Build headers with If-None-Match if we have a cached ETag
    const headers: Record<string, string> = {};
    if (cached?.etag) {
      headers['If-None-Match'] = cached.etag;
    }
    
    try {
      const response = await this.api.get(url, {
        ...config,
        headers: { ...config?.headers, ...headers },
        validateStatus: (status) => status === 200 || status === 304,
      });
      
      // 304 Not Modified - return cached data
      if (response.status === 304 && cached) {
        return { data: cached.data, fromCache: true, changed: false };
      }
      
      // 200 OK - update cache with new ETag
      const etag = response.headers['etag'];
      if (etag) {
        etagCache.set(cacheKey, {
          etag: etag.replace(/"/g, ''),
          data: response.data,
          timestamp: Date.now(),
        });
      }
      
      // Check if data actually changed (for UI optimization)
      const changed = !cached || JSON.stringify(cached.data) !== JSON.stringify(response.data);
      
      return { data: response.data, fromCache: false, changed };
    } catch (error) {
      // On error, return stale cache if available and not too old
      if (cached && (Date.now() - cached.timestamp) < CACHE_TTL) {
        console.warn('API error, returning stale cache:', error);
        return { data: cached.data, fromCache: true, changed: false };
      }
      throw error;
    }
  }

  /**
   * Clear the ETag cache (useful on logout or data mutations)
   */
  clearCache() {
    etagCache.clear();
  }

  /**
   * Clear specific cache entry
   */
  invalidateCache(urlPattern: string) {
    const keysToDelete: string[] = [];
    etagCache.forEach((_, key) => {
      if (key.includes(urlPattern)) {
        keysToDelete.push(key);
      }
    });
    keysToDelete.forEach(key => etagCache.delete(key));
  }

  setToken(token: string) {
    this.token = token;
    localStorage.setItem('token', token);
    this.api.defaults.headers.common['Authorization'] = `Bearer ${token}`;
  }

  setTokens(accessToken: string, refreshToken: string, expiresIn: number) {
    this.token = accessToken;
    this.refreshToken = refreshToken;
    this.tokenExpiresAt = Date.now() + (expiresIn * 1000);
    
    localStorage.setItem('token', accessToken);
    localStorage.setItem('refreshToken', refreshToken);
    localStorage.setItem('tokenExpiresAt', this.tokenExpiresAt.toString());
    
    this.api.defaults.headers.common['Authorization'] = `Bearer ${accessToken}`;
  }

  logout() {
    this.token = null;
    this.refreshToken = null;
    this.tokenExpiresAt = null;
    
    localStorage.removeItem('token');
    localStorage.removeItem('refreshToken');
    localStorage.removeItem('tokenExpiresAt');
    
    delete this.api.defaults.headers.common['Authorization'];
    this.clearCache();
  }

  // Auth
  async login(email: string, password: string): Promise<LoginResponse> {
    const formData = new URLSearchParams();
    formData.append('username', email);
    formData.append('password', password);
    
    const response = await this.api.post<LoginResponse>('/auth/login', formData, {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    });
    return response.data;
  }

  async register(userData: UserCreate): Promise<User> {
    const response = await this.api.post<User>('/auth/register', userData);
    return response.data;
  }

  // Work Centers
  async getWorkCenters(activeOnly = true): Promise<WorkCenter[]> {
    const response = await this.api.get<WorkCenter[]>('/work-centers/', { params: { active_only: activeOnly } });
    return response.data;
  }

  async createWorkCenter(data: WorkCenterCreate): Promise<WorkCenter> {
    const response = await this.api.post<WorkCenter>('/work-centers/', data);
    return response.data;
  }

  async updateWorkCenter(id: number, data: WorkCenterUpdate): Promise<WorkCenter> {
    const response = await this.api.put<WorkCenter>(`/work-centers/${id}`, data);
    return response.data;
  }

  async updateWorkCenterStatus(id: number, status: string): Promise<WorkCenter> {
    const response = await this.api.post<WorkCenter>(`/work-centers/${id}/status`, null, { params: { status } });
    return response.data;
  }

  // Parts
  async getParts(params?: PartListParams): Promise<Part[]> {
    const response = await this.api.get<Part[]>('/parts/', { params: { limit: 500, ...params } });
    return response.data;
  }

  async getPart(id: number): Promise<Part> {
    const response = await this.api.get<Part>(`/parts/${id}`);
    return response.data;
  }

  async createPart(data: PartCreate): Promise<Part> {
    const response = await this.api.post<Part>('/parts/', data);
    return response.data;
  }

  async updatePart(id: number, data: PartUpdate): Promise<Part> {
    const response = await this.api.put<Part>(`/parts/${id}`, data);
    return response.data;
  }

  async deletePart(id: number) {
    const response = await this.api.delete(`/parts/${id}`);
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

  async deleteBOM(id: number) {
    const response = await this.api.delete(`/bom/${id}`);
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

  async deleteWorkOrder(id: number) {
    const response = await this.api.delete(`/work-orders/${id}`);
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

  async getMaterialRequirements(workOrderId: number) {
    const response = await this.api.get(`/work-orders/${workOrderId}/material-requirements`);
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

  async startWOOperation(operationId: number) {
    const response = await this.api.post(`/work-orders/operations/${operationId}/start`);
    return response.data;
  }

  async completeWOOperation(operationId: number, quantityComplete: number, quantityScrapped = 0) {
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

  /**
   * Get dashboard data with ETag-based caching.
   * Returns cached data on 304 Not Modified, reducing bandwidth by ~75%.
   */
  async getDashboardWithCache(): Promise<{ data: any; fromCache: boolean; changed: boolean }> {
    return this.fetchWithCache('/shop-floor/dashboard');
  }

  async getActiveUsers() {
    const response = await this.api.get('/shop-floor/active-users');
    return response.data;
  }

  // Simplified Shop Floor Operations
  async getShopFloorOperations(params?: { work_center_id?: number; status?: string; search?: string }) {
    const response = await this.api.get('/shop-floor/operations', { params });
    return response.data;
  }

  async startOperation(operationId: number) {
    const response = await this.api.put(`/shop-floor/operations/${operationId}/start`);
    return response.data;
  }

  async completeOperation(operationId: number, data: { quantity_complete: number; notes?: string }) {
    const response = await this.api.post(`/shop-floor/operations/${operationId}/complete`, data);
    return response.data;
  }

  async getOperationDetails(operationId: number) {
    const response = await this.api.get(`/shop-floor/operations/${operationId}`);
    return response.data;
  }

  async holdOperation(operationId: number) {
    const response = await this.api.put(`/shop-floor/operations/${operationId}/hold`);
    return response.data;
  }

  async resumeOperation(operationId: number) {
    const response = await this.api.put(`/shop-floor/operations/${operationId}/resume`);
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

  async previewWorkOrderOperations(partId: number, quantity: number = 1) {
    const response = await this.api.get(`/work-orders/preview-operations/${partId}`, {
      params: { quantity }
    });
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

  async deleteRouting(id: number) {
    const response = await this.api.delete(`/routing/${id}`);
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

  // Scheduling
  async getScheduledJobs(params?: { start_date?: string; end_date?: string; work_center_id?: number }) {
    const response = await this.api.get('/scheduling/jobs', { params });
    return response.data;
  }

  async getSchedulableWorkOrders(params?: { start_date?: string; end_date?: string; work_center_id?: number }) {
    const response = await this.api.get('/scheduling/work-orders', { params });
    return response.data;
  }

  async scheduleWorkOrder(workOrderId: number, data: { scheduled_start: string; work_center_id?: number }) {
    const response = await this.api.put(`/scheduling/work-orders/${workOrderId}/schedule`, data);
    return response.data;
  }

  async scheduleOperation(operationId: number, data: { scheduled_start: string; scheduled_end?: string | null }) {
    const response = await this.api.put(`/scheduling/operations/${operationId}/schedule`, data);
    return response.data;
  }

  async updateOperationWorkCenter(operationId: number, workCenterId: number) {
    const response = await this.api.put(`/scheduling/operations/${operationId}/work-center`, { work_center_id: workCenterId });
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

  async getCustomerStats(customerId: number) {
    const response = await this.api.get(`/customers/${customerId}/stats`);
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

  // Admin Settings - Materials
  async getAdminMaterials(includeInactive = false, category?: string) {
    const response = await this.api.get('/admin/settings/materials', { 
      params: { include_inactive: includeInactive, category } 
    });
    return response.data;
  }

  async createAdminMaterial(data: any) {
    const response = await this.api.post('/admin/settings/materials', data);
    return response.data;
  }

  async updateAdminMaterial(id: number, data: any) {
    const response = await this.api.put(`/admin/settings/materials/${id}`, data);
    return response.data;
  }

  async deleteAdminMaterial(id: number) {
    const response = await this.api.delete(`/admin/settings/materials/${id}`);
    return response.data;
  }

  // Admin Settings - Machines
  async getAdminMachines(includeInactive = false, machineType?: string) {
    const response = await this.api.get('/admin/settings/machines', { 
      params: { include_inactive: includeInactive, machine_type: machineType } 
    });
    return response.data;
  }

  async createAdminMachine(data: any) {
    const response = await this.api.post('/admin/settings/machines', data);
    return response.data;
  }

  async updateAdminMachine(id: number, data: any) {
    const response = await this.api.put(`/admin/settings/machines/${id}`, data);
    return response.data;
  }

  async deleteAdminMachine(id: number) {
    const response = await this.api.delete(`/admin/settings/machines/${id}`);
    return response.data;
  }

  // Admin Settings - Finishes
  async getAdminFinishes(includeInactive = false, category?: string) {
    const response = await this.api.get('/admin/settings/finishes', { 
      params: { include_inactive: includeInactive, category } 
    });
    return response.data;
  }

  async createAdminFinish(data: any) {
    const response = await this.api.post('/admin/settings/finishes', data);
    return response.data;
  }

  async updateAdminFinish(id: number, data: any) {
    const response = await this.api.put(`/admin/settings/finishes/${id}`, data);
    return response.data;
  }

  async deleteAdminFinish(id: number) {
    const response = await this.api.delete(`/admin/settings/finishes/${id}`);
    return response.data;
  }

  // Admin Settings - Labor Rates
  async getAdminLaborRates(includeInactive = false) {
    const response = await this.api.get('/admin/settings/labor-rates', { 
      params: { include_inactive: includeInactive } 
    });
    return response.data;
  }

  async createAdminLaborRate(data: any) {
    const response = await this.api.post('/admin/settings/labor-rates', data);
    return response.data;
  }

  async updateAdminLaborRate(id: number, data: any) {
    const response = await this.api.put(`/admin/settings/labor-rates/${id}`, data);
    return response.data;
  }

  async deleteAdminLaborRate(id: number) {
    const response = await this.api.delete(`/admin/settings/labor-rates/${id}`);
    return response.data;
  }

  // Admin Settings - Work Center Rates
  async getAdminWorkCenterRates(includeInactive = false) {
    const response = await this.api.get('/admin/settings/work-center-rates', { 
      params: { include_inactive: includeInactive } 
    });
    return response.data;
  }

  async updateAdminWorkCenterRate(id: number, data: { hourly_rate: number }) {
    const response = await this.api.put(`/admin/settings/work-center-rates/${id}`, data);
    return response.data;
  }

  // Admin Settings - Outside Services
  async getAdminOutsideServices(includeInactive = false, processType?: string) {
    const response = await this.api.get('/admin/settings/outside-services', { 
      params: { include_inactive: includeInactive, process_type: processType } 
    });
    return response.data;
  }

  async createAdminOutsideService(data: any) {
    const response = await this.api.post('/admin/settings/outside-services', data);
    return response.data;
  }

  async updateAdminOutsideService(id: number, data: any) {
    const response = await this.api.put(`/admin/settings/outside-services/${id}`, data);
    return response.data;
  }

  async deleteAdminOutsideService(id: number) {
    const response = await this.api.delete(`/admin/settings/outside-services/${id}`);
    return response.data;
  }

  // Admin Settings - Overhead
  async getAdminOverhead() {
    const response = await this.api.get('/admin/settings/overhead');
    return response.data;
  }

  async updateAdminOverhead(key: string, value: string, settingType: string = 'text', description?: string) {
    const response = await this.api.put(`/admin/settings/overhead/${key}`, { 
      value, setting_type: settingType, description 
    });
    return response.data;
  }

  // Admin Settings - Audit Log
  async getSettingsAuditLog(entityType?: string, days = 30, limit = 100) {
    const response = await this.api.get('/admin/settings/audit-log', { 
      params: { entity_type: entityType, days, limit } 
    });
    return response.data;
  }

  // Admin Settings - Seed defaults
  async seedAdminLaborRates() {
    const response = await this.api.post('/admin/settings/seed-labor-rates');
    return response.data;
  }

  async seedAdminOutsideServices() {
    const response = await this.api.post('/admin/settings/seed-outside-services');
    return response.data;
  }

  // Admin Settings - Role Permissions
  async getRolePermissions() {
    const response = await this.api.get('/admin/settings/role-permissions');
    return response.data;
  }

  async updateRolePermissions(role: string, permissions: string[]) {
    const response = await this.api.put(`/admin/settings/role-permissions/${role}`, permissions);
    return response.data;
  }

  async resetRolePermissions(role: string) {
    const response = await this.api.post(`/admin/settings/role-permissions/${role}/reset`);
    return response.data;
  }

  // Receiving & Inspection
  async getOpenPOsForReceiving(vendorId?: number) {
    const response = await this.api.get('/receiving/open-pos', { params: { vendor_id: vendorId } });
    return response.data;
  }

  async getPOForReceiving(poId: number) {
    const response = await this.api.get(`/receiving/po/${poId}`);
    return response.data;
  }

  async receiveNewMaterial(data: {
    po_line_id: number;
    quantity_received: number;
    lot_number: string;
    serial_numbers?: string;
    heat_number?: string;
    cert_number?: string;
    coc_attached?: boolean;
    location_id?: number;
    requires_inspection?: boolean;
    packing_slip_number?: string;
    carrier?: string;
    tracking_number?: string;
    notes?: string;
    over_receive_approved?: boolean;
  }) {
    const response = await this.api.post('/receiving/receive', data);
    return response.data;
  }

  async getInspectionQueue(daysBack = 30) {
    const response = await this.api.get('/receiving/inspection-queue', { params: { days_back: daysBack } });
    return response.data;
  }

  async getReceiptDetail(receiptId: number) {
    const response = await this.api.get(`/receiving/receipt/${receiptId}`);
    return response.data;
  }

  async inspectReceiptNew(receiptId: number, data: {
    quantity_accepted: number;
    quantity_rejected: number;
    inspection_method: string;
    defect_type?: string;
    inspection_notes?: string;
  }) {
    const response = await this.api.post(`/receiving/inspect/${receiptId}`, data);
    return response.data;
  }

  async getReceivingHistory(days = 30, status?: string) {
    const response = await this.api.get('/receiving/history', { params: { days, status } });
    return response.data;
  }

  async getReceivingStats(days = 30) {
    const response = await this.api.get('/receiving/stats', { params: { days } });
    return response.data;
  }

  async getReceivingLocations() {
    const response = await this.api.get('/receiving/locations');
    return response.data;
  }

  // PO Upload & Extraction
  async uploadPOPdf(file: File) {
    const formData = new FormData();
    formData.append('file', file);
    const response = await this.api.post('/po-upload/upload-po', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 60000 // 60 second timeout for extraction
    });
    return response.data;
  }

  async createPOFromUpload(data: {
    po_number: string;
    vendor_id: number;
    create_vendor?: boolean;
    new_vendor_name?: string;
    new_vendor_code?: string;
    new_vendor_address?: string;
    order_date?: string;
    required_date?: string;
    expected_date?: string;
    payment_terms?: string;
    shipping_method?: string;
    ship_to?: string;
    notes?: string;
    line_items: Array<{
      part_id: number;
      part_number: string;
      description?: string;
      quantity_ordered: number;
      unit_price: number;
      line_total?: number;
      notes?: string;
    }>;
    create_parts?: Array<{
      part_number: string;
      description?: string;
    }>;
    pdf_path: string;
  }) {
    const response = await this.api.post('/po-upload/create-from-upload', data);
    return response.data;
  }

  async searchPartsForPO(query: string, limit = 10) {
    const response = await this.api.get('/po-upload/search-parts', { params: { q: query, limit } });
    return response.data;
  }

  async searchVendorsForPO(query: string, limit = 10) {
    const response = await this.api.get('/po-upload/search-vendors', { params: { q: query, limit } });
    return response.data;
  }

  getPOPdfUrl(path: string) {
    return `${this.api.defaults.baseURL}/po-upload/pdf/${path}`;
  }

  // Analytics & BI
  async getKPIDashboard(params?: { period?: string; start_date?: string; end_date?: string; work_center_id?: number }) {
    const response = await this.api.get('/analytics/kpis', { params });
    return response.data;
  }

  async getOEEDetails(params?: { period?: string; start_date?: string; end_date?: string; work_center_id?: number; granularity?: string }) {
    const response = await this.api.get('/analytics/oee', { params });
    return response.data;
  }

  async getProductionTrends(params?: { period?: string; start_date?: string; end_date?: string; group_by?: string; granularity?: string }) {
    const response = await this.api.get('/analytics/production-trends', { params });
    return response.data;
  }

  async getCostAnalysis(params?: { period?: string; start_date?: string; end_date?: string; work_order_id?: number }) {
    const response = await this.api.get('/analytics/cost-analysis', { params });
    return response.data;
  }

  async getAnalyticsQualityMetrics(params?: { period?: string; start_date?: string; end_date?: string; metric_type?: string }) {
    const response = await this.api.get('/analytics/quality-metrics', { params });
    return response.data;
  }

  async getInventoryTurnover(params?: { period?: string; start_date?: string; end_date?: string; category?: string }) {
    const response = await this.api.get('/analytics/inventory-turnover', { params });
    return response.data;
  }

  async predictDelivery(workOrderId: number) {
    const response = await this.api.get(`/analytics/predict/delivery/${workOrderId}`);
    return response.data;
  }

  async getCapacityForecast(weeksAhead = 4) {
    const response = await this.api.get('/analytics/predict/capacity', { params: { weeks_ahead: weeksAhead } });
    return response.data;
  }

  async getInventoryDemandPrediction() {
    const response = await this.api.get('/analytics/predict/inventory-demand');
    return response.data;
  }

  async getReportTemplates() {
    const response = await this.api.get('/analytics/custom-report/templates');
    return response.data;
  }

  async createReportTemplate(template: any) {
    const response = await this.api.post('/analytics/custom-report/templates', template);
    return response.data;
  }

  async runCustomReport(request: any) {
    const response = await this.api.post('/analytics/custom-report', request);
    return response.data;
  }

  async getDataSources() {
    const response = await this.api.get('/analytics/data-sources');
    return response.data;
  }

  // Search
  async search(query: string, type?: string) {
    const response = await this.api.get('/search', { params: { q: query, type } });
    return response.data;
  }

  async getRecentItems() {
    const response = await this.api.get('/search/recent');
    return response.data;
  }

  // Generic get method for flexibility
  async get<T = any>(url: string, config?: { params?: Record<string, any> }): Promise<{ data: T }> {
    const response = await this.api.get<T>(url, config);
    return { data: response.data };
  }
}

export const api = new ApiService();
export default api;
