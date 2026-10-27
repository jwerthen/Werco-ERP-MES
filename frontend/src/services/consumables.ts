import api from './api';
import type {
  ConsumableCheckout,
  ConsumableCheckoutCreate,
  ConsumableHistoryFilters,
  ConsumableHistoryParams,
  ConsumableMetadata,
  ConsumablePage,
  ConsumableStock,
  ConsumableStockParams,
} from '../types/consumables';

const base = '/consumables';

const consumables = {
  async metadata(): Promise<ConsumableMetadata> {
    return (await api.get<ConsumableMetadata>(`${base}/metadata`)).data;
  },
  async stock(params: ConsumableStockParams = {}): Promise<ConsumablePage<ConsumableStock>> {
    return (await api.get<ConsumablePage<ConsumableStock>>(`${base}/stock`, { params })).data;
  },
  async checkouts(params: ConsumableHistoryParams = {}): Promise<ConsumablePage<ConsumableCheckout>> {
    return (await api.get<ConsumablePage<ConsumableCheckout>>(`${base}/checkouts`, { params })).data;
  },
  async checkout(values: ConsumableCheckoutCreate): Promise<ConsumableCheckout> {
    return (await api.post<ConsumableCheckout>(`${base}/checkouts`, values)).data;
  },
  async exportCsv(params: ConsumableHistoryFilters = {}): Promise<string> {
    return (await api.get<string>(`${base}/checkouts/export.csv`, { params })).data;
  },
};

export default consumables;
