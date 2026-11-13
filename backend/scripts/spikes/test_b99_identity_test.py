from b99_identity_test import denied, identity


def test_probe_parses_mcp_text_and_structured_content():
    body = {"identity": {"user_id": "a", "session_id": "b"}}
    assert identity({"status": 200, "json": {"result": {"structuredContent": body}}}) == body["identity"]
    assert identity({"status": 200, "json": {"result": {"content": [{"text": '{"identity":{"user_id":"a"}}'}]}}}) == {"user_id": "a"}
    assert identity({"status": 200, "json": {"result": {"isError": True}}}) is None


def test_only_explicit_denials_count_as_denied():
    assert denied({"status": 403, "json": None})
    assert denied({"status": 200, "json": {"error": {"code": -32001}}})
    assert not denied({"status": 200, "json": {"result": {"isError": True}}})
    assert not denied({"status": 500, "json": None})
    assert not denied({"status": 200, "json": {"result": {"content": []}}})
