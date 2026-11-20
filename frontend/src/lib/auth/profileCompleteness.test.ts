import { describe, expect, it } from 'vitest';
import { isProfileComplete } from './profileCompleteness';

describe('isProfileComplete', () => {
  it('is false with no claims', () => {
    expect(isProfileComplete(null)).toBe(false);
    expect(isProfileComplete(undefined)).toBe(false);
  });

  it('is false when any of the three claims is missing or blank', () => {
    expect(isProfileComplete({})).toBe(false);
    expect(isProfileComplete({ given_name: 'Jane' })).toBe(false);
    expect(isProfileComplete({ given_name: 'Jane', family_name: 'Doe' })).toBe(false);
    expect(isProfileComplete({ given_name: '  ', family_name: 'Doe', birthdate: '1990-01-01' })).toBe(false);
  });

  it('is true once given_name, family_name, and birthdate are all set', () => {
    expect(isProfileComplete({ given_name: 'Jane', family_name: 'Doe', birthdate: '1990-01-01' })).toBe(true);
  });
});
