import {
  ActionRequestSchema,
  type ActionRequest,
  type JsonValue,
} from './schema';
import type { AgentEnvelope, AgentName } from '../agentClient';
import { sendAction } from '../agentClient';

export type DispatchCEIUIActionInput = {
  agent: AgentName;
  session_id: string;
  surface_id: string;
  revision: number;
  interaction_id: string;
  component_id: string;
  action_id: string;
  value?: JsonValue;
};

// ADR: dispatch goes through agentClient.sendAction, not a Next /api/ui-actions
// route | the Vite SPA has no server routes, so the browser talks to the
// agent runtime directly (via the dev proxy) using the same op:"resume" shape.
export async function dispatchCEIUIAction(input: DispatchCEIUIActionInput): Promise<AgentEnvelope> {
  const { agent, session_id, ...rest } = input;
  const request: ActionRequest = ActionRequestSchema.parse({
    request_id: crypto.randomUUID(),
    session_id,
    ...rest,
    value: rest.value ?? null,
  });
  return sendAction(agent, request, session_id);
}
