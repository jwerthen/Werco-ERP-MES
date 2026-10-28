/** Preserve the exact customer/PO pair, including reserved URL characters. */
export function customerPoDashboardUrl(customerName: string | null | undefined, customerPo: string): string {
  const params = new URLSearchParams({ customer_name: customerName ?? '', customer_po: customerPo });
  return `/work-orders/customer-pos?${params.toString()}`;
}
