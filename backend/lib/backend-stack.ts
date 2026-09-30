import * as cdk from 'aws-cdk-lib';
import { Aspects } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { AwsSolutionsChecks, NagSuppressions } from 'cdk-nag';
import { aws_bedrockagentcore as agentcore } from 'aws-cdk-lib';
import { DeployConfig } from './config/deploy-config';
// import * as sqs from 'aws-cdk-lib/aws-sqs';

export interface BackendStackProps extends cdk.StackProps {
  /** Parsed deploy config (`deploy.sh`-generated). Undefined for environment-agnostic `cdk synth`. */
  config?: DeployConfig;
}

export class BackendStack extends cdk.Stack {
  public readonly config?: DeployConfig;

  constructor(scope: Construct, id: string, props?: BackendStackProps) {
    super(scope, id, props);

    this.config = props?.config;

    // ADR: cdk-nag scoped to stack, not app entry point
    // Rationale: Self-contained security checks travel with the stack when used as a template
    // Alternative: Aspects.of(app) in bin/backend.ts (rejected - doesn't travel with stack)
    Aspects.of(this).add(new AwsSolutionsChecks({ verbose: true }));

    cdk.Tags.of(this).add('Project', 'CEI-Life-Sciences-Bot');

    // ---------------------------------------------------------------
    // Code Interpreter (AgentCore)
    // ---------------------------------------------------------------

    // ADR: Code Interpreter Public Network | Rationale: Life sciences agents require internet access to call external database APIs (NCBI, PubChem, etc.) and install Python packages | Alternative: Sandbox mode rejected — no internet blocks API access
    const codeInterpreter = new agentcore.CodeInterpreterCustom(this, 'CodeInterpreter', {
      codeInterpreterCustomName: 'ceiLifeSciencesCI',
      description: 'Code interpreter with internet access for life sciences agent tasks',
      networkConfiguration: agentcore.CodeInterpreterNetworkConfiguration.usingPublicNetwork(),
    });

    // When SessionRunnerLambda and the specialist AgentCore runtimes are added to this stack, call:
    //   codeInterpreter.grantUse(sessionRunnerLambda.role!);
    //   codeInterpreter.grantUse(agentRuntimeRole);
    // These grant: StartCodeInterpreterSession, InvokeCodeInterpreter, StopCodeInterpreterSession
    // Pass CEI_CODE_INTERPRETER_ID as an environment variable to those resources.

    new cdk.CfnOutput(this, 'CodeInterpreterId', {
      value: codeInterpreter.codeInterpreterId,
      exportName: 'CeiCodeInterpreterId',
      description: 'AgentCore Code Interpreter ID — reference as CEI_CODE_INTERPRETER_ID in agent runtimes',
    });

    if (this.config) {
      new cdk.CfnOutput(this, 'WebSearchEnabled', {
        value: String(this.config.webSearchEnabled),
        description: 'Whether the AgentCore Web Search connector is enabled for the deploy Region (B44)',
      });
    }

    // ---------------------------------------------------------------
    // cdk-nag suppression examples — uncomment and adapt when needed
    // Common findings:
    //   AwsSolutions-IAM4 : AWS managed policy used (e.g. AWSLambdaBasicExecutionRole)
    //   AwsSolutions-IAM5 : Wildcard permissions — scope to specific resource ARNs
    //   AwsSolutions-S1   : S3 access logging disabled — enable for sensitive buckets
    //   AwsSolutions-L1   : Lambda runtime not latest — update to python3.12+
    //
    // Stack-level suppression (applies to all resources in this stack):
    // NagSuppressions.addStackSuppressions(this, [
    //   { id: 'AwsSolutions-IAM4', reason: 'ADR: <title> | Rationale: <why> | Alternative: <rejected>' },
    // ]);
    //
    // Resource-level suppression (applies to one specific construct):
    // NagSuppressions.addResourceSuppressions(myResource, [
    //   { id: 'AwsSolutions-IAM4', reason: 'ADR: <title> | Rationale: <why> | Alternative: <rejected>' },
    // ]);
    // ---------------------------------------------------------------
  }
}
