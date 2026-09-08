export type NestingPriceBasis = 'per_lb' | 'per_cubic_inch' | 'per_square_foot';

export type NestingCatalogMaterial = {
  id: number;
  name: string;
  category: string;
  source_updated_at: string | null;
  catalog_hash: string;
  density_lb_per_cubic_inch: string | null;
  price_options: {
    price_basis: NestingPriceBasis;
    price_key: string | null;
    source_field: string;
    unit_price: string | null;
  }[];
  missing_metadata: string[];
};

export type NestingCatalogResponse = {
  schema_version: 1;
  items: NestingCatalogMaterial[];
  total: number;
  offset: number;
  limit: number;
};

export type NestingMaterialRequest = {
  catalog_material_id: number;
  thickness_in: string;
  stock_options: { id: string; width_in: string; length_in: string }[];
  price_basis: NestingPriceBasis;
  price_key?: string;
  expected_catalog_hash?: string;
};

export type NestingMaterialResolution = {
  schema_version: 1;
  company_id: number;
  catalog_material: NestingCatalogMaterial;
  thickness_in: string;
  price_basis: NestingPriceBasis;
  price_key: string | null;
  currency: null;
  status: 'review_required' | 'unresolved';
  calculable: boolean;
  confirmed: false;
  stocks: {
    id: string;
    width_in: string;
    length_in: string;
    area_sq_in: string;
    area_sq_ft: string;
    volume_cu_in: string;
    weight_lb: string | null;
    sheet_cost: string | null;
  }[];
  issues: { code: string; field: string; message: string }[];
  content_hash: string;
};
