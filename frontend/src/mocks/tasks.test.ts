import { describe, expect, it } from 'vitest';
import { taskGroupsForMode } from './tasks';

const ids = (mode: 'science' | 'business') =>
  taskGroupsForMode(mode).flatMap((group) => group.tasks.map((task) => task.id)).sort();

describe('taskGroupsForMode', () => {
  it('keeps the same live task ids in both modes', () => {
    expect(ids('science')).toEqual(ids('business'));
  });

  it('gives every task different copy in science and business mode', () => {
    const science = taskGroupsForMode('science').flatMap((group) => group.tasks);
    const business = new Map(taskGroupsForMode('business').flatMap((group) => group.tasks).map((t) => [t.id, t]));
    for (const task of science) {
      const other = business.get(task.id)!;
      expect(other.description).not.toEqual(task.description);
      expect(other.examples).not.toEqual(task.examples);
    }
  });

  it('orders groups by the mode priority', () => {
    expect(taskGroupsForMode('science')[0].id).toBe('live-research-drafting');
    expect(taskGroupsForMode('business')[0].id).toBe('landscape-analysis');
  });
});
