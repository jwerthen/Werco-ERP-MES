import api from './api';
import type {
  ImprovementCreate,
  ImprovementDetail,
  ImprovementFilters,
  ImprovementList,
  ImprovementMetadata,
  ImprovementUpdate,
} from '../types/continuousImprovement';

const base = '/continuous-improvement';

const continuousImprovement = {
  async metadata(): Promise<ImprovementMetadata> {
    return (await api.get<ImprovementMetadata>(`${base}/metadata`)).data;
  },
  async list(params: ImprovementFilters = {}): Promise<ImprovementList> {
    return (await api.get<ImprovementList>(`${base}/`, { params })).data;
  },
  async detail(id: number): Promise<ImprovementDetail> {
    return (await api.get<ImprovementDetail>(`${base}/${id}`)).data;
  },
  async create(values: ImprovementCreate): Promise<ImprovementDetail> {
    return (await api.post<ImprovementDetail>(`${base}/`, values)).data;
  },
  async update(id: number, values: ImprovementUpdate): Promise<ImprovementDetail> {
    return (await api.patch<ImprovementDetail>(`${base}/${id}`, values)).data;
  },
  async comment(id: number, expected_version: number, body: string): Promise<ImprovementDetail> {
    return (await api.post<ImprovementDetail>(`${base}/${id}/comments`, { expected_version, body })).data;
  },
};

export default continuousImprovement;
