#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { BackendStack } from '../lib/backend-stack';
import { resolveConfigPath, tryLoadDeployConfig } from '../lib/config/deploy-config';

const app = new cdk.App();

const configPath = resolveConfigPath(app);
const config = tryLoadDeployConfig(configPath);

if (config) {
  // ADR: Region from deploy config | Rationale: B45 — Region is configurable via CDK context/env, never hardcoded
  new BackendStack(app, 'BackendStack', {
    env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: config.region },
    config,
  });
} else {
  // ADR: environment-agnostic fallback | Rationale: `cdk synth` must still work before `deploy.sh` has written a config (B46 single environment)
  process.stderr.write(
    `Warning: no deploy config found at ${configPath}. Synthesizing an environment-agnostic stack (run deploy.sh first).\n`,
  );
  new BackendStack(app, 'BackendStack', {});
}
