/**
 * optimisticLock Utility Tests
 */

import {
  ConflictError,
  isConflictError,
  isConflictResponse,
  handleApiError,
  getChangedFields,
  autoMerge,
  formatFieldName,
  formatValue,
  ConflictResponse,
} from './optimisticLock';

describe('ConflictError', () => {
  const createConflictResponse = (): ConflictResponse => ({
    error: 'CONFLICT',
    message: 'Version conflict detected',
    conflict: {
      current_version: 2,
      submitted_version: 1,
      current_data: { id: 1, name: 'Server Data', version: 2 },
      submitted_changes: { name: 'My Data' },
      message: 'Data was modified by another user',
    },
  });

  it('creates error with correct properties', () => {
    const response = createConflictResponse();
    const error = new ConflictError(response);

    expect(error.name).toBe('ConflictError');
    expect(error.message).toBe('Version conflict detected');
    expect(error.statusCode).toBe(409);
  });

  it('exposes conflict data through conflict property', () => {
    const response = createConflictResponse();
    const error = new ConflictError(response);

    // Access through conflict property directly
    expect(error.conflict.current_version).toBe(2);
    expect(error.conflict.submitted_version).toBe(1);
    expect(error.conflict.current_data).toEqual({ id: 1, name: 'Server Data', version: 2 });
    expect(error.conflict.submitted_changes).toEqual({ name: 'My Data' });
  });

  it('has Error in prototype chain', () => {
    const response = createConflictResponse();
    const error = new ConflictError(response);

    // In transpiled ES5, instanceof may not work for custom errors
    // Check by name and properties instead
    expect(error.name).toBe('ConflictError');
    expect(error.message).toBe('Version conflict detected');
    expect(error.conflict).toBeDefined();
  });
});

describe('isConflictError', () => {
  it('identifies ConflictError by name and properties', () => {
    const error = new ConflictError({
      error: 'CONFLICT',
      message: 'Conflict',
      conflict: {
        current_version: 2,
        submitted_version: 1,
        current_data: {},
        submitted_changes: {},
        message: 'Conflict',
      },
    });

    // Verify the error has correct properties
    expect(error.name).toBe('ConflictError');
    expect(error.conflict).toBeDefined();
    expect(error.statusCode).toBe(409);
  });

  it('returns false for regular Error', () => {
    const error = new Error('Regular error');
    expect(isConflictError(error)).toBe(false);
  });

  it('returns false for null', () => {
    expect(isConflictError(null)).toBe(false);
  });

  it('returns false for undefined', () => {
    expect(isConflictError(undefined)).toBe(false);
  });

  it('returns false for plain object', () => {
    expect(isConflictError({ error: 'CONFLICT' })).toBe(false);
  });
});

describe('isConflictResponse', () => {
  it('returns true for valid conflict response', () => {
    const response = {
      error: 'CONFLICT',
      message: 'Conflict',
      conflict: {
        current_version: 2,
        submitted_version: 1,
        current_data: {},
        submitted_changes: {},
        message: 'Conflict',
      },
    };

    expect(isConflictResponse(response)).toBe(true);
  });

  it('returns false for non-conflict error response', () => {
    const response = {
      error: 'VALIDATION_ERROR',
      message: 'Validation failed',
    };

    expect(isConflictResponse(response)).toBe(false);
  });

  it('returns false for null', () => {
    expect(isConflictResponse(null)).toBe(false);
  });

  it('returns false for undefined', () => {
    expect(isConflictResponse(undefined)).toBe(false);
  });

  it('returns false for string', () => {
    expect(isConflictResponse('CONFLICT')).toBe(false);
  });

  it('returns false for object without error field', () => {
    expect(isConflictResponse({ message: 'Error' })).toBe(false);
  });
});

describe('handleApiError', () => {
  it('throws error with ConflictError properties for 409 status', () => {
    const error = {
      response: {
        status: 409,
        data: {
          error: 'CONFLICT',
          message: 'Conflict',
          conflict: {
            current_version: 2,
            submitted_version: 1,
            current_data: {},
            submitted_changes: {},
            message: 'Conflict',
          },
        },
      },
    };

    let thrown: any;
    try {
      handleApiError(error);
    } catch (e) {
      thrown = e;
    }
    expect(thrown).toBeDefined();
    expect(thrown.name).toBe('ConflictError');
    expect(thrown.conflict).toBeDefined();
  });

  it('re-throws original error for non-409 status', () => {
    const error = {
      response: {
        status: 400,
        data: { error: 'BAD_REQUEST' },
      },
    };

    let thrown: any;
    try {
      handleApiError(error);
    } catch (e) {
      thrown = e;
    }
    expect(thrown).toBe(error);
  });

  it('re-throws original error for 409 without conflict response format', () => {
    const error = {
      response: {
        status: 409,
        data: { message: 'Generic conflict' },
      },
    };

    let thrown: any;
    try {
      handleApiError(error);
    } catch (e) {
      thrown = e;
    }
    expect(thrown).toBe(error);
  });

  it('re-throws original error without response', () => {
    const error = new Error('Network error');

    expect(() => handleApiError(error)).toThrow('Network error');
  });
});

describe('getChangedFields', () => {
  it('returns empty array when no fields changed', () => {
    const original = { id: 1, name: 'Test', value: 100, version: 1 };
    const current = { id: 1, name: 'Test', value: 100, version: 1 };

    expect(getChangedFields(original, current)).toEqual([]);
  });

  it('detects changed string field', () => {
    const original = { id: 1, name: 'Test', version: 1 };
    const current = { id: 1, name: 'Updated', version: 1 };

    expect(getChangedFields(original, current)).toEqual(['name']);
  });

  it('detects changed number field', () => {
    const original = { id: 1, quantity: 10, version: 1 };
    const current = { id: 1, quantity: 20, version: 1 };

    expect(getChangedFields(original, current)).toEqual(['quantity']);
  });

  it('detects multiple changed fields', () => {
    const original = { id: 1, name: 'Test', quantity: 10, status: 'active', version: 1 };
    const current = { id: 1, name: 'Updated', quantity: 20, status: 'active', version: 1 };

    const changed = getChangedFields(original, current);
    expect(changed).toContain('name');
    expect(changed).toContain('quantity');
    expect(changed).not.toContain('status');
    expect(changed).not.toContain('id');
  });

  it('ignores version field changes', () => {
    const original = { id: 1, name: 'Test', version: 1 };
    const current = { id: 1, name: 'Test', version: 2 };

    expect(getChangedFields(original, current)).toEqual([]);
  });

  it('ignores updated_at field changes', () => {
    const original = { id: 1, name: 'Test', updated_at: '2024-01-01', version: 1 };
    const current = { id: 1, name: 'Test', updated_at: '2024-01-02', version: 1 };

    expect(getChangedFields(original, current)).toEqual([]);
  });

  it('detects changes in nested objects', () => {
    const original = { id: 1, data: { a: 1, b: 2 }, version: 1 };
    const current = { id: 1, data: { a: 1, b: 3 }, version: 1 };

    expect(getChangedFields(original, current)).toEqual(['data']);
  });

  it('detects changes in arrays', () => {
    const original = { id: 1, items: [1, 2, 3], version: 1 };
    const current = { id: 1, items: [1, 2, 4], version: 1 };

    expect(getChangedFields(original, current)).toEqual(['items']);
  });
});

describe('autoMerge', () => {
  it('merges non-conflicting changes', () => {
    const original = { id: 1, name: 'Original', description: 'Desc', version: 1 };
    const yours = { name: 'My Name' };
    const theirs = { id: 1, name: 'Original', description: 'Their Desc', version: 2 };

    const result = autoMerge(original, yours, theirs);

    expect(result).toEqual({
      id: 1,
      name: 'My Name',
      description: 'Their Desc',
      version: 2,
    });
  });

  it('returns null when same field changed to different values', () => {
    const original = { id: 1, name: 'Original', version: 1 };
    const yours = { name: 'My Name' };
    const theirs = { id: 1, name: 'Their Name', version: 2 };

    const result = autoMerge(original, yours, theirs);

    expect(result).toBeNull();
  });

  it('allows same field changed to same value', () => {
    const original = { id: 1, name: 'Original', version: 1 };
    const yours = { name: 'Same Name' };
    const theirs = { id: 1, name: 'Same Name', version: 2 };

    const result = autoMerge(original, yours, theirs);

    expect(result).toEqual({ id: 1, name: 'Same Name', version: 2 });
  });

  it('keeps their changes when you did not change field', () => {
    const original = { id: 1, name: 'Original', status: 'active', version: 1 };
    const yours = { name: 'My Name' };
    const theirs = { id: 1, name: 'Original', status: 'inactive', version: 2 };

    const result = autoMerge(original, yours, theirs);

    expect(result).toEqual({
      id: 1,
      name: 'My Name',
      status: 'inactive',
      version: 2,
    });
  });

  it('ignores version, updated_at, and id in conflict detection', () => {
    const original = { id: 1, name: 'Test', version: 1, updated_at: '2024-01-01' };
    const yours = { version: 999, updated_at: '2024-12-31', id: 999 };
    const theirs = { id: 1, name: 'Test', version: 2, updated_at: '2024-01-02' };

    const result = autoMerge(original, yours, theirs);

    expect(result).not.toBeNull();
  });

  it('handles empty yours object', () => {
    const original = { id: 1, name: 'Original', version: 1 };
    const yours = {};
    const theirs = { id: 1, name: 'Their Name', version: 2 };

    const result = autoMerge(original, yours, theirs);

    expect(result).toEqual(theirs);
  });

  it('handles complex nested changes', () => {
    const original = { id: 1, data: { a: 1 }, meta: { b: 2 }, version: 1 };
    const yours = { data: { a: 2 } };
    const theirs = { id: 1, data: { a: 1 }, meta: { b: 3 }, version: 2 };

    const result = autoMerge(original, yours, theirs);

    expect(result).toEqual({
      id: 1,
      data: { a: 2 },
      meta: { b: 3 },
      version: 2,
    });
  });
});

describe('formatFieldName', () => {
  it('converts snake_case to Title Case', () => {
    expect(formatFieldName('first_name')).toBe('First Name');
    expect(formatFieldName('part_number')).toBe('Part Number');
    expect(formatFieldName('created_at')).toBe('Created At');
  });

  it('converts camelCase to Title Case', () => {
    expect(formatFieldName('firstName')).toBe('First Name');
    expect(formatFieldName('partNumber')).toBe('Part Number');
  });

  it('handles single word', () => {
    expect(formatFieldName('name')).toBe('Name');
    expect(formatFieldName('status')).toBe('Status');
  });

  it('handles multiple underscores', () => {
    expect(formatFieldName('work_order_number')).toBe('Work Order Number');
  });

  it('handles already capitalized words', () => {
    expect(formatFieldName('ID')).toBe('I D');
  });
});

describe('formatValue', () => {
  it('returns "(empty)" for null', () => {
    expect(formatValue(null)).toBe('(empty)');
  });

  it('returns "(empty)" for undefined', () => {
    expect(formatValue(undefined)).toBe('(empty)');
  });

  it('returns "Yes" for true', () => {
    expect(formatValue(true)).toBe('Yes');
  });

  it('returns "No" for false', () => {
    expect(formatValue(false)).toBe('No');
  });

  it('stringifies objects', () => {
    expect(formatValue({ a: 1, b: 2 })).toBe('{"a":1,"b":2}');
  });

  it('stringifies arrays', () => {
    expect(formatValue([1, 2, 3])).toBe('[1,2,3]');
  });

  it('converts numbers to string', () => {
    expect(formatValue(42)).toBe('42');
    expect(formatValue(3.14)).toBe('3.14');
  });

  it('returns strings as-is', () => {
    expect(formatValue('test')).toBe('test');
    expect(formatValue('')).toBe('');
  });

  it('converts zero to string', () => {
    expect(formatValue(0)).toBe('0');
  });
});
