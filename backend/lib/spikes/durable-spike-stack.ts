import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { aws_bedrockagentcore as agentcore, aws_dynamodb as dynamodb, aws_iam as iam } from 'aws-cdk-lib';
import { Aspects, RemovalPolicy } from 'aws-cdk-lib';
import { AwsSolutionsChecks, NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import { LocalUvBundling } from '../constructs/agent-runtimes';

/** Isolated S3/S4 resources. Never attach application users or data. */
export class DurableSpikeStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: cdk.StackProps = {}) {
    super(scope, id, props);
    Aspects.of(this).add(new AwsSolutionsChecks({ verbose: true }));
    cdk.Tags.of(this).add('Project', 'CEI-Life-Sciences-Bot');
    cdk.Tags.of(this).add('Spike', 'S3-S4-durable');
    const memory = new agentcore.CfnMemory(this, 'Memory', {
      name: 'CEIDurableSpikeMemory', eventExpiryDuration: 365,
      description: 'Synthetic checkpoints for the isolated durable workflow spike',
    });
    memory.applyRemovalPolicy(RemovalPolicy.DESTROY);
    const cancellation = new dynamodb.Table(this, 'Cancellation', {
      partitionKey: { name: 'job_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const agentsDir = path.join(__dirname, '..', '..', 'agents');
    const sourceDirs = ['research', 'drafting', 'analysis', 'pipeline_manager', 'tool_agent', 'shared', 'spikes'];
    const extraFiles = ['spike_async_main.py', 'spike_hitl_main.py'];
    const exclusions = ['.env', '.env.*', '.venv', '.pytest_cache', '__pycache__', '*.pyc', 'tests',
      'demo_recordings', 'agentcore', 'code_interpreter_packages'];
    const assetOptions = process.env.JEST_WORKER_ID !== undefined ? { exclude: exclusions } : {
      exclude: exclusions,
      bundling: {
        image: cdk.DockerImage.fromRegistry('ghcr.io/astral-sh/uv:python3.12-bookworm-slim'),
        local: new LocalUvBundling(['spikes'], extraFiles),
        command: ['bash', '-c', [
          'uv export --frozen --no-dev --no-emit-project --no-hashes --output-file /tmp/requirements.txt',
          'uv pip install -r /tmp/requirements.txt --target /asset-output --python-platform aarch64-manylinux_2_28 --python-version 3.12 --only-binary :all:',
          `cp -r ${sourceDirs.join(' ')} ${extraFiles.join(' ')} /asset-output/`,
          'find /asset-output -name __pycache__ -prune -exec rm -rf {} +',
        ].join(' && ')],
      },
    };
    for (const [name, entrypoint] of [['Async', extraFiles[0]], ['Hitl', extraFiles[1]]] as const) {
      const runtime = new agentcore.Runtime(this, `${name}Runtime`, {
        runtimeName: `CEIDurableSpike${name}`,
        description: `Isolated synthetic ${name} workflow proof`,
        agentRuntimeArtifact: agentcore.AgentRuntimeArtifact.fromCodeAsset({
          path: agentsDir, runtime: agentcore.AgentCoreRuntime.PYTHON_3_12, entrypoint: [entrypoint], ...assetOptions,
        }),
        environmentVariables: {
          CEI_SPIKE_MEMORY_ID: memory.attrMemoryId,
          ...(name === 'Async' ? { CEI_SPIKE_CANCEL_TABLE: cancellation.tableName } : {}),
        },
      });
      runtime.role.addToPrincipalPolicy(new iam.PolicyStatement({
        actions: ['bedrock-agentcore:CreateEvent', 'bedrock-agentcore:ListEvents', 'bedrock-agentcore:DeleteEvent'],
        resources: [memory.attrMemoryArn],
      }));
      if (name === 'Async') cancellation.grant(runtime.role, 'dynamodb:GetItem');
      NagSuppressions.addResourceSuppressions(runtime, [{ id: 'AwsSolutions-IAM5',
        reason: 'ADR: Isolated runtime service permissions | Rationale: CDK grants required runtime logging and workload identity actions with wildcard resources | Alternative: removing service permissions rejected because the spike runtime would not start',
      }], true);
      new cdk.CfnOutput(this, `${name}RuntimeArn`, { value: runtime.agentRuntimeArn });
    }
    new cdk.CfnOutput(this, 'MemoryId', { value: memory.attrMemoryId });
    new cdk.CfnOutput(this, 'CancellationTable', { value: cancellation.tableName });
  }
}
