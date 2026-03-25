import axios, { AxiosInstance, AxiosError, AxiosRequestConfig } from 'axios';
import {
  LoginResponse,
  UserCreate,
  PartCreate,
  PartUpdate,
  PartListParams,
  WorkCenterCreate,
  WorkCenterUpdate,
  CustomerCreate,
  CustomerNameOption,
  CustomerStatsResponse,
} from '../types/api';
import { User, Part, WorkCenter } from '../types';

const API_BASE_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000/api/v1';

// ETag cache for conditional requests
interface CacheEntry {
  etag: string;
  data: unknown;
  timestamp: number;
}

// Global cache for ETag-based conditional requests
const etagCache = new Map<string, CacheEntry>();
const ETAG_CACHE_MAX_SIZE = 500;

// Evict oldest entries when cache exceeds max size
function pruneEtagCache(): void {
  if (etagCache.size <= ETAG_CACHE_MAX_SIZE) return;
  // Map iterates in insertion order; delete oldest entries
  const excess = etagCache.size - ETAG_CACHE_MAX_SIZE;
  let removed = 0;
  for (const key of Array.from(etagCache.keys())) {
    if (removed >= excess) break;
    etagCache.delete(key);
    removed++;
  }
}

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
        pruneEtagCache();
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

  async loginWithEmployeeId(employeeId: string): Promise<LoginResponse> {
    const response = await this.api.post<LoginResponse>('/auth/employee-login', { employee_id: employeeId });
    return response.data;
  }

  async logoutWithEmployeeId(employeeId: string) {
    const response = await this.api.post('/auth/employee-logout', { employee_id: employeeId });
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

  async getWorkCenterTypes(): Promise<{ types: string[] }> {
    const response = await this.api.get('/work-centers/types');
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

  // Admin Settings - Work Center Types
  async getAdminWorkCenterTypes(): Promise<{ types: string[]; in_use?: string[] }> {
    const response = await this.api.get('/admin/settings/work-center-types');
    return response.data;
  }

  async updateAdminWorkCenterTypes(types: string[]): Promise<{ types: string[]; in_use?: string[] }> {
    const response = await this.api.put('/admin/settings/work-center-types', { types });
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

  async getSuggestedPartNumber(description: string, partType: string): Promise<{ suggested_part_number: string | null; existing: boolean }> {
    const response = await this.api.get('/parts/generate-number', {
      params: { description, part_type: partType }
    });
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

  async unreleaseBOM(id: number) {
    const response = await this.api.post(`/bom/${id}/unrelease`);
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

  async importBOMDocument(formData: FormData) {
    const response = await this.api.post('/bom/import', formData, {
      headers: { 'Content-Type': 'multipart/form-data' }
    });
    return response.data;
  }

  async previewBOMImport(formData: FormData) {
    const response = await this.api.post('/bom/import/preview', formData, {
      headers: { 'Content-Type': 'multipart/form-data' }
    });
    return response.data;
  }

  async commitBOMImport(data: any) {
    const response = await this.api.post('/bom/import/commit', data);
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

  async updateWorkOrderPriority(id: number, priority: number, reason?: string) {
    const response = await this.api.put(`/work-orders/${id}/priority`, { priority, reason });
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
  async getShopFloorOperations(params?: { work_center_id?: number; status?: string; search?: string; due_today?: boolean }) {
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

  async getPurchaseOrderPrintData(poId: number) {
    const response = await this.api.get(`/print/purchase-orders/${poId}/print-data`);
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

  async scheduleWorkOrderEarliest(
    workOrderId: number,
    data?: { work_center_id?: number; start_date?: string; horizon_days?: number }
  ) {
    const response = await this.api.post(`/scheduling/work-orders/${workOrderId}/schedule-earliest`, data || {});
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

  async getCapacityHeatmap(startDate: string, endDate: string, workCenterId?: number) {
    const response = await this.api.get('/scheduling/capacity-heatmap', {
      params: { start_date: startDate, end_date: endDate, work_center_id: workCenterId },
    });
    return response.data;
  }

  async autoScheduleOperations(workCenterId?: number) {
    const response = await this.api.post('/scheduling/auto-schedule', null, { params: { work_center_id: workCenterId } });
    return response.data;
  }

  async unscheduleWorkOrder(workOrderId: number) {
    const response = await this.api.put(`/scheduling/work-orders/${workOrderId}/unschedule`);
    return response.data;
  }

  async getCapacityForDate(workCenterId: number, targetDate: string) {
    const response = await this.api.post('/scheduling/capacity-for-date', {
      work_center_id: workCenterId,
      target_date: targetDate,
    });
    return response.data;
  }

  async bulkScheduleEarliest(workOrderIds: number[], options?: { horizon_days?: number; forward_schedule?: boolean }) {
    const response = await this.api.post('/scheduling/bulk-schedule-earliest', {
      work_order_ids: workOrderIds,
      horizon_days: options?.horizon_days || 90,
      forward_schedule: options?.forward_schedule || false,
    });
    return response.data;
  }

  async runScheduling(data?: { work_center_ids?: number[]; horizon_days?: number; optimize_setup?: boolean }) {
    const response = await this.api.post('/scheduling/run', data || {});
    return response.data;
  }

  // Documents
  async getDocuments(params?: { part_id?: number; work_order_id?: number; vendor_id?: number; document_type?: string; search?: string }) {
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

  async generateCustomerQuotePdf(quoteId: number): Promise<Blob> {
    const response = await this.api.post(`/quotes/${quoteId}/generate-pdf`, null, {
      responseType: 'blob'
    });
    return response.data;
  }

  // AI RFQ Quotes
  async createRfqPackage(formData: FormData) {
    const response = await this.api.post('/rfq-packages/', formData, {
      headers: { 'Content-Type': 'multipart/form-data' }
    });
    return response.data;
  }

  async getRfqPackage(packageId: number) {
    const response = await this.api.get(`/rfq-packages/${packageId}`);
    return response.data;
  }

  async generateRfqEstimate(packageId: number, data: { target_margin_pct?: number; valid_days?: number }) {
    const response = await this.api.post(`/rfq-packages/${packageId}/generate-estimate`, data);
    return response.data;
  }

  async approveRfqEstimate(packageId: number) {
    const response = await this.api.post(`/rfq-packages/${packageId}/approve-create-quote`);
    return response.data;
  }

  async exportInternalEstimate(packageId: number): Promise<Blob> {
    const response = await this.api.get(`/rfq-packages/${packageId}/internal-estimate-export`, {
      responseType: 'blob'
    });
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

  async importUsersCsv(file: File, defaultPassword?: string) {
    const formData = new FormData();
    formData.append('file', file);
    if (defaultPassword && defaultPassword.trim()) {
      formData.append('default_password', defaultPassword.trim());
    }

    const response = await this.api.post('/users/import-csv', formData, {
      headers: { 'Content-Type': 'multipart/form-data' }
    });
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

  async getCustomerNames(): Promise<CustomerNameOption[]> {
    const response = await this.api.get<CustomerNameOption[]>('/customers/names');
    return response.data;
  }

  async createCustomer(data: CustomerCreate) {
    const response = await this.api.post('/customers/', data);
    return response.data;
  }

  async updateCustomer(customerId: number, data: any) {
    const response = await this.api.put(`/customers/${customerId}`, data);
    return response.data;
  }

  async getCustomerStats(customerId: number): Promise<CustomerStatsResponse> {
    const response = await this.api.get<CustomerStatsResponse>(`/customers/${customerId}/stats`);
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

  async uploadQuotePdf(file: File) {
    const formData = new FormData();
    formData.append('file', file);
    const response = await this.api.post('/po-upload/upload-quote', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 60000
    });
    return response.data;
  }

  async uploadInvoicePdf(file: File) {
    const formData = new FormData();
    formData.append('file', file);
    const response = await this.api.post('/po-upload/upload-invoice', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 60000
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

  // OEE Tracking
  async getOEERecords(params?: { work_center_id?: number; date_from?: string; date_to?: string; shift?: string; skip?: number; limit?: number }) {
    const response = await this.api.get('/oee/records', { params });
    return response.data;
  }

  async getOEERecord(id: number) {
    const response = await this.api.get(`/oee/records/${id}`);
    return response.data;
  }

  async createOEERecord(data: any) {
    const response = await this.api.post('/oee/records', data);
    return response.data;
  }

  async updateOEERecord(id: number, data: any) {
    const response = await this.api.put(`/oee/records/${id}`, data);
    return response.data;
  }

  async deleteOEERecord(id: number) {
    const response = await this.api.delete(`/oee/records/${id}`);
    return response.data;
  }

  async autoCalculateOEE(workCenterId: number, params?: { record_date?: string; shift?: string }) {
    const response = await this.api.post(`/oee/calculate/${workCenterId}`, null, { params });
    return response.data;
  }

  async getOEEDashboard(params?: { period?: string }) {
    const response = await this.api.get('/oee/dashboard', { params });
    return response.data;
  }

  async getOEETrends(params?: { work_center_id?: number; period?: string }) {
    const response = await this.api.get('/oee/trends', { params });
    return response.data;
  }

  async getOEESixBigLosses(workCenterId: number, params?: { period?: string }) {
    const response = await this.api.get(`/oee/six-big-losses/${workCenterId}`, { params });
    return response.data;
  }

  async getOEETargets() {
    const response = await this.api.get('/oee/targets');
    return response.data;
  }

  async createOEETarget(data: any) {
    const response = await this.api.post('/oee/targets', data);
    return response.data;
  }

  async updateOEETarget(id: number, data: any) {
    const response = await this.api.put(`/oee/targets/${id}`, data);
    return response.data;
  }

  async deleteOEETarget(id: number) {
    const response = await this.api.delete(`/oee/targets/${id}`);
    return response.data;
  }

  // Downtime Tracking
  async getDowntimeEvents(params?: { work_center_id?: number; category?: string; planned_type?: string; date_from?: string; date_to?: string; active_only?: boolean }) {
    const response = await this.api.get('/downtime/', { params });
    return response.data;
  }

  async getDowntimeEvent(id: number) {
    const response = await this.api.get(`/downtime/${id}`);
    return response.data;
  }

  async getActiveDowntime() {
    const response = await this.api.get('/downtime/active');
    return response.data;
  }

  async createDowntimeEvent(data: any) {
    const response = await this.api.post('/downtime/', data);
    return response.data;
  }

  async updateDowntimeEvent(id: number, data: any) {
    const response = await this.api.put(`/downtime/${id}`, data);
    return response.data;
  }

  async resolveDowntimeEvent(id: number, data: any) {
    const response = await this.api.post(`/downtime/${id}/resolve`, data);
    return response.data;
  }

  async getDowntimeSummary(params?: { date_from?: string; date_to?: string; work_center_id?: number }) {
    const response = await this.api.get('/downtime/summary', { params });
    return response.data;
  }

  async getDowntimeByWorkCenter(params?: { date_from?: string; date_to?: string }) {
    const response = await this.api.get('/downtime/by-work-center', { params });
    return response.data;
  }

  async getDowntimeReasonCodes(params?: { category?: string; active_only?: boolean }) {
    const response = await this.api.get('/downtime/reason-codes', { params });
    return response.data;
  }

  async createDowntimeReasonCode(data: any) {
    const response = await this.api.post('/downtime/reason-codes', data);
    return response.data;
  }

  async updateDowntimeReasonCode(id: number, data: any) {
    const response = await this.api.put(`/downtime/reason-codes/${id}`, data);
    return response.data;
  }

  // Job Costing
  async getJobCosts(params?: Record<string, any>) {
    const response = await this.api.get('/job-costs/', { params });
    return response.data;
  }

  async getJobCost(id: number) {
    const response = await this.api.get(`/job-costs/${id}`);
    return response.data;
  }

  async createJobCost(data: any) {
    const response = await this.api.post('/job-costs/', data);
    return response.data;
  }

  async updateJobCost(id: number, data: any) {
    const response = await this.api.put(`/job-costs/${id}`, data);
    return response.data;
  }

  async getJobCostSummary() {
    const response = await this.api.get('/job-costs/summary');
    return response.data;
  }

  async getJobCostEntries(jobCostId: number) {
    const response = await this.api.get(`/job-costs/${jobCostId}/entries`);
    return response.data;
  }

  async addJobCostEntry(jobCostId: number, data: any) {
    const response = await this.api.post(`/job-costs/${jobCostId}/entries`, data);
    return response.data;
  }

  async deleteJobCostEntry(jobCostId: number, entryId: number) {
    const response = await this.api.delete(`/job-costs/${jobCostId}/entries/${entryId}`);
    return response.data;
  }

  async recalculateJobCost(jobCostId: number) {
    const response = await this.api.post(`/job-costs/${jobCostId}/calculate`);
    return response.data;
  }

  async getJobCostVarianceReport(jobCostId: number) {
    const response = await this.api.get(`/job-costs/${jobCostId}/variance-report`);
    return response.data;
  }

  // Tool & Fixture Management
  async getToolDashboard() {
    const response = await this.api.get('/tool-management/tools/dashboard');
    return response.data;
  }

  async getTools(params?: { status?: string; tool_type?: string; search?: string; include_inactive?: boolean }) {
    const response = await this.api.get('/tool-management/tools/', { params });
    return response.data;
  }

  async getTool(id: number) {
    const response = await this.api.get(`/tool-management/tools/${id}`);
    return response.data;
  }

  async createTool(data: any) {
    const response = await this.api.post('/tool-management/tools/', data);
    return response.data;
  }

  async updateTool(id: number, data: any) {
    const response = await this.api.put(`/tool-management/tools/${id}`, data);
    return response.data;
  }

  async retireTool(id: number) {
    const response = await this.api.delete(`/tool-management/tools/${id}`);
    return response.data;
  }

  async checkoutTool(id: number, data: any) {
    const response = await this.api.post(`/tool-management/tools/${id}/checkout`, data);
    return response.data;
  }

  async checkinTool(id: number, data: any) {
    const response = await this.api.post(`/tool-management/tools/${id}/checkin`, data);
    return response.data;
  }

  async logToolUsage(id: number, data: any) {
    const response = await this.api.post(`/tool-management/tools/${id}/log-usage`, data);
    return response.data;
  }

  async getToolHistory(id: number) {
    const response = await this.api.get(`/tool-management/tools/${id}/history`);
    return response.data;
  }

  async getToolsCheckedOut() {
    const response = await this.api.get('/tool-management/tools/checked-out');
    return response.data;
  }

  async getToolsReplacementDue() {
    const response = await this.api.get('/tool-management/tools/replacement-due');
    return response.data;
  }

  async getToolsInspectionDue() {
    const response = await this.api.get('/tool-management/tools/inspection-due');
    return response.data;
  }

  // Preventive Maintenance
  async getMaintenanceSchedules(params?: { work_center_id?: number; is_active?: boolean }) {
    const response = await this.api.get('/maintenance/schedules', { params });
    return response.data;
  }

  async getMaintenanceSchedule(id: number) {
    const response = await this.api.get(`/maintenance/schedules/${id}`);
    return response.data;
  }

  async createMaintenanceSchedule(data: any) {
    const response = await this.api.post('/maintenance/schedules', data);
    return response.data;
  }

  async updateMaintenanceSchedule(id: number, data: any) {
    const response = await this.api.put(`/maintenance/schedules/${id}`, data);
    return response.data;
  }

  async deleteMaintenanceSchedule(id: number) {
    const response = await this.api.delete(`/maintenance/schedules/${id}`);
    return response.data;
  }

  async getMaintenanceWorkOrders(params?: { status?: string; work_center_id?: number; maintenance_type?: string; start_date?: string; end_date?: string }) {
    const response = await this.api.get('/maintenance/work-orders', { params });
    return response.data;
  }

  async getMaintenanceWorkOrder(id: number) {
    const response = await this.api.get(`/maintenance/work-orders/${id}`);
    return response.data;
  }

  async createMaintenanceWorkOrder(data: any) {
    const response = await this.api.post('/maintenance/work-orders', data);
    return response.data;
  }

  async updateMaintenanceWorkOrder(id: number, data: any) {
    const response = await this.api.put(`/maintenance/work-orders/${id}`, data);
    return response.data;
  }

  async startMaintenanceWorkOrder(id: number) {
    const response = await this.api.post(`/maintenance/work-orders/${id}/start`);
    return response.data;
  }

  async completeMaintenanceWorkOrder(id: number, data: any) {
    const response = await this.api.post(`/maintenance/work-orders/${id}/complete`, data);
    return response.data;
  }

  async getOverdueMaintenanceWorkOrders() {
    const response = await this.api.get('/maintenance/work-orders/overdue');
    return response.data;
  }

  async getMaintenanceCalendar(start_date: string, end_date: string) {
    const response = await this.api.get('/maintenance/calendar', { params: { start_date, end_date } });
    return response.data;
  }

  async getMaintenanceDashboard() {
    const response = await this.api.get('/maintenance/dashboard');
    return response.data;
  }

  async getMaintenanceHistory(work_center_id: number, limit?: number) {
    const response = await this.api.get(`/maintenance/history/${work_center_id}`, { params: { limit } });
    return response.data;
  }

  async createMaintenanceLog(data: any) {
    const response = await this.api.post('/maintenance/log', data);
    return response.data;
  }

  // Operator Certifications
  async getCertificationsDashboard() {
    const response = await this.api.get('/certifications/certifications/dashboard');
    return response.data;
  }

  async getCertifications(params?: { user_id?: number; status?: string; expiring_within_days?: number; skip?: number; limit?: number }) {
    const response = await this.api.get('/certifications/certifications/', { params });
    return response.data;
  }

  async getCertification(id: number) {
    const response = await this.api.get(`/certifications/certifications/${id}`);
    return response.data;
  }

  async createCertification(data: any) {
    const response = await this.api.post('/certifications/certifications/', data);
    return response.data;
  }

  async updateCertification(id: number, data: any) {
    const response = await this.api.put(`/certifications/certifications/${id}`, data);
    return response.data;
  }

  async deleteCertification(id: number) {
    const response = await this.api.delete(`/certifications/certifications/${id}`);
    return response.data;
  }

  async getExpiringCertifications(params?: { days?: number }) {
    const response = await this.api.get('/certifications/certifications/expiring', { params });
    return response.data;
  }

  async getUserCertifications(userId: number) {
    const response = await this.api.get(`/certifications/certifications/user/${userId}`);
    return response.data;
  }

  async getTrainingRecords(params?: { user_id?: number; status?: string }) {
    const response = await this.api.get('/certifications/training/', { params });
    return response.data;
  }

  async createTrainingRecord(data: any) {
    const response = await this.api.post('/certifications/training/', data);
    return response.data;
  }

  async updateTrainingRecord(id: number, data: any) {
    const response = await this.api.put(`/certifications/training/${id}`, data);
    return response.data;
  }

  async getSkillMatrix(params?: { user_id?: number; work_center_id?: number }) {
    const response = await this.api.get('/certifications/skill-matrix/', { params });
    return response.data;
  }

  async createSkillMatrixEntry(data: any) {
    const response = await this.api.post('/certifications/skill-matrix/', data);
    return response.data;
  }

  async updateSkillMatrixEntry(id: number, data: any) {
    const response = await this.api.put(`/certifications/skill-matrix/${id}`, data);
    return response.data;
  }

  // Engineering Change Orders
  async getECODashboard() {
    const response = await this.api.get('/eco/eco/dashboard');
    return response.data;
  }

  async getECOs(params?: { status?: string; priority?: string; eco_type?: string; requestor_id?: number; skip?: number; limit?: number }) {
    const response = await this.api.get('/eco/eco/', { params });
    return response.data;
  }

  async getECO(id: number) {
    const response = await this.api.get(`/eco/eco/${id}`);
    return response.data;
  }

  async createECO(data: any) {
    const response = await this.api.post('/eco/eco/', data);
    return response.data;
  }

  async updateECO(id: number, data: any) {
    const response = await this.api.put(`/eco/eco/${id}`, data);
    return response.data;
  }

  async submitECO(id: number) {
    const response = await this.api.post(`/eco/eco/${id}/submit`);
    return response.data;
  }

  async approveECO(id: number, data?: any) {
    const response = await this.api.post(`/eco/eco/${id}/approve`, data);
    return response.data;
  }

  async rejectECO(id: number, data?: any) {
    const response = await this.api.post(`/eco/eco/${id}/reject`, data);
    return response.data;
  }

  async implementECO(id: number) {
    const response = await this.api.post(`/eco/eco/${id}/implement`);
    return response.data;
  }

  async completeECO(id: number) {
    const response = await this.api.post(`/eco/eco/${id}/complete`);
    return response.data;
  }

  async getECOApprovals(ecoId: number) {
    const response = await this.api.get(`/eco/eco/${ecoId}/approvals`);
    return response.data;
  }

  async addECOApproval(ecoId: number, data: any) {
    const response = await this.api.post(`/eco/eco/${ecoId}/approvals`, data);
    return response.data;
  }

  async addECOTask(ecoId: number, data: any) {
    const response = await this.api.post(`/eco/eco/${ecoId}/tasks`, data);
    return response.data;
  }

  async updateECOTask(ecoId: number, taskId: number, data: any) {
    const response = await this.api.put(`/eco/eco/${ecoId}/tasks/${taskId}`, data);
    return response.data;
  }

  async getECOAffectedItems(ecoId: number) {
    const response = await this.api.get(`/eco/eco/affected-items/${ecoId}`);
    return response.data;
  }

  // SPC (Statistical Process Control)
  async getSPCDashboard() {
    const response = await this.api.get('/spc/dashboard');
    return response.data;
  }

  async getSPCCharacteristics(params?: { part_id?: number; active_only?: boolean }) {
    const response = await this.api.get('/spc/characteristics', { params });
    return response.data;
  }

  async getSPCCharacteristic(id: number) {
    const response = await this.api.get(`/spc/characteristics/${id}`);
    return response.data;
  }

  async createSPCCharacteristic(data: any) {
    const response = await this.api.post('/spc/characteristics', data);
    return response.data;
  }

  async updateSPCCharacteristic(id: number, data: any) {
    const response = await this.api.put(`/spc/characteristics/${id}`, data);
    return response.data;
  }

  async addSPCMeasurements(data: any) {
    const response = await this.api.post('/spc/measurements', data);
    return response.data;
  }

  async getSPCMeasurements(characteristicId: number, params?: { limit?: number; offset?: number }) {
    const response = await this.api.get(`/spc/measurements/${characteristicId}`, { params });
    return response.data;
  }

  async getSPCChartData(characteristicId: number, params?: { chart_type?: string; limit?: number }) {
    const response = await this.api.get(`/spc/chart-data/${characteristicId}`, { params });
    return response.data;
  }

  async calculateSPCControlLimits(characteristicId: number) {
    const response = await this.api.post(`/spc/control-limits/${characteristicId}/calculate`);
    return response.data;
  }

  async getSPCControlLimits(characteristicId: number) {
    const response = await this.api.get(`/spc/control-limits/${characteristicId}`);
    return response.data;
  }

  async runSPCCapabilityStudy(characteristicId: number) {
    const response = await this.api.post(`/spc/capability-study/${characteristicId}`);
    return response.data;
  }

  async getSPCCapability(characteristicId: number) {
    const response = await this.api.get(`/spc/capability/${characteristicId}`);
    return response.data;
  }

  async getSPCOutOfControl() {
    const response = await this.api.get('/spc/out-of-control');
    return response.data;
  }

  async getSPCViolations(characteristicId: number) {
    const response = await this.api.get(`/spc/violations/${characteristicId}`);
    return response.data;
  }

  // Customer Complaints & RMA
  async getComplaintsDashboard() {
    const response = await this.api.get('/complaints/complaints/dashboard');
    return response.data;
  }

  async getComplaints(params?: { status?: string; severity?: string; customer_id?: number; skip?: number; limit?: number }) {
    const response = await this.api.get('/complaints/complaints/', { params });
    return response.data;
  }

  async getComplaint(id: number) {
    const response = await this.api.get(`/complaints/complaints/${id}`);
    return response.data;
  }

  async createComplaint(data: any) {
    const response = await this.api.post('/complaints/complaints/', data);
    return response.data;
  }

  async updateComplaint(id: number, data: any) {
    const response = await this.api.put(`/complaints/complaints/${id}`, data);
    return response.data;
  }

  async investigateComplaint(id: number, data: any) {
    const response = await this.api.post(`/complaints/complaints/${id}/investigate`, data);
    return response.data;
  }

  async resolveComplaint(id: number, data: any) {
    const response = await this.api.post(`/complaints/complaints/${id}/resolve`, data);
    return response.data;
  }

  async closeComplaint(id: number, data?: any) {
    const response = await this.api.post(`/complaints/complaints/${id}/close`, data);
    return response.data;
  }

  async createNCRFromComplaint(complaintId: number) {
    const response = await this.api.post(`/complaints/complaints/${complaintId}/create-ncr`);
    return response.data;
  }

  async createCARFromComplaint(complaintId: number) {
    const response = await this.api.post(`/complaints/complaints/${complaintId}/create-car`);
    return response.data;
  }

  async get8DReport(complaintId: number) {
    const response = await this.api.get(`/complaints/complaints/8d-report/${complaintId}`);
    return response.data;
  }

  async getRMAs(params?: { status?: string; customer_id?: number }) {
    const response = await this.api.get('/complaints/rma/', { params });
    return response.data;
  }

  async getRMA(id: number) {
    const response = await this.api.get(`/complaints/rma/${id}`);
    return response.data;
  }

  async createRMA(data: any) {
    const response = await this.api.post('/complaints/rma/', data);
    return response.data;
  }

  async updateRMA(id: number, data: any) {
    const response = await this.api.put(`/complaints/rma/${id}`, data);
    return response.data;
  }

  async approveRMA(id: number) {
    const response = await this.api.post(`/complaints/rma/${id}/approve`);
    return response.data;
  }

  async denyRMA(id: number, data?: any) {
    const response = await this.api.post(`/complaints/rma/${id}/deny`, data);
    return response.data;
  }

  async receiveRMA(id: number, data?: any) {
    const response = await this.api.post(`/complaints/rma/${id}/receive`, data);
    return response.data;
  }

  async inspectRMA(id: number, data?: any) {
    const response = await this.api.post(`/complaints/rma/${id}/inspect`, data);
    return response.data;
  }

  async disposeRMA(id: number, data?: any) {
    const response = await this.api.post(`/complaints/rma/${id}/dispose`, data);
    return response.data;
  }

  // Supplier Scorecards
  async getSupplierScorecardsDashboard() {
    const response = await this.api.get('/supplier-scorecards/supplier-scorecards/dashboard');
    return response.data;
  }

  async getSupplierRanking() {
    const response = await this.api.get('/supplier-scorecards/supplier-scorecards/ranking');
    return response.data;
  }

  async getSupplierScorecards(params?: { vendor_id?: number; period?: string; skip?: number; limit?: number }) {
    const response = await this.api.get('/supplier-scorecards/supplier-scorecards/', { params });
    return response.data;
  }

  async getSupplierScorecard(id: number) {
    const response = await this.api.get(`/supplier-scorecards/supplier-scorecards/${id}`);
    return response.data;
  }

  async createSupplierScorecard(data: any) {
    const response = await this.api.post('/supplier-scorecards/supplier-scorecards/', data);
    return response.data;
  }

  async updateSupplierScorecard(id: number, data: any) {
    const response = await this.api.put(`/supplier-scorecards/supplier-scorecards/${id}`, data);
    return response.data;
  }

  async calculateSupplierScorecard(vendorId: number) {
    const response = await this.api.post(`/supplier-scorecards/supplier-scorecards/calculate/${vendorId}`);
    return response.data;
  }

  async getSupplierHistory(vendorId: number) {
    const response = await this.api.get(`/supplier-scorecards/supplier-scorecards/vendor/${vendorId}/history`);
    return response.data;
  }

  async getSupplierAudits(params?: { vendor_id?: number; status?: string }) {
    const response = await this.api.get('/supplier-scorecards/supplier-audits/', { params });
    return response.data;
  }

  async getSupplierAuditsDueSoon() {
    const response = await this.api.get('/supplier-scorecards/supplier-audits/due-soon');
    return response.data;
  }

  async createSupplierAudit(data: any) {
    const response = await this.api.post('/supplier-scorecards/supplier-audits/', data);
    return response.data;
  }

  async updateSupplierAudit(id: number, data: any) {
    const response = await this.api.put(`/supplier-scorecards/supplier-audits/${id}`, data);
    return response.data;
  }

  async getApprovedSuppliers(params?: { vendor_id?: number; status?: string }) {
    const response = await this.api.get('/supplier-scorecards/approved-suppliers/', { params });
    return response.data;
  }

  async getApprovedSupplier(id: number) {
    const response = await this.api.get(`/supplier-scorecards/approved-suppliers/${id}`);
    return response.data;
  }

  async createApprovedSupplier(data: any) {
    const response = await this.api.post('/supplier-scorecards/approved-suppliers/', data);
    return response.data;
  }

  async updateApprovedSupplier(id: number, data: any) {
    const response = await this.api.put(`/supplier-scorecards/approved-suppliers/${id}`, data);
    return response.data;
  }

  // Generic get method for flexibility
  async get<T = any>(url: string, config?: { params?: Record<string, any> }): Promise<{ data: T }> {
    const response = await this.api.get<T>(url, config);
    return { data: response.data };
  }

  // Generic post method for flexibility
  async post<T = any>(url: string, data?: any): Promise<{ data: T }> {
    const response = await this.api.post<T>(url, data);
    return { data: response.data };
  }

  // Generic put method for flexibility
  async put<T = any>(url: string, data?: any): Promise<{ data: T }> {
    const response = await this.api.put<T>(url, data);
    return { data: response.data };
  }

  // Generic patch method for flexibility
  async patch<T = any>(url: string, data?: any): Promise<{ data: T }> {
    const response = await this.api.patch<T>(url, data);
    return { data: response.data };
  }

  // Generic delete method for flexibility
  async delete<T = any>(url: string): Promise<{ data: T }> {
    const response = await this.api.delete<T>(url);
    return { data: response.data };
  }
}

export const api = new ApiService();
export default api;
