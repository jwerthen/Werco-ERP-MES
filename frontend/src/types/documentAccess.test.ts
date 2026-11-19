import {
  accessLevelRaisedNotice,
  documentAccessLevel,
  normalizeDocumentAccessReview,
  serverRaisedAccessLevel,
  type DocumentUploadResult,
} from './documentAccess';

const saved = (access_level?: DocumentUploadResult['access_level'], extra: Partial<DocumentUploadResult> = {}) =>
  ({ id: 1, document_number: 'DOC-1', revision: 'A', title: 'T', access_level, ...extra }) as DocumentUploadResult;

describe('documentAccessLevel', () => {
  it('reads a known level and treats anything else (an API without migration 131) as standard', () => {
    expect(documentAccessLevel({ access_level: 'export_controlled' })).toBe('export_controlled');
    expect(documentAccessLevel({ access_level: 'restricted' })).toBe('restricted');
    expect(documentAccessLevel({})).toBe('standard');
    expect(documentAccessLevel({ access_level: 'secret' })).toBe('standard');
    expect(documentAccessLevel(null)).toBe('standard');
  });
});

describe('serverRaisedAccessLevel', () => {
  it('is true when the saved level is above the requested one, and never for the same or lower', () => {
    expect(serverRaisedAccessLevel('standard', saved('export_controlled'))).toBe(true);
    expect(serverRaisedAccessLevel('restricted', saved('export_controlled'))).toBe(true);
    expect(serverRaisedAccessLevel('restricted', saved('restricted'))).toBe(false);
    expect(serverRaisedAccessLevel('export_controlled', saved('restricted'))).toBe(false);
  });

  it('honors an explicit access_level_raised flag, and reads a missing level as standard', () => {
    expect(
      serverRaisedAccessLevel('export_controlled', saved('export_controlled', { access_level_raised: true }))
    ).toBe(true);
    expect(serverRaisedAccessLevel('standard', saved(undefined))).toBe(false);
  });
});

describe('accessLevelRaisedNotice', () => {
  it("names the server's reasons, and never guesses one it did not give", () => {
    expect(
      accessLevelRaisedNotice(
        'Document uploaded',
        'standard',
        saved('export_controlled', { access_level_raised_because: ['export_controlled_customer'] })
      )
    ).toBe(
      "Document uploaded as Export-controlled, not Standard: the linked part's customer is export-controlled, so the server raised it."
    );
    expect(
      accessLevelRaisedNotice(
        'Revision C saved',
        'standard',
        saved('restricted', { access_level_raised_because: ['predecessor_revision'] })
      )
    ).toBe(
      'Revision C saved as Restricted, not Standard: the previous revision is already at that level, so the server raised it.'
    );
    expect(accessLevelRaisedNotice('Document uploaded', 'standard', saved('restricted'))).toBe(
      'Document uploaded as Restricted, not Standard: the server raised the access level.'
    );
  });
});

describe('normalizeDocumentAccessReview', () => {
  it('reads the API shape: document_id, unlinked_document_count, classification', () => {
    const review = normalizeDocumentAccessReview({
      documents: [
        {
          document_id: 31,
          document_number: 'DWG-31',
          revision: 'A',
          title: 'Fin mount',
          document_type: 'drawing',
          access_level: 'standard',
          part_id: 8,
          part_number: 'ITAR-800',
          customer_id: 4,
          customer_name: 'Orbital',
          classification: 'itar_export_controlled',
        },
      ],
      document_count: 1,
      unlinked_document_count: 3,
    });
    expect(review).toEqual({
      documents: [
        {
          id: 31,
          document_number: 'DWG-31',
          revision: 'A',
          title: 'Fin mount',
          part_id: 8,
          part_number: 'ITAR-800',
          customer_id: 4,
          customer_name: 'Orbital',
          classification: 'itar_export_controlled',
          access_level: 'standard',
        },
      ],
      unlinked_count: 3,
    });
  });

  it('reads the canonical shape, flat or nested part / customer, and drops rows without an id', () => {
    const review = normalizeDocumentAccessReview({
      documents: [
        {
          id: 4,
          document_number: 'DWG-4',
          revision: 'B',
          title: 'Bracket',
          part_number: 'PN-4',
          customer_name: 'Acme Aero',
          access_level: 'restricted',
        },
        {
          id: 5,
          document_number: 'DWG-5',
          title: 'Plate',
          part: { id: 9, part_number: 'PN-9' },
          customer: { id: 2, name: 'Orbital' },
          access_level: 'standard',
        },
        { document_number: 'NO-ID', title: 'dropped' },
      ],
      unlinked_count: 7,
    });
    expect(review.unlinked_count).toBe(7);
    expect(review.documents).toEqual([
      expect.objectContaining({ id: 4, part_number: 'PN-4', customer_name: 'Acme Aero', access_level: 'restricted' }),
      expect.objectContaining({
        id: 5,
        part_id: 9,
        part_number: 'PN-9',
        customer_id: 2,
        customer_name: 'Orbital',
        access_level: 'standard',
        revision: null,
      }),
    ]);
  });

  it('survives an empty or malformed body', () => {
    expect(normalizeDocumentAccessReview(null)).toEqual({ documents: [], unlinked_count: 0 });
    expect(normalizeDocumentAccessReview({ documents: 'x', unlinked_count: -1 })).toEqual({
      documents: [],
      unlinked_count: 0,
    });
  });
});
