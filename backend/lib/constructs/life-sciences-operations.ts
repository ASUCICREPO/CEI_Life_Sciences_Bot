import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { aws_bedrockagentcore as agentcore, aws_iam as iam, aws_lambda as lambda, aws_logs as logs } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { cedarPermit } from './cedar-policy';

const DIRECTORY = path.join(__dirname, '..', '..', 'lambdas', 'lsops');

export function loadLifeSciencesSchemas(): agentcore.CfnGatewayTarget.ToolDefinitionProperty[] {
  return JSON.parse(fs.readFileSync(path.join(DIRECTORY, 'tool-schemas.json'), 'utf8'));
}

export interface LifeSciencesOperationsProps {
  readonly gateway: agentcore.CfnGateway;
  readonly gatewayRole: iam.Role;
  readonly policyEngine: agentcore.CfnPolicyEngine;
  /** A prebuilt image supports isolated tests; deployments use the source image asset. */
  readonly code?: lambda.DockerImageCode;
}

/** A single deterministic LS Ops Lambda target, permitted only for the common scope. */
export class LifeSciencesOperations extends Construct {
  public readonly function: lambda.DockerImageFunction;
  public readonly target: agentcore.CfnGatewayTarget;
  public readonly policy: agentcore.CfnPolicy;

  constructor(scope: Construct, id: string, props: LifeSciencesOperationsProps) {
    super(scope, id);
    const logGroup = new logs.LogGroup(this, 'Logs', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const role = new iam.Role(this, 'ExecutionRole', { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com') });
    logGroup.grantWrite(role);
    this.function = new lambda.DockerImageFunction(this, 'Function', {
      code: props.code ?? lambda.DockerImageCode.fromImageAsset(DIRECTORY),
      role,
      logGroup,
      timeout: cdk.Duration.seconds(30),
      memorySize: 1024,
    });
    const invokeGrant = iam.Grant.addToPrincipal({
      grantee: props.gatewayRole, actions: ['lambda:InvokeFunction'], resourceArns: [this.function.functionArn],
    });
    this.target = new agentcore.CfnGatewayTarget(this, 'Target', {
      name: 'lsops',
      description: 'Deterministic life-sciences sequence, chemistry and lab operations',
      gatewayIdentifier: props.gateway.attrGatewayIdentifier,
      credentialProviderConfigurations: [{ credentialProviderType: 'GATEWAY_IAM_ROLE' }],
      targetConfiguration: {
        mcp: { lambda: { lambdaArn: this.function.functionArn, toolSchema: { inlinePayload: loadLifeSciencesSchemas() } } },
      },
    });
    invokeGrant.applyBefore(this.target);
    this.policy = new agentcore.CfnPolicy(this, 'Permit', {
      name: 'permit_lsops',
      policyEngineId: props.policyEngine.attrPolicyEngineId,
      description: 'Permit deterministic operations only with cei-tools/common',
      definition: { cedar: { statement: cedarPermit('lsops', props.gateway.attrGatewayArn, 'cei-tools/common') } },
      validationMode: 'IGNORE_ALL_FINDINGS',
    });
    this.policy.node.addDependency(this.target);
  }
}
