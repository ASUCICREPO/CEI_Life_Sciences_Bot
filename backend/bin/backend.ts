#!/usr/bin/env node
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { buildStacks } from '../lib/app';
import { resolveConfigPath, tryLoadDeployConfig } from '../lib/config/deploy-config';

const app = new cdk.App();

const configPath = resolveConfigPath(app);
const config = tryLoadDeployConfig(configPath);

// ADR: frontend/dist required for every synth (config or not) | Rationale: B2 — BucketDeployment needs a real asset directory at synth time; failing fast here beats a cryptic CDK asset-staging error
const frontendAssetPath = path.resolve(__dirname, '..', '..', 'frontend', 'dist');
if (!fs.existsSync(frontendAssetPath)) {
  throw new Error(
    `Frontend build not found at ${frontendAssetPath}. Run: cd frontend && npm ci && npm run build`,
  );
}

if (!config) {
  // ADR: environment-agnostic fallback | Rationale: `cdk synth` must still work before `deploy.sh` has written a config (B46 single environment)
  process.stderr.write(
    `Warning: no deploy config found at ${configPath}. Synthesizing an environment-agnostic stack (run deploy.sh first).\n`,
  );
}

buildStacks(app, config, frontendAssetPath);
