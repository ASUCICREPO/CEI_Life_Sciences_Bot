#!/usr/bin/env node
/**
 * Region capability + model availability pre-flight check.
 *
 * Usage (run from backend/):
 *   npx ts-node scripts/preflight.ts --config <path> [--write]
 *
 * Exit code 0 = no errors. Exit code 1 = at least one error (printed to stderr).
 * With --write, rewrites the config file's `webSearchEnabled` field in place.
 */
import * as fs from 'fs';
import { SSMClient, GetParameterCommand } from '@aws-sdk/client-ssm';
import {
  BedrockClient,
  GetFoundationModelAvailabilityCommand,
  GetInferenceProfileCommand,
} from '@aws-sdk/client-bedrock';
import { loadDeployConfig, DeployConfig } from '../lib/config/deploy-config';
import {
  evaluateRegion,
  ModelCheckInput,
  SsmServiceProbe,
} from '../lib/config/region-capabilities';

const SSM_SERVICE_IDS: Array<keyof SsmServiceProbe> = ['bedrock-agentcore', 's3vectors', 'bedrock'];
const INFERENCE_PROFILE_PREFIX = /^(us|eu|apac|global)\./;

interface CliArgs {
  configPath: string;
  write: boolean;
}

function parseArgs(argv: string[]): CliArgs {
  let configPath: string | undefined;
  let write = false;

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--config') {
      configPath = argv[i + 1];
      i += 1;
    } else if (arg === '--write') {
      write = true;
    }
  }

  if (!configPath) {
    throw new Error('Usage: npx ts-node scripts/preflight.ts --config <path> [--write]');
  }

  return { configPath, write };
}

/** Probe the public SSM global-infrastructure parameter for one service id in one Region. */
async function probeSsmService(
  client: SSMClient,
  region: string,
  serviceId: string,
): Promise<boolean> {
  try {
    await client.send(
      new GetParameterCommand({
        Name: `/aws/service/global-infrastructure/regions/${region}/services/${serviceId}`,
      }),
    );
    return true;
  } catch (err) {
    if (err instanceof Error && err.name === 'ParameterNotFound') {
      return false;
    }
    throw err;
  }
}

async function probeSsmServices(region: string): Promise<SsmServiceProbe> {
  const client = new SSMClient({ region });
  const results = await Promise.all(SSM_SERVICE_IDS.map((id) => probeSsmService(client, region, id)));
  return {
    'bedrock-agentcore': results[0],
    s3vectors: results[1],
    bedrock: results[2],
  };
}

async function checkModel(
  client: BedrockClient,
  modelId: string,
  role: 'default' | 'router',
): Promise<ModelCheckInput> {
  try {
    const availabilityResponse = await client.send(
      new GetFoundationModelAvailabilityCommand({ modelId }),
    );

    const model: ModelCheckInput = {
      modelId,
      role,
      availability: {
        regionAvailability: availabilityResponse.regionAvailability ?? 'NOT_AVAILABLE',
        authorizationStatus: availabilityResponse.authorizationStatus ?? 'NOT_AUTHORIZED',
        entitlementAvailability: availabilityResponse.entitlementAvailability ?? 'NOT_AVAILABLE',
        agreementStatus: availabilityResponse.agreementAvailability?.status ?? 'ERROR',
      },
    };

    if (INFERENCE_PROFILE_PREFIX.test(modelId)) {
      const profileResponse = await client.send(
        new GetInferenceProfileCommand({ inferenceProfileIdentifier: modelId }),
      );
      model.inferenceProfileStatus = profileResponse.status;
    }

    return model;
  } catch (err) {
    return {
      modelId,
      role,
      error: err instanceof Error ? err.message : String(err),
    };
  }
}

async function checkModels(region: string, config: DeployConfig): Promise<ModelCheckInput[]> {
  const client = new BedrockClient({ region });
  return Promise.all([
    checkModel(client, config.defaultModelId, 'default'),
    checkModel(client, config.routerModelId, 'router'),
  ]);
}

function printReport(config: DeployConfig, ssmServices: SsmServiceProbe, errors: string[], warnings: string[]): void {
  process.stdout.write(`CEI pre-flight report for Region ${config.region}\n`);
  process.stdout.write('-------------------------------------------------\n');
  process.stdout.write(
    `SSM services: bedrock-agentcore=${ssmServices['bedrock-agentcore']} s3vectors=${ssmServices.s3vectors} bedrock=${ssmServices.bedrock}\n`,
  );
  process.stdout.write(`Models checked: ${config.defaultModelId} (default), ${config.routerModelId} (router)\n\n`);

  if (warnings.length > 0) {
    process.stdout.write('Warnings:\n');
    for (const warning of warnings) {
      process.stdout.write(`  - ${warning}\n`);
    }
    process.stdout.write('\n');
  }

  if (errors.length === 0) {
    process.stdout.write('Result: PASS — no capability or model errors.\n');
  } else {
    process.stdout.write(`Result: FAIL — ${errors.length} error(s). See stderr for details.\n`);
    process.stderr.write('Pre-flight errors:\n');
    for (const error of errors) {
      process.stderr.write(`  - ${error}\n`);
    }
  }
}

function writeWebSearchFlag(configPath: string, webSearchEnabled: boolean): void {
  const raw = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
  raw.webSearchEnabled = webSearchEnabled;
  fs.writeFileSync(configPath, `${JSON.stringify(raw, null, 2)}\n`, 'utf-8');
}

async function main(): Promise<number> {
  const { configPath, write } = parseArgs(process.argv.slice(2));
  const config = loadDeployConfig(configPath);

  const [ssmServices, models] = await Promise.all([
    probeSsmServices(config.region),
    checkModels(config.region, config),
  ]);

  const { errors, warnings, webSearchEnabled } = evaluateRegion({
    region: config.region,
    ssmServices,
    models,
  });

  printReport(config, ssmServices, errors, warnings);

  if (write) {
    writeWebSearchFlag(configPath, webSearchEnabled);
    process.stdout.write(`Wrote webSearchEnabled=${webSearchEnabled} to ${configPath}\n`);
  }

  return errors.length > 0 ? 1 : 0;
}

main()
  .then((exitCode) => {
    process.exitCode = exitCode;
  })
  .catch((err) => {
    process.stderr.write(`Pre-flight failed: ${err instanceof Error ? err.message : String(err)}\n`);
    process.exitCode = 1;
  });
