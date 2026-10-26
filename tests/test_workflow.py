import json
import os
import re
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest
from SpiffWorkflow.bpmn.workflow import BpmnWorkflow

import workflow
from store import JsonStore, RequestNotFound
from workflow import (
    APPROVERS, ApprovalService, PurchaseRequest, RecordScriptEngine, RecordScriptEnvironment, WorkflowClosed,
    WrongStage,
)


@pytest.fixture
def service(tmp_path):
    return ApprovalService(JsonStore(tmp_path / "requests.json"))


def pending(service, request_id):
    return {p["node_id"] for p in service.detail(request_id, "dave")["pending"]}


def test_small_amount_only_needs_manager(service):
    rid = service.create("dave", "筆電", 5_000)
    assert pending(service, rid) == {"manager_approve"}

    service.approve(rid, "alice")

    detail = service.detail(rid, "dave")
    assert detail["status"] == "done"
    assert detail["record"]["state"] == "approved"
    assert detail["nodes"]["write_back"] == "COMPLETED"
    assert "cfo_approve" not in detail["nodes"]
    assert detail["task_data"]["approval_result"] == "approved"


def test_predicted_states_before_gateway(service):
    rid = service.create("dave", "伺服器", 50_000)
    nodes = service.detail(rid, "dave")["nodes"]
    # Gateway 還沒算條件：預設線 LIKELY、條件線 MAYBE，跟金額無關
    assert nodes["amount_gateway"] == "FUTURE"
    assert nodes["write_back"] == "LIKELY"
    assert nodes["cfo_approve"] == "MAYBE"


def test_big_amount_needs_countersign(service):
    rid = service.create("dave", "伺服器", 50_000)
    service.approve(rid, "alice")
    assert pending(service, rid) == {"cfo_approve", "legal_approve"}

    service.approve(rid, "erin")
    detail = service.detail(rid, "dave")
    assert detail["status"] == "running"
    assert detail["nodes"]["countersign_join"] == "WAITING"
    assert pending(service, rid) == {"cfo_approve"}

    service.approve(rid, "carol")
    detail = service.detail(rid, "dave")
    assert detail["status"] == "done"
    assert detail["record"]["state"] == "approved"
    assert ["countersign_join", "write_back"] in detail["taken"]
    assert ["amount_gateway", "write_back"] not in detail["taken"]


def test_user_without_permission_cannot_approve(service):
    rid = service.create("dave", "筆電", 5_000)
    with pytest.raises(PermissionError):
        service.approve(rid, "bob")
    with pytest.raises(PermissionError):
        service.approve(rid, "carol")  # 還沒輪到財務長


def test_countersign_reject_goes_back_to_applicant(service):
    rid = service.create("dave", "伺服器", 50_000)
    service.approve(rid, "alice")
    service.approve(rid, "erin")

    service.reject(rid, "carol", "請補報價單")

    detail = service.detail(rid, "dave")
    assert detail["status"] == "running"
    assert detail["record"]["state"] == "returned"
    assert pending(service, rid) == {"applicant_revise"}
    assert detail["history"][-1]["result"] == "退回到「申請人填寫」"
    # 重新送出後從主管開始，erin 已經簽過的會簽也被清掉，兩個人都要重簽
    service.resubmit(rid, "dave", "伺服器", 50_000)
    assert pending(service, rid) == {"manager_approve"}
    service.approve(rid, "alice")
    assert pending(service, rid) == {"cfo_approve", "legal_approve"}


def test_first_level_reject_goes_back_to_applicant(service):
    rid = service.create("dave", "筆電", 8_000)
    assert service.detail(rid, "alice")["pending"][0]["reject_to"] == "申請人填寫"

    service.reject(rid, "alice", "太貴")

    assert service.detail(rid, "dave")["pending"] == [{
        "node_id": "applicant_revise", "node_name": "申請人填寫", "stage": "applicant",
        "approvers": ["dave"], "can_act": True, "reject_to": None,
    }]
    assert service.detail(rid, "alice")["pending"][0]["can_act"] is False
    assert service.list_requests("dave")[0]["my_turn"] is True
    assert service.list_requests("alice")[0]["my_turn"] is False
    # 單據在申請人手上時，主管沒有東西可以簽
    with pytest.raises(PermissionError, match="alice 沒有可以簽的關卡"):
        service.approve(rid, "alice")


def test_resubmit_updates_record_and_restarts_from_manager(service):
    rid = service.create("dave", "筆電", 8_000)
    service.reject(rid, "alice", "太貴")

    service.resubmit(rid, "dave", "二手筆電", 6_000, "換便宜的")

    detail = service.detail(rid, "dave")
    assert detail["record"]["title"] == "二手筆電"
    assert detail["record"]["amount_total"] == 6_000
    assert detail["record"]["state"] == "to_approve"
    assert pending(service, rid) == {"manager_approve"}
    assert history(service, rid)[-1] == entry("dave", "重新送出", "申請人填寫", "換便宜的")
    service.approve(rid, "alice")
    assert service.detail(rid, "dave")["status"] == "done"
    assert service.detail(rid, "dave")["task_data"]["applicant_revise_by"] == "dave"


def test_resubmit_can_change_amount_and_route(service):
    # 退回後把金額改到門檻以上，重新送出會走到會簽
    rid = service.create("dave", "筆電", 8_000)
    service.reject(rid, "alice")
    service.resubmit(rid, "dave", "工作站", 50_000)
    service.approve(rid, "alice")
    assert pending(service, rid) == {"cfo_approve", "legal_approve"}


def test_resubmit_only_by_applicant_and_only_when_returned(service):
    rid = service.create("dave", "筆電", 8_000)
    with pytest.raises(WrongStage, match=f"{rid} 不在申請人手上，不能重新送出"):
        service.resubmit(rid, "dave", "筆電", 8_000)
    service.reject(rid, "alice")
    with pytest.raises(PermissionError, match="alice 不是申請人，不能重新送出"):
        service.resubmit(rid, "alice", "筆電", 8_000)
    assert service.detail(rid, "dave")["record"]["title"] == "筆電"


def test_withdraw_cancels_whole_request(service):
    rid = service.create("dave", "筆電", 8_000)
    service.reject(rid, "alice", "不需要買")

    service.withdraw(rid, "dave", "那就不買了")

    detail = service.detail(rid, "dave")
    assert detail["status"] == "cancelled"
    assert detail["record"]["state"] == "cancelled"
    assert detail["pending"] == []
    assert detail["nodes"]["applicant_revise"] == "CANCELLED"
    assert history(service, rid)[-1] == entry("dave", "撤回", "申請人填寫", "那就不買了", "整張作廢")
    with pytest.raises(WorkflowClosed):
        service.resubmit(rid, "dave", "筆電", 8_000)


def test_withdraw_allowed_while_approving(service):
    # 還在會簽中也可以撤回，紀錄會寫下當時停在哪幾關
    rid = service.create("dave", "伺服器", 50_000)
    service.approve(rid, "alice")
    with pytest.raises(PermissionError, match="carol 不是申請人，不能撤回"):
        service.withdraw(rid, "carol")
    service.withdraw(rid, "dave")
    assert service.detail(rid, "dave")["status"] == "cancelled"
    assert history(service, rid)[-1] == entry("dave", "撤回", "財務長審核、法務審核", "", "整張作廢")


def test_state_survives_reload_from_json(tmp_path):
    path = tmp_path / "requests.json"
    rid = ApprovalService(JsonStore(path)).create("dave", "伺服器", 50_000)
    ApprovalService(JsonStore(path)).approve(rid, "alice")

    # 換一個全新的 service 讀同一個檔，流程接著跑
    service = ApprovalService(JsonStore(path))
    assert pending(service, rid) == {"cfo_approve", "legal_approve"}


def test_bpmn_change_only_affects_new_requests(tmp_path):
    # 流程定義跟著單據一起序列化，改 .bpmn 後舊單還是照舊的 Gateway 條件走
    path = tmp_path / "requests.json"
    old_rid = ApprovalService(JsonStore(path)).create("dave", "伺服器", 50_000)
    changed = tmp_path / "changed.bpmn"
    changed.write_text(workflow.BPMN_PATH.read_text(encoding="utf-8").replace(
        "self.amount_total &gt; 10000", "self.amount_total &gt; 100000"), encoding="utf-8")

    service = ApprovalService(JsonStore(path), bpmn_path=changed)
    new_rid = service.create("dave", "伺服器", 50_000)
    service.approve(old_rid, "alice")
    service.approve(new_rid, "alice")

    assert pending(service, old_rid) == {"cfo_approve", "legal_approve"}
    assert service.detail(new_rid, "dave")["status"] == "done"


def test_concurrent_countersign_keeps_both_approvals(service):
    rid = service.create("dave", "伺服器", 50_000)
    service.approve(rid, "alice")

    threads = [threading.Thread(target=service.approve, args=(rid, u)) for u in ("carol", "erin")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert service.detail(rid, "dave")["status"] == "done"


def test_unknown_request(service):
    with pytest.raises(RequestNotFound):
        service.detail("PR-999", "dave")


# ---------------------------------------------------------------------------
# 回傳資料的欄位與內容
# ---------------------------------------------------------------------------
TIME_FORMAT = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def history(service, request_id):
    """簽核紀錄，檢查完時間格式後拿掉 time 方便比對"""
    rows = service.detail(request_id, "dave")["history"]
    for row in rows:
        assert TIME_FORMAT.match(row.pop("time"))
    return rows


def entry(user, action, node, comment="", result=""):
    return {"user": user, "action": action, "node": node, "comment": comment, "result": result}


def test_create_stores_record_and_logs_submit(service):
    rid = service.create("dave", "筆電", 5_000)
    detail = service.detail(rid, "dave")
    assert detail["record"] == {
        "id": rid, "title": "筆電", "amount_total": 5_000, "applicant": "dave", "state": "to_approve",
        "manager": "alice",
    }
    assert TIME_FORMAT.match(detail["created_at"])
    assert history(service, rid) == [entry("dave", "送出", "申請人填寫")]


def test_approve_logs_and_writes_approver_into_task_data(service):
    rid = service.create("dave", "筆電", 5_000)
    service.approve(rid, "alice", "OK")
    assert history(service, rid)[1:] == [entry("alice", "核准", "直屬主管審核", "OK")]
    assert service.detail(rid, "dave")["task_data"]["manager_approve_by"] == "alice"


def test_approve_without_comment_logs_empty_comment(service):
    rid = service.create("dave", "筆電", 5_000)
    service.approve(rid, "alice")
    assert history(service, rid)[-1] == entry("alice", "核准", "直屬主管審核")


def test_reject_logs_full_entry(service):
    rid = service.create("dave", "筆電", 8_000)
    service.reject(rid, "alice", "不需要買")
    assert history(service, rid)[-1] == entry("alice", "退回", "直屬主管審核", "不需要買", "退回到「申請人填寫」")


def test_countersign_reject_logs_full_entry(service):
    rid = service.create("dave", "伺服器", 50_000)
    service.approve(rid, "alice")
    service.reject(rid, "carol", "請補報價單")
    assert history(service, rid)[-1] == entry("carol", "退回", "財務長審核", "請補報價單", "退回到「申請人填寫」")


def test_closed_and_forbidden_messages(service):
    rid = service.create("dave", "筆電", 5_000)
    with pytest.raises(PermissionError, match="bob 沒有可以簽的關卡"):
        service.approve(rid, "bob")
    service.withdraw(rid, "dave")
    with pytest.raises(WorkflowClosed, match=f"{rid} 狀態為 cancelled"):
        service.reject(rid, "alice")


def test_detail_pending_fields(service):
    rid = service.create("dave", "伺服器", 50_000)
    assert service.detail(rid, "alice")["pending"] == [{
        "node_id": "manager_approve", "node_name": "直屬主管審核", "stage": "approval",
        "approvers": ["alice"], "can_act": True, "reject_to": "申請人填寫",
    }]
    service.approve(rid, "alice")
    pending_rows = service.detail(rid, "dave")["pending"]
    assert [p["can_act"] for p in pending_rows] == [False, False]
    assert {p["node_id"]: p["reject_to"] for p in pending_rows} == {
        "cfo_approve": "申請人填寫", "legal_approve": "申請人填寫",
    }


def test_detail_task_data_only_when_done(service):
    rid = service.create("dave", "筆電", 5_000)
    assert service.detail(rid, "dave")["task_data"] is None
    service.reject(rid, "alice")
    # 作廢的 workflow 也算 completed，但不能把 task data 當成結案結果
    assert service.detail(rid, "dave")["task_data"] is None


def test_detail_tree_and_json_size(service):
    rid = service.create("dave", "筆電", 5_000)
    detail = service.detail(rid, "dave")
    assert detail["tree"][0] == {
        "node_id": "start", "node_name": "開始", "type": "StartEvent", "state": "COMPLETED", "depth": 0,
    }
    assert detail["tree"][1]["depth"] == 1
    assert detail["json_size"] == len(json.dumps(service.workflow_json(rid)))


def test_node_state_keeps_most_active_copy(service):
    rid = service.create("dave", "伺服器", 50_000)
    service.approve(rid, "alice")
    # 財務長先簽：財務長分支那份「會簽合併」是 WAITING，法務分支那份還是 FUTURE
    service.approve(rid, "carol")
    assert service.detail(rid, "dave")["nodes"]["countersign_join"] == "WAITING"


def test_list_requests_fields(service):
    rid = service.create("dave", "伺服器", 50_000)
    assert service.list_requests("alice") == [{
        "id": rid, "title": "伺服器", "amount_total": 50_000, "applicant": "dave", "state": "to_approve",
        "manager": "alice", "status": "running", "pending": ["直屬主管審核"], "my_turn": True,
    }]


# ---------------------------------------------------------------------------
# 簽核人：第一關看申請人的直屬主管，申請人不能簽自己的單，同一輪簽過的人自動核准
# ---------------------------------------------------------------------------
def test_manager_stage_goes_to_applicants_manager(service):
    # alice 自己送單，第一關不是 alice 而是她的主管 carol
    rid = service.create("alice", "螢幕", 5_000)
    assert service.detail(rid, "carol")["pending"] == [{
        "node_id": "manager_approve", "node_name": "直屬主管審核", "stage": "approval",
        "approvers": ["carol"], "can_act": True, "reject_to": "申請人填寫",
    }]
    assert service.list_requests("alice")[0]["my_turn"] is False
    assert service.list_requests("carol")[0]["my_turn"] is True
    service.approve(rid, "carol")
    assert service.detail(rid, "alice")["status"] == "done"


def test_applicant_cannot_approve_own_request(service):
    rid = service.create("alice", "螢幕", 5_000)
    with pytest.raises(PermissionError, match="alice 是申請人，不能簽核自己送出的單據"):
        service.approve(rid, "alice")
    with pytest.raises(PermissionError, match="alice 是申請人"):
        service.reject(rid, "alice")
    assert history(service, rid) == [entry("alice", "送出", "申請人填寫")]


def test_fixed_stage_held_only_by_applicant_goes_to_manager():
    # 法務只有 erin 一個人：erin 自己送的單改由 erin 的主管 carol 簽，不然這一關永遠沒人能簽
    by_erin = PurchaseRequest("PR-001", "伺服器", 50_000, applicant="erin", manager="carol")
    by_dave = PurchaseRequest("PR-002", "伺服器", 50_000, applicant="dave", manager="alice")
    assert ApprovalService.approvers("legal_approve", by_erin) == {"carol"}
    assert ApprovalService.approvers("legal_approve", by_dave) == {"erin"}
    # 舊版存檔沒有 manager，就沒有人可以代簽
    by_erin.manager = None
    assert ApprovalService.approvers("legal_approve", by_erin) == set()


def test_same_approver_is_auto_approved_in_same_round(service):
    # alice 的主管 carol 也是財務長：主管那關核准後，財務長審核自動核准，只剩法務
    rid = service.create("alice", "伺服器", 50_000)
    service.approve(rid, "carol", "OK")
    assert pending(service, rid) == {"legal_approve"}
    assert history(service, rid)[1:] == [
        entry("carol", "核准", "直屬主管審核", "OK"),
        entry("carol", "自動核准", "財務長審核", "", "已在「直屬主管審核」核准過"),
    ]
    service.approve(rid, "erin")
    detail = service.detail(rid, "alice")
    assert detail["status"] == "done"
    assert detail["task_data"]["cfo_approve_by"] == "carol"


def test_applicant_holding_fixed_stage_needs_one_approval(service):
    # erin 送大額單：carol 簽一次，財務長、法務（代 erin）兩關都自動核准
    rid = service.create("erin", "伺服器", 50_000)
    service.approve(rid, "carol")
    assert service.detail(rid, "erin")["status"] == "done"
    assert [(h["action"], h["node"]) for h in history(service, rid)[1:]] == [
        ("核准", "直屬主管審核"), ("自動核准", "財務長審核"), ("自動核准", "法務審核"),
    ]


def test_auto_approve_starts_over_after_return(service):
    # 退回後重新送出是新的一輪，之前的核准不算，carol 要重新簽主管那關
    rid = service.create("alice", "伺服器", 50_000)
    service.approve(rid, "carol")
    service.reject(rid, "erin", "補文件")
    service.resubmit(rid, "alice", "伺服器", 50_000)
    assert pending(service, rid) == {"manager_approve"}
    service.approve(rid, "carol")
    assert pending(service, rid) == {"legal_approve"}
    assert [h["action"] for h in history(service, rid)].count("自動核准") == 2


def test_user_without_manager_cannot_create(service):
    with pytest.raises(PermissionError, match="frank 沒有設定直屬主管，不能送出申請"):
        service.create("frank", "伺服器", 50_000)
    assert service.list_requests("frank") == []


def test_cfo_request_goes_to_ceo(service):
    # carol 自己送大額單：主管那關是 frank，財務長那關只有 carol 自己，改由 frank 簽並自動核准
    rid = service.create("carol", "伺服器", 50_000)
    assert service.detail(rid, "frank")["pending"][0]["approvers"] == ["frank"]
    service.approve(rid, "frank")
    assert pending(service, rid) == {"legal_approve"}
    assert history(service, rid)[-1] == entry("frank", "自動核准", "財務長審核", "", "已在「直屬主管審核」核准過")
    service.approve(rid, "erin")
    assert service.detail(rid, "carol")["status"] == "done"


def test_manager_is_snapshotted_at_create(service, monkeypatch):
    # 送出後才改組織表，已送出的單仍然由原本的主管簽
    rid = service.create("dave", "筆電", 5_000)
    monkeypatch.setitem(workflow.MANAGER_OF, "dave", "carol")
    assert service.detail(rid, "alice")["pending"][0]["approvers"] == ["alice"]
    assert service.detail(rid, "carol")["pending"][0]["can_act"] is False


def test_old_record_without_manager_has_nobody_for_first_stage(service):
    # 舊版存檔沒有 manager 欄位，讀回來第一關不會隨便讓人簽
    rid = service.create("dave", "筆電", 5_000)
    data = service.store.get(rid)
    del data["record"]["manager"]
    service.store.save(data)
    assert service.detail(rid, "alice")["pending"][0]["approvers"] == []
    with pytest.raises(PermissionError, match="alice 沒有可以簽的關卡"):
        service.approve(rid, "alice")


def test_ready_user_tasks_skips_engine_tasks(service):
    # 剛建立還沒跑的 workflow，READY 的只有引擎自己的 Start，不算使用者要簽的關卡
    assert service._ready_user_tasks(BpmnWorkflow(service.spec)) == []


# ---------------------------------------------------------------------------
# 指定 node_id：同一個人同時有兩關可以簽
# ---------------------------------------------------------------------------
@pytest.fixture
def carol_signs_both(service, monkeypatch):
    monkeypatch.setitem(APPROVERS, "legal_approve", {"carol", "erin"})
    rid = service.create("dave", "伺服器", 50_000)
    service.approve(rid, "alice")
    return rid


@pytest.mark.parametrize("node_id, node_name, other", [
    ("cfo_approve", "財務長審核", "法務審核"), ("legal_approve", "法務審核", "財務長審核"),
])
def test_approve_picks_given_node(service, carol_signs_both, node_id, node_name, other):
    # 指定的那關是手動核准，另一關因為 carol 這一輪已經核准過而自動核准
    service.approve(carol_signs_both, "carol", node_id=node_id)
    assert service.detail(carol_signs_both, "dave")["status"] == "done"
    assert history(service, carol_signs_both)[-2:] == [
        entry("carol", "核准", node_name),
        entry("carol", "自動核准", other, "", f"已在「{node_name}」核准過"),
    ]


@pytest.mark.parametrize("node_id, node_name", [("cfo_approve", "財務長審核"), ("legal_approve", "法務審核")])
def test_reject_picks_given_node(service, carol_signs_both, node_id, node_name):
    service.reject(carol_signs_both, "carol", node_id=node_id)
    assert history(service, carol_signs_both)[-1] == entry("carol", "退回", node_name, "", "退回到「申請人填寫」")


# ---------------------------------------------------------------------------
# 自訂 BPMN：送出時就要算 Gateway 條件、關卡沒有設定簽核人
# ---------------------------------------------------------------------------
GATEWAY_FIRST_BPMN = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  id="Definitions_test" targetNamespace="http://bpmn.io/schema/bpmn">
  <bpmn:process id="gateway_first" isExecutable="true">
    <bpmn:startEvent id="start" name="送出申請"><bpmn:outgoing>f1</bpmn:outgoing></bpmn:startEvent>
    <bpmn:exclusiveGateway id="gw" default="f_small">
      <bpmn:incoming>f1</bpmn:incoming><bpmn:outgoing>f_big</bpmn:outgoing><bpmn:outgoing>f_small</bpmn:outgoing>
    </bpmn:exclusiveGateway>
    <bpmn:userTask id="manager_approve" name="直屬主管審核">
      <bpmn:incoming>f_small</bpmn:incoming><bpmn:outgoing>f2</bpmn:outgoing>
    </bpmn:userTask>
    <bpmn:userTask id="board_approve" name="董事會審核">
      <bpmn:incoming>f_big</bpmn:incoming><bpmn:outgoing>f3</bpmn:outgoing>
    </bpmn:userTask>
    <bpmn:endEvent id="end"><bpmn:incoming>f2</bpmn:incoming><bpmn:incoming>f3</bpmn:incoming></bpmn:endEvent>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="gw" />
    <bpmn:sequenceFlow id="f_big" sourceRef="gw" targetRef="board_approve">
      <bpmn:conditionExpression xsi:type="bpmn:tFormalExpression">self.amount_total &gt; 10000</bpmn:conditionExpression>
    </bpmn:sequenceFlow>
    <bpmn:sequenceFlow id="f_small" sourceRef="gw" targetRef="manager_approve" />
    <bpmn:sequenceFlow id="f2" sourceRef="manager_approve" targetRef="end" />
    <bpmn:sequenceFlow id="f3" sourceRef="board_approve" targetRef="end" />
  </bpmn:process>
</bpmn:definitions>
"""


@pytest.fixture
def gateway_first(tmp_path):
    bpmn_path = tmp_path / "gateway_first.bpmn"
    bpmn_path.write_text(GATEWAY_FIRST_BPMN, encoding="utf-8")
    return ApprovalService(JsonStore(tmp_path / "requests.json"), bpmn_path)


def test_create_evaluates_gateway_with_record(gateway_first):
    # 送出時就會算 Gateway 條件，create 用的 script engine 必須認得 self
    assert pending(gateway_first, gateway_first.create("dave", "筆電", 5_000)) == {"manager_approve"}
    assert pending(gateway_first, gateway_first.create("dave", "伺服器", 50_000)) == {"board_approve"}


def test_node_without_approvers_nobody_can_sign(gateway_first):
    rid = gateway_first.create("dave", "伺服器", 50_000)
    assert gateway_first.detail(rid, "alice")["pending"][0]["approvers"] == []
    assert gateway_first.list_requests("alice")[0]["my_turn"] is False
    with pytest.raises(PermissionError):
        gateway_first.approve(rid, "alice")


def test_bpmn_without_applicant_stage_rejects_to_cancel(gateway_first):
    # 流程裡沒有「申請人填寫」就退不回去，退回等於整張作廢
    rid = gateway_first.create("dave", "筆電", 5_000)
    assert gateway_first.detail(rid, "alice")["pending"][0]["reject_to"] is None
    gateway_first.reject(rid, "alice")
    assert gateway_first.detail(rid, "dave")["status"] == "cancelled"
    assert history(gateway_first, rid)[-1] == entry("alice", "退回", "直屬主管審核", "", "整張作廢")


# ---------------------------------------------------------------------------
# Script engine
# ---------------------------------------------------------------------------
def record():
    return PurchaseRequest("PR-001", "筆電", 5_000, applicant="dave")


def test_script_environment_exposes_record_and_contexts():
    env = RecordScriptEnvironment(record())
    assert env.evaluate("self.amount_total + record.amount_total + x", {"x": 1}) == 10_001
    assert env.evaluate("self.amount_total + y", {}, {"y": 1}) == 5_001


def test_script_engine_passes_external_context():
    task = SimpleNamespace(data={}, task_spec=SimpleNamespace(bpmn_id="some_script"))
    RecordScriptEngine(record()).execute(task, "out = z * 2", {"z": 21})
    assert task.data["out"] == 42


CALL_ACTIVITY_BPMN = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  id="Definitions_test" targetNamespace="http://bpmn.io/schema/bpmn">
  <bpmn:process id="main_process" isExecutable="true">
    <bpmn:startEvent id="start" name="送出申請"><bpmn:outgoing>f1</bpmn:outgoing></bpmn:startEvent>
    <bpmn:callActivity id="call_review" name="呼叫審核" calledElement="review">
      <bpmn:incoming>f1</bpmn:incoming><bpmn:outgoing>f2</bpmn:outgoing>
    </bpmn:callActivity>
    <bpmn:endEvent id="end"><bpmn:incoming>f2</bpmn:incoming></bpmn:endEvent>
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="call_review" />
    <bpmn:sequenceFlow id="f2" sourceRef="call_review" targetRef="end" />
  </bpmn:process>
  <bpmn:process id="review" isExecutable="true">
    <bpmn:startEvent id="review_start"><bpmn:outgoing>r1</bpmn:outgoing></bpmn:startEvent>
    <bpmn:userTask id="manager_approve" name="直屬主管審核">
      <bpmn:incoming>r1</bpmn:incoming><bpmn:outgoing>r2</bpmn:outgoing>
    </bpmn:userTask>
    <bpmn:endEvent id="review_end"><bpmn:incoming>r2</bpmn:incoming></bpmn:endEvent>
    <bpmn:sequenceFlow id="r1" sourceRef="review_start" targetRef="manager_approve" />
    <bpmn:sequenceFlow id="r2" sourceRef="manager_approve" targetRef="review_end" />
  </bpmn:process>
</bpmn:definitions>
"""


def test_call_activity_runs_subprocess(tmp_path):
    bpmn_path = tmp_path / "call_activity.bpmn"
    bpmn_path.write_text(CALL_ACTIVITY_BPMN, encoding="utf-8")
    service = ApprovalService(JsonStore(tmp_path / "requests.json"), bpmn_path)

    # 子流程的 spec 要一起交給 BpmnWorkflow，Call Activity 才跑得進去
    rid = service.create("dave", "筆電", 5_000)
    assert pending(service, rid) == {"manager_approve"}
    service.approve(rid, "alice")
    assert service.detail(rid, "dave")["status"] == "done"


# ---------------------------------------------------------------------------
# 系統預設編碼不是 UTF-8（例如繁中 Windows 是 cp950）時，讀寫中文也不能壞
# ---------------------------------------------------------------------------
# 用 UTF-8 locale 啟動（命令列和環境變數才不會被解壞），再把 LC_CTYPE 切成 C，
# 之後沒指定 encoding 的檔案讀寫都會用 ASCII
NON_UTF8_SCRIPT = """
import locale
import sys

locale.setlocale(locale.LC_CTYPE, "C")
assert locale.getencoding() != "UTF-8"

from store import JsonStore
from workflow import ApprovalService

path, rid = sys.argv[1:]
service = ApprovalService(JsonStore(path))
assert service.detail(rid, "dave")["record"]["title"] == "伺服器"
service.approve(rid, "alice")
"""


def test_works_when_locale_encoding_is_not_utf8(tmp_path):
    # 本行程寫入中文單據 → 預設編碼是 ASCII 的子行程讀出並核准 → 本行程再讀回來
    path = tmp_path / "requests.json"
    rid = ApprovalService(JsonStore(path)).create("dave", "伺服器", 5_000)

    env = {**os.environ, "LC_ALL": "C.UTF-8", "PYTHONUTF8": "0"}
    result = subprocess.run(
        [sys.executable, "-c", NON_UTF8_SCRIPT, str(path), rid],
        cwd=os.path.dirname(workflow.__file__), env=env, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr

    detail = ApprovalService(JsonStore(path)).detail(rid, "dave")
    assert detail["status"] == "done"
    assert detail["record"]["title"] == "伺服器"
