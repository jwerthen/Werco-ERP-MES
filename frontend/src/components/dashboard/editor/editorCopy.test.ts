/**
 * Edit mode's copy and element ids: the per-widget ids every focus request targets are
 * unique and valid across the whole registry, and the copy helpers with a branch say the
 * right thing on each side of it.
 */
import { ALL_WIDGETS } from '../widgetRegistry';
import {
  RESET_FAILED,
  RESET_FAILED_AFTER_LEAVING,
  SAVE_FAILED,
  SAVE_FAILED_AFTER_LEAVING,
  arrangeMenuId,
  bannerLabel,
  carriedNotice,
  galleryAddId,
  moveButtonId,
  moveLabel,
  positionTextId,
  removeButtonId,
  safeId,
  unknownWidgetsNotice,
} from './editorCopy';

jest.mock('../../../services/api', () => ({ __esModule: true, default: {} }));

const ID_BUILDERS = { moveButtonId, removeButtonId, arrangeMenuId, positionTextId, galleryAddId };

describe('element ids', () => {
  it('maps every character a widget id may carry outside [a-zA-Z0-9_-] to "-"', () => {
    expect(safeId('kpi.overdue')).toBe('kpi-overdue');
    expect(safeId('capacity_overview')).toBe('capacity_overview');
    expect(moveButtonId('kpi.open_ncrs')).toBe('dashboard-edit-move-kpi-open_ncrs');
  });

  it.each(Object.entries(ID_BUILDERS))(
    '%s gives every registered widget its own valid, unambiguous id',
    (_name, build) => {
      const ids = ALL_WIDGETS.map(definition => build(definition.id));
      expect(new Set(ids).size).toBe(ALL_WIDGETS.length);
      ids.forEach(id => expect(id).toMatch(/^[a-zA-Z][a-zA-Z0-9_-]*$/));
      // A focus request looks the control up by this id: no selector or whitespace surprises.
      ids.forEach(id => expect(id).not.toMatch(/[.\s#]/));
    }
  );

  it('never lets two builders collide for the same widget', () => {
    ALL_WIDGETS.forEach(definition => {
      const ids = Object.values(ID_BUILDERS).map(build => build(definition.id));
      expect(new Set(ids).size).toBe(ids.length);
    });
  });
});

describe('copy helpers with a branch', () => {
  it('names a panel Move button for moving or resizing, and a KPI tile for moving only', () => {
    expect(moveLabel('Capacity Overview', 'panel')).toBe('Move or resize Capacity Overview');
    expect(moveLabel('Overdue', 'kpi')).toBe('Move Overdue');
  });

  it('says how many saved widgets are carried, in the singular and the plural', () => {
    expect(carriedNotice(1)).toBe(
      '1 widget in your saved layout is not available to you right now; it is kept and will reappear if access returns.'
    );
    expect(carriedNotice(3)).toBe(
      '3 widgets in your saved layout are not available to you right now; they are kept and will reappear if access returns.'
    );
  });

  it('says how many unknown saved widgets a save would drop, in the singular and the plural', () => {
    expect(unknownWidgetsNotice(1)).toBe(
      "1 widget from a newer version of the app isn't shown here and will be removed if you save. Refresh the page to keep it."
    );
    expect(unknownWidgetsNotice(2)).toBe(
      "2 widgets from a newer version of the app aren't shown here and will be removed if you save. Refresh the page to keep them."
    );
  });

  it('labels a banner Undo step by what the step did', () => {
    expect(bannerLabel('Attention', true)).toBe('showed Attention');
    expect(bannerLabel('Attention', false)).toBe('hid Attention');
  });
});

describe('failures reported after the user left the page', () => {
  it.each([
    ['save', SAVE_FAILED_AFTER_LEAVING, SAVE_FAILED],
    ['reset', RESET_FAILED_AFTER_LEAVING, RESET_FAILED],
  ])(
    'a failed %s offers no "try again" (the draft is gone) and says the layout is unchanged',
    (_verb, after, inPage) => {
      expect(inPage).toMatch(/Try again\.$/);
      expect(after).not.toBe(inPage);
      expect(after).not.toMatch(/try again/i);
      expect(after).toMatch(/unchanged\.$/);
    }
  );
});
