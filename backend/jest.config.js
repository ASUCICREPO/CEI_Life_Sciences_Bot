module.exports = {
  testEnvironment: 'node',
  roots: ['<rootDir>/test'],
  testMatch: ['**/*.test.ts'],
  // TypeScript builds emit adjacent .js files. Prefer the source so local tests
  // cannot silently exercise an older compiled version after a source edit.
  moduleFileExtensions: ['ts', 'tsx', 'js', 'jsx', 'json', 'node'],
  // Each worker synthesizes full stacks with cdk-nag; cap workers so small CI hosts do not run out of memory.
  maxWorkers: process.env.CI || process.env.CODEBUILD_BUILD_ID ? 1 : '50%',
  transform: {
    // Transpile only: `npm run build` (tsc) type-checks before tests, and full type-checking per worker exhausts the heap.
    '^.+\\.tsx?$': ['ts-jest', { tsconfig: { isolatedModules: true } }]
  }
};
