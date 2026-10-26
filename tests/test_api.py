import pytest
from fastapi.testclient import TestClient

from main import create_app


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path / "requests.json"))


def test_full_flow_over_http(client):
    resp = client.post("/api/requests", json={"user": "dave", "title": "伺服器", "amount_total": 50_000})
    assert resp.status_code == 201
    rid = resp.json()["record"]["id"]

    assert client.get("/api/requests", params={"user": "alice"}).json()[0]["my_turn"] is True
    assert client.get("/api/requests", params={"user": "carol"}).json()[0]["my_turn"] is False

    client.post(f"/api/requests/{rid}/approve", json={"user": "alice"})
    client.post(f"/api/requests/{rid}/approve", json={"user": "carol", "node_id": "cfo_approve"})
    detail = client.post(f"/api/requests/{rid}/approve", json={"user": "erin"}).json()

    assert detail["status"] == "done"
    assert [h["action"] for h in detail["history"]] == ["送出", "核准", "核准", "核准"]


def test_error_status_codes(client):
    rid = client.post("/api/requests", json={"user": "dave", "amount_total": 5_000}).json()["record"]["id"]

    assert client.post(f"/api/requests/{rid}/approve", json={"user": "bob"}).status_code == 403
    assert client.post(f"/api/requests/{rid}/approve", json={"user": "nobody"}).status_code == 422
    assert client.get("/api/requests/PR-999", params={"user": "dave"}).status_code == 404

    assert client.post(f"/api/requests/{rid}/resubmit", json={"user": "dave", "title": "筆電", "amount_total": 1}).status_code == 409
    client.post(f"/api/requests/{rid}/withdraw", json={"user": "dave"})
    assert client.post(f"/api/requests/{rid}/approve", json={"user": "alice"}).status_code == 409


def test_reject_resubmit_withdraw_over_http(client):
    rid = client.post("/api/requests", json={"user": "dave", "title": "筆電", "amount_total": 8_000}).json()["record"]["id"]
    detail = client.post(f"/api/requests/{rid}/reject", json={"user": "alice", "comment": "太貴"}).json()
    assert detail["pending"][0]["stage"] == "applicant"
    assert client.get("/api/requests", params={"user": "dave"}).json()[0]["my_turn"] is True

    assert client.post(f"/api/requests/{rid}/resubmit", json={"user": "alice", "title": "筆電", "amount_total": 6_000}).status_code == 403
    assert client.post(f"/api/requests/{rid}/resubmit", json={"user": "dave", "title": "", "amount_total": 6_000}).status_code == 422
    detail = client.post(f"/api/requests/{rid}/resubmit", json={"user": "dave", "title": "二手筆電", "amount_total": 6_000}).json()
    assert detail["record"]["amount_total"] == 6_000
    assert [p["node_id"] for p in detail["pending"]] == ["manager_approve"]

    assert client.post(f"/api/requests/{rid}/withdraw", json={"user": "alice"}).status_code == 403
    detail = client.post(f"/api/requests/{rid}/withdraw", json={"user": "dave", "comment": "不買了"}).json()
    assert detail["status"] == "cancelled"
    assert [h["action"] for h in detail["history"]] == ["送出", "退回", "重新送出", "撤回"]


def test_applicant_cannot_sign_own_request_over_http(client):
    rid = client.post("/api/requests", json={"user": "alice", "amount_total": 5_000}).json()["record"]["id"]
    resp = client.post(f"/api/requests/{rid}/approve", json={"user": "alice"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "alice 是申請人，不能簽核自己送出的單據"
    assert client.post(f"/api/requests/{rid}/approve", json={"user": "carol"}).json()["status"] == "done"


def test_auto_approve_over_http(client):
    rid = client.post("/api/requests", json={"user": "erin", "amount_total": 50_000}).json()["record"]["id"]
    detail = client.post(f"/api/requests/{rid}/approve", json={"user": "carol"}).json()
    assert detail["status"] == "done"
    assert [h["action"] for h in detail["history"]] == ["送出", "核准", "自動核准", "自動核准"]


def test_user_without_manager_cannot_create_over_http(client):
    resp = client.post("/api/requests", json={"user": "frank", "amount_total": 5_000})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "frank 沒有設定直屬主管，不能送出申請"


def test_users_include_manager_and_nodes(client):
    users = {u["id"]: u for u in client.get("/api/users").json()}
    assert users["dave"]["manager"] == "alice" and users["dave"]["nodes"] == []
    assert users["alice"]["manager"] == "carol" and users["alice"]["nodes"] == ["manager_approve"]
    assert users["carol"]["manager"] == "frank" and users["carol"]["nodes"] == ["cfo_approve", "manager_approve"]
    assert users["frank"]["manager"] is None and users["frank"]["nodes"] == ["manager_approve"]
    assert users["erin"]["nodes"] == ["legal_approve"]


def test_bpmn_and_raw_json(client):
    assert "purchase_approval" in client.get("/api/bpmn").text
    rid = client.post("/api/requests", json={"user": "dave"}).json()["record"]["id"]
    raw = client.get(f"/api/requests/{rid}/workflow-json").json()
    assert "spec" in raw and "tasks" in raw


def test_clear(client):
    client.post("/api/requests", json={"user": "dave"})
    assert client.delete("/api/requests").status_code == 204
    assert client.get("/api/requests", params={"user": "dave"}).json() == []


def test_openapi_info_matches_page(client):
    # API 文件的標題、說明要和操作頁一致，兩邊改文案要一起改
    page = client.get("/").text
    info = client.get("/openapi.json").json()["info"]
    assert f"<title>{info['title']}</title>" in page
    assert f'<div class="sub">{info["description"]}' in page
