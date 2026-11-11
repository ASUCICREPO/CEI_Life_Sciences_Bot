// Per-file cache for expensive CDK stack builds and synths.
//
// A full BackendStack synth (with cdk-nag) takes many seconds. Many tests in one file build the
// same stack with the same props and only read the result. They can share one build.
//
// Jest gives each test file its own module registry, so this cache never crosses test files.
//
// Rules for callers:
// - Use one key for one set of props. Build the key from every input that changes the stack.
// - Share a build only when the test reads it. A test that adds constructs, adds Aspects,
//   changes context, or mutates template JSON after the build must build its own stack.

const cache = new Map<string, unknown>();

export function memoSynth<T>(key: string, build: () => T): T {
  if (!cache.has(key)) {
    cache.set(key, build());
  }
  return cache.get(key) as T;
}
