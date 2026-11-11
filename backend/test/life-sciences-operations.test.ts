import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { aws_bedrockagentcore as agentcore, aws_ecr as ecr, aws_iam as iam, aws_lambda as lambda } from 'aws-cdk-lib';
import { LifeSciencesOperations, loadLifeSciencesSchemas } from '../lib/constructs/life-sciences-operations';

test('all 20 operations have distinct, bounded schema identities', () => {
  const tools = loadLifeSciencesSchemas();
  expect(tools).toHaveLength(20);
  expect(new Set(tools.map((tool) => tool.name)).size).toBe(20);
  expect(tools.map((tool) => tool.name)).toEqual(expect.arrayContaining([
    'validate_sequence', 'transcribe', 'reverse_complement', 'translate', 'orf_finder', 'gc_content',
    'composition', 'protein_properties', 'primer_tm', 'primer_hairpin_dimer', 'restriction_sites',
    'validate_structure', 'molecular_descriptors', 'lipinski_check', 'tanimoto_similarity',
    'molarity', 'dilution', 'unit_convert', 'hgvs_check', 'parse_fasta_genbank',
  ]));
  for (const tool of tools) expect(tool.inputSchema).toMatchObject({ type: 'object' });
});

test('scopes gateway invocation and common Cedar permit to the LS Ops target', () => {
  const stack = new cdk.Stack(new cdk.App(), 'LsOpsTest', { env: { account: '123456789012', region: 'us-east-1' } });
  const gatewayRole = new iam.Role(stack, 'GatewayRole', { assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com') });
  const engine = new agentcore.CfnPolicyEngine(stack, 'Engine', { name: 'lsops_test_engine' });
  const gateway = new agentcore.CfnGateway(stack, 'Gateway', {
    name: 'lsops-test', protocolType: 'MCP', roleArn: gatewayRole.roleArn, authorizerType: 'NONE',
  });
  const repository = ecr.Repository.fromRepositoryName(stack, 'Repository', 'lsops-test');
  new LifeSciencesOperations(stack, 'LifeSciencesOperations', {
    gateway, gatewayRole, policyEngine: engine, code: lambda.DockerImageCode.fromEcr(repository, { tagOrDigest: 'test' }),
  });
  const template = Template.fromStack(stack);
  template.hasResourceProperties('AWS::Lambda::Function', { PackageType: 'Image', Timeout: 30, MemorySize: 1024 });
  template.hasResourceProperties('AWS::BedrockAgentCore::GatewayTarget', Match.objectLike({
    Name: 'lsops', CredentialProviderConfigurations: [{ CredentialProviderType: 'GATEWAY_IAM_ROLE' }],
    TargetConfiguration: { Mcp: { Lambda: { LambdaArn: Match.anyValue(), ToolSchema: { InlinePayload: Match.arrayWith([
      Match.objectLike({ Name: 'molarity' }), Match.objectLike({ Name: 'hgvs_check' }),
    ]) } } } },
  }));
  template.hasResourceProperties('AWS::BedrockAgentCore::Policy', {
    Name: 'permit_lsops',
    PolicyEngineId: Match.anyValue(),
    Description: Match.anyValue(),
    Definition: { Cedar: { Statement: Match.anyValue() } },
    ValidationMode: 'IGNORE_ALL_FINDINGS',
  });
  const policies = template.findResources('AWS::BedrockAgentCore::Policy');
  expect(JSON.stringify(policies)).toContain('cei-tools/common');
  expect(JSON.stringify(policies)).toContain('lsops');
  const iamPolicies = template.findResources('AWS::IAM::Policy');
  expect(JSON.stringify(iamPolicies)).toContain('lambda:InvokeFunction');
  expect(JSON.stringify(iamPolicies)).toContain('LifeSciencesOperationsFunction');
});
