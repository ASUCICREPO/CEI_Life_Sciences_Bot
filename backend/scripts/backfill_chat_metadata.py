"""One-off backfill for ChatMetadataStore rows written before the by_activity / by_title indexes existed.

Legacy rows have no `last_activity_at` / `title_search`, so the sparse GSIs leave them out of the history list and
search. This sets, only where missing:
  - last_activity_at = updated_at   (closest stored proxy for the last message; new turns keep it exact)
  - title_search     = normalize_title(title)

Dry run by default; pass --apply to write. Uses the ambient AWS credentials and CEI_* table settings.

    cd backend/agents && .venv\\Scripts\\python ..\\scripts\\backfill_chat_metadata.py --table <ChatMetadataStore name>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

from shared.persistence.chat_metadata import TITLE_SEARCH_ATTRIBUTE, normalize_title  # noqa: E402


def backfill(table, *, apply: bool) -> tuple[int, int]:
    scanned = changed = 0
    kwargs: dict = {
        "ProjectionExpression": "user_id, session_id, title, updated_at, last_activity_at, #ts",
        "ExpressionAttributeNames": {"#ts": TITLE_SEARCH_ATTRIBUTE},
    }
    while True:
        page = table.scan(**kwargs)
        for item in page.get("Items", []):
            scanned += 1
            sets: dict[str, str] = {}
            if "last_activity_at" not in item and item.get("updated_at"):
                sets["last_activity_at"] = item["updated_at"]
            if TITLE_SEARCH_ATTRIBUTE not in item:
                sets[TITLE_SEARCH_ATTRIBUTE] = normalize_title(item.get("title") or "")
            if not sets:
                continue
            changed += 1
            if not apply:
                continue
            names = {f"#a{i}": k for i, k in enumerate(sets)}
            values = {f":v{i}": v for i, v in enumerate(sets.values())}
            try:
                table.update_item(
                    Key={"user_id": item["user_id"], "session_id": item["session_id"]},
                    UpdateExpression="SET " + ", ".join(f"#a{i} = if_not_exists(#a{i}, :v{i})" for i in range(len(sets))),
                    ConditionExpression="attribute_exists(session_id)",
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues=values,
                )
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
        if "LastEvaluatedKey" not in page:
            return scanned, changed
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--table", required=True, help="ChatMetadataStore table name")
    parser.add_argument("--region", default=None)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = parser.parse_args()
    table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)
    scanned, changed = backfill(table, apply=args.apply)
    verb = "updated" if args.apply else "would update"
    print(f"scanned {scanned} rows; {verb} {changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
