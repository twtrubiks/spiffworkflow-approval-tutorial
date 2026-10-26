"""
SpiffWorkflow 簽核流程範例（純 Python，不連任何 server）

整合進實際的系統時，各部分大致這樣對應，這裡都換成純 Python：
    - 流程實例存 workflow JSON 的欄位 → FakeDB 裡的 JSON 字串
    - 每個節點設定的簽核人             → APPROVERS 字典
    - 業務單據（請購單、訂單…）        → PurchaseRequest dataclass
    - 自訂的 script engine            → RecordScriptEngine

執行（在 repo 根目錄）：
    python examples/basic/spiff_approval_demo.py
"""
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from SpiffWorkflow.bpmn.parser import BpmnParser
from SpiffWorkflow.bpmn.script_engine import PythonScriptEngine, TaskDataEnvironment
from SpiffWorkflow.bpmn.serializer import BpmnWorkflowSerializer
from SpiffWorkflow.bpmn.workflow import BpmnWorkflow
from SpiffWorkflow.util.task import TaskState


# ---------------------------------------------------------------------------
# 1. BPMN 流程定義（實務上是存在資料庫裡，由前端 bpmn-js 畫出來）
#    放在同目錄的 spiff_approval_demo.bpmn，用 spiff_approval_demo.html 可以看到流程圖
#
#   送出申請 → 直屬主管審核 → <金額判斷> ─ 金額 > 10000 → 財務長審核 ─┐
#                                        └─ 其他(default) ─────────────┴→ 回寫單據狀態 → 結案
# ---------------------------------------------------------------------------
BPMN_XML = Path(__file__).with_name("spiff_approval_demo.bpmn").read_text(encoding="utf-8")

# 每個 UserTask 誰可以簽（實務上是存在資料庫的節點設定，例如指定使用者或群組）
APPROVERS = {
    "manager_approve": {"alice"},
    "cfo_approve": {"carol"},
}


# ---------------------------------------------------------------------------
# 2. 模擬業務單據與資料庫
# ---------------------------------------------------------------------------
@dataclass
class PurchaseRequest:
    name: str
    amount_total: int
    state: str = "draft"


class FakeDB:
    """模擬流程實例的資料表：存 workflow 的 JSON 狀態、實例狀態、簽核紀錄"""

    def __init__(self):
        self.instances = {}

    def save(self, instance_id, workflow_state, state="running"):
        instance = self.instances.setdefault(instance_id, {"history": []})
        instance.update({"workflow_state": workflow_state, "state": state})

    def load(self, instance_id):
        return self.instances[instance_id]

    def log(self, instance_id, user, action, node_name, comment):
        self.instances[instance_id]["history"].append((user, action, node_name, comment))


# ---------------------------------------------------------------------------
# 3. 自訂 Script Engine
#    - evaluate：讓 Gateway 條件可以寫 self.amount_total / record.amount_total
#    - execute ：遇到有註冊動作的節點，就執行對應的 Python 函式
# ---------------------------------------------------------------------------
class RecordScriptEnvironment(TaskDataEnvironment):

    def __init__(self, record):
        super().__init__()
        self.record = record

    def evaluate(self, expression, context, external_context=None):
        external_context = dict(external_context or {})
        external_context.update({"self": self.record, "record": self.record})
        return super().evaluate(expression, context, external_context)


class RecordScriptEngine(PythonScriptEngine):

    def __init__(self, record, node_actions=None):
        self.record = record
        self.node_actions = node_actions or {}
        super().__init__(environment=RecordScriptEnvironment(record))

    def execute(self, task, script, external_context=None):
        action = self.node_actions.get(task.task_spec.bpmn_id)
        if action:
            action(self.record)
        return super().execute(task, script, external_context)


def mark_record_approved(record):
    record.state = "approved"
    print(f"    [節點動作] {record.name} 狀態改為 approved")


NODE_ACTIONS = {"write_back": mark_record_approved}


# ---------------------------------------------------------------------------
# 4. 簽核服務
# ---------------------------------------------------------------------------
class ApprovalService:

    def __init__(self, bpmn_xml, db):
        self.db = db
        self.serializer = BpmnWorkflowSerializer()
        # lxml 不接受帶 encoding 宣告的 str，要先把 <?xml ...?> 拿掉
        cleaned_xml = re.sub(r"<\?xml.*?\?>", "", bpmn_xml).lstrip()
        self.parser = BpmnParser()
        self.parser.add_bpmn_str(cleaned_xml)
        self.process_id = self.parser.get_process_ids()[0]

    def _engine(self, record):
        return RecordScriptEngine(record, NODE_ACTIONS)

    def _load(self, instance_id, record):
        data = self.db.load(instance_id)
        if data["state"] != "running":
            raise RuntimeError(f"實例狀態為 {data['state']}，不能再簽核")
        workflow = self.serializer.deserialize_json(data["workflow_state"])
        # script engine 不會被序列化，反序列化後一定要掛回去
        workflow.script_engine = self._engine(record)
        return workflow

    def _save(self, instance_id, workflow, state=None):
        if state is None:
            state = "done" if workflow.completed else "running"
        self.db.save(instance_id, self.serializer.serialize_json(workflow), state)

    def _ready_user_tasks(self, workflow):
        return workflow.get_tasks(state=TaskState.READY, manual=True)

    def _authorized_task(self, workflow, user):
        for task in self._ready_user_tasks(workflow):
            if user in APPROVERS.get(task.task_spec.bpmn_id, set()):
                return task
        raise PermissionError(f"{user} 沒有任何待簽的關卡")

    def submit(self, record, user):
        """送出申請：建立 workflow，跑到第一個 UserTask 停下來"""
        spec = self.parser.get_spec(self.process_id)
        subprocess_specs = self.parser.get_subprocess_specs(self.process_id)
        workflow = BpmnWorkflow(spec, subprocess_specs, script_engine=self._engine(record))
        workflow.do_engine_steps()

        instance_id = uuid.uuid4().hex[:8]
        record.state = "to_approve"
        self._save(instance_id, workflow)
        self.db.log(instance_id, user, "送出", "送出申請", "")
        return instance_id

    def approve(self, instance_id, record, user, comment=""):
        workflow = self._load(instance_id, record)
        task = self._authorized_task(workflow, user)
        # 寫進 task.data 的值會一路往後面的節點傳，Gateway 條件也讀得到
        task.data[f"{task.task_spec.bpmn_id}_by"] = user
        task.complete()
        workflow.do_engine_steps()
        self._save(instance_id, workflow)
        self.db.log(instance_id, user, "核准", task.task_spec.bpmn_name, comment)

    def reject(self, instance_id, record, user, comment=""):
        """
        駁回：往回找上一個「不是 ExclusiveGateway」的節點
            - 上一個是 StartEvent → 整張作廢，單據退回 draft
            - 否則 reset_from_task_id 退回上一關重簽
        """
        workflow = self._load(instance_id, record)
        task = self._authorized_task(workflow, user)

        previous = task.parent
        while previous and previous.task_spec.__class__.__name__ == "ExclusiveGateway":
            previous = previous.parent

        if previous is None or previous.task_spec.__class__.__name__ == "StartEvent":
            record.state = "draft"
            self._save(instance_id, workflow, state="cancelled")
        else:
            workflow.reset_from_task_id(previous.id)
            self._save(instance_id, workflow)
        self.db.log(instance_id, user, "駁回", task.task_spec.bpmn_name, comment)

    def status(self, instance_id):
        """
        列出每個節點狀態。給使用者看時只分 READY / COMPLETED / WAITING 三種，
        括號內是 SpiffWorkflow 原始狀態：MAYBE / LIKELY 是 Gateway 之後「預測」可能會走的節點，
        所以同一個節點可能出現兩次（兩條路都會經過）。
        """
        data = self.db.load(instance_id)
        workflow = self.serializer.deserialize_json(data["workflow_state"])
        rows = []
        for task in workflow.get_tasks():
            bpmn_id = getattr(task.task_spec, "bpmn_id", None)
            if not bpmn_id:
                continue
            raw_state = TaskState.get_name(task.state)
            state = raw_state if raw_state in ("READY", "COMPLETED") else "WAITING"
            rows.append((task.task_spec.bpmn_name, state, raw_state))
        final_data = workflow.last_task.data if workflow.completed else None
        return data["state"], rows, final_data


# ---------------------------------------------------------------------------
# 5. 示範情境
# ---------------------------------------------------------------------------
def step(title):
    print(f"\n  ▶ {title}")


def show(service, instance_id, record):
    state, rows, final_data = service.status(instance_id)
    print(f"    單據 {record.name}（{record.amount_total:,}）state={record.state}，實例 state={state}")
    for name, node_state, raw_state in rows:
        print(f"      - {name}：{node_state}（{raw_state}）")
    if final_data:
        print(f"    結案時 task.data = {final_data}")


def show_history(db, instance_id):
    print("    簽核紀錄：")
    for user, action, node_name, comment in db.load(instance_id)["history"]:
        print(f"      {user:<6}{action}  {node_name}  {comment}")


def try_action(func, *args, **kwargs):
    try:
        func(*args, **kwargs)
    except (PermissionError, RuntimeError) as e:
        print(f"    ✗ {e}")


def main():
    db = FakeDB()
    service = ApprovalService(BPMN_XML, db)

    print("=" * 60)
    print("情境 1：小額請購 5,000 → 主管核准就結案")
    pr = PurchaseRequest("PR-001", 5_000)
    step("dave 送出")
    iid = service.submit(pr, "dave")
    show(service, iid, pr)
    step("alice 核准")
    service.approve(iid, pr, "alice", "OK")
    show(service, iid, pr)

    print("\n" + "=" * 60)
    print("情境 2：大額請購 50,000 → 多一關財務長；財務長駁回會退回主管")
    pr = PurchaseRequest("PR-002", 50_000)
    iid = service.submit(pr, "dave")
    step("bob 想簽主管關卡（沒權限）")
    try_action(service.approve, iid, pr, "bob")
    step("alice 核准（金額 > 10000，走到財務長）")
    service.approve(iid, pr, "alice", "同意")
    show(service, iid, pr)
    step("carol 駁回（退回主管）")
    service.reject(iid, pr, "carol", "請補報價單")
    show(service, iid, pr)
    step("alice 再核准、carol 核准")
    service.approve(iid, pr, "alice", "已補報價單")
    service.approve(iid, pr, "carol", "同意")
    show(service, iid, pr)
    show_history(db, iid)
    print(f"    JSON 狀態大小：{len(db.load(iid)['workflow_state']):,} bytes")

    print("\n" + "=" * 60)
    print("情境 3：主管直接駁回（上一關是 StartEvent）→ 整張作廢")
    pr = PurchaseRequest("PR-003", 8_000)
    iid = service.submit(pr, "dave")
    step("alice 駁回")
    service.reject(iid, pr, "alice", "不需要買")
    show(service, iid, pr)
    step("作廢後 alice 想再簽")
    try_action(service.approve, iid, pr, "alice")


if __name__ == "__main__":
    main()
