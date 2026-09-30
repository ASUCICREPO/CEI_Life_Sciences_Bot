import { UserManager, WebStorageStateStore, InMemoryWebStorage } from 'oidc-client-ts';
import type { RuntimeConfig } from './config';

let managerInstance: UserManager | null = null;
let managerConfig: RuntimeConfig | null = null;

export function getUserManager(config: RuntimeConfig): UserManager {
  if (managerInstance && managerConfig === config) {
    return managerInstance;
  }

  managerInstance = new UserManager({
    authority: `https://cognito-idp.${config.region}.amazonaws.com/${config.userPoolId}`,
    client_id: config.userPoolClientId,
    redirect_uri: `${window.location.origin}/auth/callback`,
    post_logout_redirect_uri: `${window.location.origin}/`,
    response_type: 'code',
    scope: 'openid email profile',
    // ADR: access tokens in memory only, never localStorage | reduces XSS token theft blast radius
    userStore: new WebStorageStateStore({ store: new InMemoryWebStorage() }),
    stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
    automaticSilentRenew: true,
    metadataSeed: { revocation_endpoint: `${config.cognitoDomain}/oauth2/revoke` },
  });
  managerConfig = config;

  return managerInstance;
}

// ADR: Cognito has no OIDC end_session_endpoint | hand-build the hosted-UI logout URL
export function buildCognitoLogoutUrl(config: RuntimeConfig): string {
  const origin = window.location.origin;
  const logoutUri = encodeURIComponent(`${origin}/`);
  const clientId = encodeURIComponent(config.userPoolClientId);
  return `${config.cognitoDomain}/logout?client_id=${clientId}&logout_uri=${logoutUri}`;
}
