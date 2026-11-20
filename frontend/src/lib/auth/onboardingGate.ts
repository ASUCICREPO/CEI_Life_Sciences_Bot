export type OnboardingRequiredHandler = () => void;

let handler: OnboardingRequiredHandler | null = null;

// ADR: a swappable callback instead of a React context | non-React modules (agentClient, sessionsClient,
// openStream) hit a run-starting 403 onboarding_required and need to redirect without importing
// react-router-dom themselves; AuthProvider registers the real `navigate('/onboarding')` call.
export function setOnboardingRequiredHandler(next: OnboardingRequiredHandler | null): void {
  handler = next;
}

export function notifyOnboardingRequired(): void {
  handler?.();
}
