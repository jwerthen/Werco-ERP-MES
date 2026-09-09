import { catalogFixture, catalogQuoteFixture } from '../../../test-utils/nestingCatalogFixtures';
import {
  clearCatalogPricing,
  hasAcknowledgedCatalogPricing,
  resolutionMatchesInputs,
  validateMaterialBinding,
} from './material-binding';
import { addImportedParts, createBlankProject, projectFromFile, projectToFile } from './quote-project';
import { createBlankQuote, validateQuote } from './quoting';
import { bounds, rect } from './nesting';

describe('explicit ERP material and price provenance', () => {
  it('requires a matching USD acknowledgment and clears applied prices without erasing source evidence', () => {
    const { quote, binding } = catalogQuoteFixture(true);
    expect(validateQuote(quote)).toBe(quote);
    expect(hasAcknowledgedCatalogPricing(quote)).toBe(true);
    const cleared = clearCatalogPricing(quote);
    expect(cleared.options.every(option => option.price === null)).toBe(true);
    expect(cleared.materialBinding?.acknowledgement).toBeUndefined();
    expect(cleared.materialBinding?.catalog).toEqual(binding.catalog);
    expect(cleared.materialBinding?.resolution).toEqual(binding.resolution);
    expect(hasAcknowledgedCatalogPricing(cleared)).toBe(false);
    expect(quote.materialBinding?.acknowledgement).toBeDefined();
    expect(quote.options.every(option => option.price !== null)).toBe(true);
  });

  it.each(['thickness', 'stock dimensions', 'catalog revision', 'price basis'] as const)(
    'refuses stale %s pricing',
    change => {
      const { quote } = catalogQuoteFixture(true);
      if (change === 'thickness') quote.thickness *= 2;
      if (change === 'stock dimensions') quote.options[0].width += 25.4;
      if (change === 'catalog revision')
        quote.materialBinding!.catalog = { ...quote.materialBinding!.catalog, catalog_hash: 'c'.repeat(64) };
      if (change === 'price basis') quote.materialBinding!.priceBasis = 'per_cubic_inch';
      expect(resolutionMatchesInputs(quote)).toBe(false);
      expect(() => validateQuote(quote)).toThrow();
    }
  );

  it('refuses forged approval/currency and crossed-company or acknowledgment snapshots', () => {
    const { binding } = catalogQuoteFixture(true);
    for (const patch of [{ confirmed: true }, { currency: 'USD' }, { company_id: 99 }])
      expect(() => validateMaterialBinding({ ...binding, resolution: { ...binding.resolution, ...patch } })).toThrow();
    expect(() =>
      validateMaterialBinding({
        ...binding,
        acknowledgement: { ...binding.acknowledgement, contentHash: 'c'.repeat(64) },
      })
    ).toThrow();
    expect(() => validateQuote({ ...catalogQuoteFixture().quote, material: 'Aluminum' })).toThrow('material family');
  });

  it.each(['catalog ID', 'company ID'] as const)(
    'keeps equal-name records with different %s separate across version5 Save/Open',
    difference => {
      const first = { companyId: 2, catalog: catalogFixture(11) };
      const second = {
        companyId: difference === 'company ID' ? 3 : 2,
        catalog: catalogFixture(difference === 'catalog ID' ? 12 : 11),
      };
      const parts = ['a', 'b'].map(id => ({
        id,
        name: id,
        quantity: 1,
        rotate: false,
        color: 0,
        loops: [rect(20, 10)],
      }));
      const project = addImportedParts(
        createBlankProject(),
        [
          { material: 'Carbon steel', thickness: 3.175, materialBinding: first, partIds: ['a'] },
          { material: 'Carbon steel', thickness: 3.175, materialBinding: second, partIds: ['b'] },
        ],
        parts
      );
      const populated = project.groups.filter(group => group.quote.parts.length);
      expect(populated).toHaveLength(2);
      expect(populated.map(group => group.quote.materialBinding!.catalog.id)).toEqual([11, second.catalog.id]);
      expect(populated.map(group => group.quote.materialBinding!.companyId)).toEqual([2, second.companyId]);
      // Retain the pre-profile version 5 compatibility case explicitly.
      project.groups.forEach(group => {
        delete group.quote.geometryProfile;
      });
      const saved = projectToFile(project);
      expect(saved.version).toBe(5);
      const reopened = projectFromFile(JSON.parse(JSON.stringify(saved)));
      expect(reopened.groups.map(group => group.quote.materialBinding)).toEqual(
        project.groups.map(group => group.quote.materialBinding)
      );
      expect(reopened.groups.map(group => group.quote.parts.map(part => part.id))).toEqual(
        project.groups.map(group => group.quote.parts.map(part => part.id))
      );
      for (const group of reopened.groups.filter(group => group.quote.parts.length)) {
        expect(bounds(group.quote.parts[0].loops[0]).width).toBeCloseTo(20, 10);
        expect(bounds(group.quote.parts[0].loops[0]).height).toBeCloseTo(10, 10);
      }
      const legacyBlank = createBlankQuote();
      delete legacyBlank.geometryProfile;
      expect(projectToFile(createBlankProject(legacyBlank)).version).toBe(4);
    }
  );
});
