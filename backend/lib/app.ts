import * as cdk from 'aws-cdk-lib';
import { BackendStack } from './backend-stack';
import { EdgeWafStack } from './edge-waf-stack';
import { DeployConfig } from './config/deploy-config';
// FIXME: remove after S2 validation (B92)
import { ToolScopeSpikeStack } from './spikes/tool-scope-spike-stack';
// FIXME: remove after S1 validation (B92)
import { StreamingSpikeStack } from './spikes/streaming-spike-stack';
import { DurableSpikeStack } from './spikes/durable-spike-stack';

export interface BuiltStacks {
  /** Only present when `config` is supplied — the CLOUDFRONT-scope WAF stack (always us-east-1, B88). */
  edgeWaf?: EdgeWafStack;
  backend: BackendStack;
  /** FIXME: remove after S2 validation (B92). Only present when `-c ceiSpikes=tool-scope` is set. */
  toolScopeSpike?: ToolScopeSpikeStack;
  /** FIXME: remove after S1 validation (B92). */
  streamingSpike?: StreamingSpikeStack;
  durableSpike?: DurableSpikeStack;
}

/**
 * Wires `EdgeWafStack` + `BackendStack` exactly as `bin/backend.ts` does. Extracted so tests can exercise
 * the same cross-region wiring (crossRegionReferences, webAclArn passed from EdgeWafStack) the real CDK
 * app entry point uses, instead of re-deriving it in test code.
 */
export function buildStacks(app: cdk.App, config: DeployConfig | undefined, frontendAssetPath: string): BuiltStacks {
  const built = buildCoreStacks(app, config, frontendAssetPath);

  // FIXME: remove after S2 validation (B92)
  // ADR: spike stack only when `-c ceiSpikes` lists it | Rationale: B92 — a normal deploy must never create spike resources; deploy.sh --spike sets the flag | Alternative: always synthesizing it rejected — would deploy a throwaway Gateway with every release
  const spikes = String(app.node.tryGetContext('ceiSpikes') ?? '')
    .split(',')
    .map((id) => id.trim())
    .filter(Boolean);
  if (spikes.includes('tool-scope')) {
    built.toolScopeSpike = new ToolScopeSpikeStack(app, 'ToolScopeSpikeStack', {
      // ADR: Region from deploy config | Rationale: B45 — same env rule as BackendStack; env-agnostic when no config exists yet
      env: config ? { account: process.env.CDK_DEFAULT_ACCOUNT, region: config.region } : undefined,
      policyMode: app.node.tryGetContext('ceiSpikePolicyMode') || undefined,
    });
  }
  // FIXME: remove after S1 validation (B92)
  if (spikes.includes('streaming')) {
    built.streamingSpike = new StreamingSpikeStack(app, 'StreamingSpikeStack', {
      env: config ? { account: process.env.CDK_DEFAULT_ACCOUNT, region: config.region } : undefined,
    });
  }
  if (spikes.includes('durable')) {
    built.durableSpike = new DurableSpikeStack(app, 'DurableSpikeStack', {
      env: config ? { account: process.env.CDK_DEFAULT_ACCOUNT, region: config.region } : undefined,
    });
  }

  return built;
}

function buildCoreStacks(app: cdk.App, config: DeployConfig | undefined, frontendAssetPath: string): BuiltStacks {
  if (config) {
    // ADR: Region from deploy config | Rationale: B45 — Region is configurable via CDK context/env, never hardcoded
    const env = { account: process.env.CDK_DEFAULT_ACCOUNT, region: config.region };

    // ADR: EdgeWafStack always us-east-1 | Rationale: B88 — AWS only accepts a CLOUDFRONT-scope web ACL in us-east-1, regardless of the deploy Region
    const edgeWaf = new EdgeWafStack(app, 'EdgeWafStack', {
      env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: 'us-east-1' },
      crossRegionReferences: true,
      alertTopicRegion: config.region,
    });

    const backend = new BackendStack(app, 'BackendStack', {
      env,
      crossRegionReferences: true,
      config,
      webAclArn: edgeWaf.webAclArn,
      frontendAssetPath,
    });

    return { edgeWaf, backend };
  }

  // ADR: environment-agnostic fallback | Rationale: `cdk synth` must still work before `deploy.sh` has written a config (B46 single environment)
  const backend = new BackendStack(app, 'BackendStack', { frontendAssetPath });
  return { backend };
}
