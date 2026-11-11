'use strict';

/**
 * B109/F19 run-start gate: `GetItem` on the UserMetadataTable's (user_id, record="deletion") row,
 * written by `POST /account/delete` (backend/lambdas/account-delete/handler.py). Shared by every
 * Node Lambda that starts a new run or writes user data (Session Runner, UiActions) so the account
 * "deleting" check stays identical across both. The Python equivalent is
 * `shared.persistence.account_gate.AccountGateRepository.is_deleting`
 * (backend/agents/shared/persistence/account_gate.py), used by the Python REST Lambdas.
 */

const RECORD_KEY = 'deletion';

/**
 * @param {import('@aws-sdk/client-dynamodb').DynamoDBClient} db
 * @param {string} tableName - CEI_USER_METADATA_TABLE; a falsy value (flag off, no table wired) never blocks.
 * @param {string} userId - Cognito `sub`, never a caller-supplied value.
 * @param {typeof import('@aws-sdk/client-dynamodb').GetItemCommand} [GetItemCommand] - injected by the
 *   caller's own `productionDependencies()` (which already imports `@aws-sdk/client-dynamodb` -- provided
 *   by the Lambda Node.js runtime, not a repo dependency, same as every other DynamoDB call in this
 *   Lambda); defaults to requiring it here only when not supplied, so a direct unit test of this function
 *   can inject a fake command class instead.
 * @returns {Promise<boolean>}
 */
async function isAccountDeleting(db, tableName, userId, GetItemCommand) {
  if (!tableName) return false;
  const Command = GetItemCommand || require('@aws-sdk/client-dynamodb').GetItemCommand;
  const result = await db.send(new Command({
    TableName: tableName,
    Key: { user_id: { S: userId }, record: { S: RECORD_KEY } },
    ConsistentRead: true,
  }));
  return !!result.Item;
}

module.exports = { isAccountDeleting, RECORD_KEY };
