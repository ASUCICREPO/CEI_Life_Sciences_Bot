import { describe, expect, it } from 'vitest';
import { displayFirstName, displayFullName, timeOfDayGreeting } from './displayName';

describe('displayFirstName', () => {
  it('prefers given_name', () => {
    expect(displayFirstName({ given_name: 'Priya', name: 'Someone Else', email: 'x@y.com' })).toBe('Priya');
  });

  it('uses the first word of name', () => {
    expect(displayFirstName({ name: 'Alex Morgan', email: 'x@y.com' })).toBe('Alex');
  });

  it('falls back to the email local part', () => {
    expect(displayFirstName({ email: 'jane.doe@example.com' })).toBe('Jane');
    expect(displayFirstName({ email: 'ROB_smith42@example.com' })).toBe('Rob');
  });

  it('returns null with no usable claim', () => {
    expect(displayFirstName(null)).toBeNull();
    expect(displayFirstName({ email: '123@example.com' })).toBeNull();
  });
});

describe('displayFullName', () => {
  it('joins given_name and family_name once the onboarding profile is set', () => {
    expect(displayFullName({ given_name: 'Jane', family_name: 'Doe', email: 'x@y.com' })).toBe('Jane Doe');
  });

  it('falls back to displayFirstName when family_name is absent', () => {
    expect(displayFullName({ given_name: 'Jane', email: 'x@y.com' })).toBe('Jane');
    expect(displayFullName({ email: 'jane.doe@example.com' })).toBe('Jane');
  });
});

describe('timeOfDayGreeting', () => {
  it('follows the local hour', () => {
    expect(timeOfDayGreeting(new Date(2026, 0, 1, 8))).toBe('Good morning');
    expect(timeOfDayGreeting(new Date(2026, 0, 1, 14))).toBe('Good afternoon');
    expect(timeOfDayGreeting(new Date(2026, 0, 1, 21))).toBe('Good evening');
  });
});
