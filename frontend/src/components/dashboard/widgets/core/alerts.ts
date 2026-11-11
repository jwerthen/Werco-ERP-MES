/**
 * The dashboard's exception list, shared by the "Needs attention" banner (first alert)
 * and the alert chips (every alert). Order is priority: overdue, NCRs, calibration, stock.
 */
import { useMemo, type ElementType } from 'react';
import { ClockIcon, CubeIcon, ShieldExclamationIcon, WrenchScrewdriverIcon } from '@heroicons/react/24/outline';
import { useDashboardSource } from '../../DashboardWidgetContext';
import type { EquipmentDueSoonRow, LowStockRow, QualitySummaryData } from '../../sources/coreSources';
import { useCoreDashboard } from './CoreDashboardContext';

export interface DashboardAlert {
  type: 'error' | 'warning' | 'info';
  message: string;
  link: string;
  icon?: ElementType;
}

export function useDashboardAlerts(): DashboardAlert[] {
  const { data } = useCoreDashboard();
  const quality = useDashboardSource<QualitySummaryData>('quality');
  const equipment = useDashboardSource<EquipmentDueSoonRow[]>('equipment');
  const stock = useDashboardSource<LowStockRow[]>('stock');
  const openNCRs = quality.data ? (quality.data.open_ncrs ?? 0) : null;
  const equipmentDue = equipment.data ? equipment.data.length : null;
  const lowInventory = stock.data ? stock.data.length : null;

  return useMemo<DashboardAlert[]>(() => {
    const next: DashboardAlert[] = [];
    if (data?.summary.overdue)
      next.push({
        type: 'error',
        message: `${data.summary.overdue} work order(s) are overdue`,
        link: '/work-orders?scope=overdue&cots=1',
        icon: ClockIcon,
      });
    if ((openNCRs ?? 0) > 0)
      next.push({
        type: 'warning',
        message: `${openNCRs} open NCR(s) require attention`,
        link: '/quality?tab=ncr&filter=open',
        icon: ShieldExclamationIcon,
      });
    if ((equipmentDue ?? 0) > 0)
      next.push({
        type: 'warning',
        message: `${equipmentDue} equipment item(s) due for calibration within 30 days`,
        link: '/calibration?filter=due_soon',
        icon: WrenchScrewdriverIcon,
      });
    if ((lowInventory ?? 0) > 0)
      next.push({
        type: 'warning',
        message: `${lowInventory} part(s) below reorder point`,
        link: '/warehouse?tab=inventory&filter=low_stock',
        icon: CubeIcon,
      });
    return next;
  }, [data, openNCRs, equipmentDue, lowInventory]);
}
