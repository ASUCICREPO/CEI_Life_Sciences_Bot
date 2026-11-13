import type { ActionRequest, ActionResponse, JsonValue } from './schema';

export function actionResponseMatchesRequest(request: ActionRequest, response: ActionResponse): boolean {
  return response.request_id === request.request_id &&
    response.session_id === request.session_id &&
    response.surface_id === request.surface_id &&
    response.revision === request.revision &&
    response.interaction_id === request.interaction_id &&
    response.component_id === request.component_id &&
    response.action_id === request.action_id &&
    jsonValuesEqual(response.value, request.value);
}

function jsonValuesEqual(left: JsonValue, right: JsonValue): boolean {
  if (left === right) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left) && Array.isArray(right) &&
      left.length === right.length && left.every((item, index) => jsonValuesEqual(item, right[index]));
  }
  if (isJsonObject(left) && isJsonObject(right)) {
    const leftKeys = Object.keys(left);
    const rightKeys = Object.keys(right);
    return leftKeys.length === rightKeys.length &&
      leftKeys.every((key) => Object.hasOwn(right, key) && jsonValuesEqual(left[key], right[key]));
  }
  return false;
}

function isJsonObject(value: JsonValue): value is Record<string, JsonValue> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}