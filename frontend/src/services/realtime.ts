type QueryParams = Record<string, string | number | boolean | null | undefined>;

const API_BASE_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000/api/v1';
const WS_BASE_URL = process.env.REACT_APP_WS_URL || API_BASE_URL.replace(/^http/, 'ws');

const trimTrailingSlash = (value: string) => value.replace(/\/+$/, '');

export const getAccessToken = (): string | null => {
  return localStorage.getItem('token');
};

export const buildWsUrl = (path: string, params?: QueryParams): string => {
  const base = trimTrailingSlash(WS_BASE_URL);
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  const url = new URL(`${base}${normalizedPath}`);

  if (params) {
    Object.entries(params).forEach(([key, value]) => {
      if (value !== null && value !== undefined) {
        url.searchParams.set(key, String(value));
      }
    });
  }

  return url.toString();
};
