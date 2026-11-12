import {
  ActionRequestSchema,
  type ActionRequest,
  type JsonValue,
} from './schema';
import type { ActionDispatchResult, AgentEnvelope, AgentName } from '../agentClient';
import { sendAction, sendActionExpectingStream } from '../agentClient';

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

function toActionRequest(input: DispatchCEIUIActionInput): ActionRequest {
  const { agent: _agent, session_id, ...rest } = input;
  return ActionRequestSchema.parse({
    request_id: crypto.randomUUID(),
    session_id,
    ...rest,
    value: rest.value ?? null,
  });
}

// ADR: dispatch goes through agentClient.sendAction, not a Next /api/ui-actions
// route | the Vite SPA has no server routes, so the browser talks to the
// agent runtime directly (via the dev proxy) using the same op:"resume" shape.
export async function dispatchCEIUIAction(input: DispatchCEIUIActionInput): Promise<AgentEnvelope> {
  return sendAction(input.agent, toActionRequest(input), input.session_id);
}

/** Like dispatchCEIUIAction, but lets the caller (a drafting chat) accept an SSE response instead of JSON. */
export async function dispatchCEIUIActionExpectingStream(input: DispatchCEIUIActionInput): Promise<ActionDispatchResult> {
  return sendActionExpectingStream(input.agent, toActionRequest(input), input.session_id);
}
