/** S2-proven target-level permit with an optional scope requirement. */
export function cedarPermit(targetName: string, gatewayArn: string, scope?: string): string {
  const condition = scope ? ` when { principal.hasTag("scope") && principal.getTag("scope") like "*${scope}*" }` : '';
  return `permit(principal is AgentCore::OAuthUser, action in AgentCore::Action::"${targetName}", resource == AgentCore::Gateway::"${gatewayArn}")${condition};`;
}
