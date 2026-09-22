export interface HankRecordContext {
  workOrderId?: number;
  purchaseOrderId?: number;
  partId?: number;
}

export function hankRecordContext(pathname: string, search = ''): HankRecordContext {
  const positiveId = (value: string | null | undefined) => {
    if (!value || !/^[1-9]\d*$/.test(value)) return undefined;
    const id = Number(value);
    return Number.isSafeInteger(id) ? id : undefined;
  };
  const workOrderId = positiveId(/^\/work-orders\/([1-9]\d*)$/.exec(pathname)?.[1]);
  const partId = positiveId(/^\/parts\/([1-9]\d*)$/.exec(pathname)?.[1]);
  const params = new URLSearchParams(search);
  const poWorkspace =
    ['/purchasing', '/receiving'].includes(pathname) ||
    (pathname === '/warehouse' && params.get('tab') === 'receiving');
  const purchaseOrderId = poWorkspace ? positiveId(params.get('po')) : undefined;
  return { workOrderId, purchaseOrderId, partId };
}
