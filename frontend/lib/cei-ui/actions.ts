import {
  ActionRequestSchema,
  ActionResponseSchema,
  type ActionResponse,
  type JsonValue,
} from './schema';
import { actionResponseMatchesRequest } from './action-correlation';

const ACTION_FETCH_TIMEOUT_MS = 15_000;

export type DispatchCEIUIActionInput = {
  session_id: string;
  surface_id: string;
  revision: number;
  interaction_id: string;
  component_id: string;
  action_id: string;
  value?: JsonValue;
};

export async function dispatchCEIUIAction(input: DispatchCEIUIActionInput): Promise<ActionResponse> {
  const request = ActionRequestSchema.parse({
    request_id: crypto.randomUUID(),
    ...input,
    value: input.value ?? null,
  });
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), ACTION_FETCH_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch('/api/ui-actions', {
      method: 'POST',
      headers: { 'content-type': 'application/json', accept: 'application/json' },
      body: JSON.stringify(request),
      signal: controller.signal,
    });
  } catch (error) {
    if (controller.signal.aborted) throw new Error('The action service timed out.');
    throw error;
  } finally {
    clearTimeout(timeout);
  }
  const body: unknown = await response.json().catch(() => null);
  const parsed = ActionResponseSchema.safeParse(body);
  if (!parsed.success) throw new Error('The action service returned an invalid response.');
  if (!actionResponseMatchesRequest(request, parsed.data)) throw new Error('The action response did not match the request.');
  return parsed.data;
}