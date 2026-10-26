"""
SpiffWorkflow 簽核服務：送出、核准、退回、查詢狀態。

main.py（FastAPI）和 tests 都只透過 ApprovalService 操作，SpiffWorkflow 的細節都在這支檔案裡。
"""
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from SpiffWorkflow.bpmn.parser import BpmnParser
from SpiffWorkflow.bpmn.script_engine import PythonScriptEngine, TaskDataEnvironment
from SpiffWorkflow.bpmn.serializer import BpmnWorkflowSerializer
from SpiffWorkflow.bpmn.workflow import BpmnWorkflow
from SpiffWorkflow.util.task import TaskState

from store import JsonStore

BPMN_PATH = Path(__file__).with_name("purchase_approval.bpmn")

# ---------------------------------------------------------------------------
# 1. 角色與權限
#    SpiffWorkflow 只管「流程走到哪一關」，誰能簽哪一關要自己管：
#    - 第一關「直屬主管審核」看申請人的直屬主管（MANAGER_OF），不是固定某個人
#    - 其他關卡是固定的職位（APPROVERS）
#    - 不管哪一關，申請人都不能簽自己送出的單；固定職位只有申請人自己時，改由申請人的直屬主管簽
#    - 同一個人這一輪已經核准過，之後輪到他的關卡自動核准，不用再簽一次
#    - 「申請人填寫」是申請人自己的關卡：送出時直接完成，被退回時回到這裡改完重新送出
# ---------------------------------------------------------------------------
UserId = Literal["dave", "alice", "carol", "erin", "bob", "frank"]

USERS = {
    "dave": "申請人",
    "alice": "直屬主管",
    "carol": "財務長",
    "erin": "法務",
    "bob": "一般員工，沒有簽核權限",
    "frank": "總經理",
}

# 組織關係：誰的直屬主管是誰。None 代表已經是最高層，沒有人可以簽他的第一關，所以不能送單
MANAGER_OF = {
    "dave": "alice",
    "bob": "alice",
    "alice": "carol",
    "erin": "carol",
    "carol": "frank",
    "frank": None,
}

# 申請人自己的關卡（BPMN 裡的 id）：送出、退回後修改都在這一關
APPLICANT_NODE = "applicant_revise"

# 由申請人的直屬主管簽的那一關（BPMN 裡的 id）
MANAGER_NODE = "manager_approve"

# 固定職位的關卡（BPMN 裡的 id）誰可以簽
APPROVERS = {
    "cfo_approve": {"carol"},
    "legal_approve": {"erin"},
}

class WorkflowClosed(Exception):
    """單據已結案或已作廢，不能再簽核"""


class WrongStage(Exception):
    """單據不在這個動作需要的關卡，例如沒被退回卻要重新送出"""


# ---------------------------------------------------------------------------
# 2. 業務單據
# ---------------------------------------------------------------------------
@dataclass
class PurchaseRequest:
    id: str
    title: str
    amount_total: int
    applicant: str
    state: str = "to_approve"  # to_approve / returned / approved / cancelled
    manager: str | None = None  # 送出當下申請人的直屬主管，之後組織異動也不影響這張單


# ---------------------------------------------------------------------------
# 3. 自訂 Script Engine
#    - evaluate：讓 Gateway 條件可以寫 self.amount_total
#    - execute ：BPMN 跑到有註冊動作的節點時，執行對應的 Python 函式
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

    def __init__(self, record):
        self.record = record
        super().__init__(environment=RecordScriptEnvironment(record))

    def execute(self, task, script, external_context=None):
        action = NODE_ACTIONS.get(task.task_spec.bpmn_id)
        if action:
            action(self.record)
        return super().execute(task, script, external_context)


def mark_record_approved(record):
    record.state = "approved"


NODE_ACTIONS = {"write_back": mark_record_approved}


# ---------------------------------------------------------------------------
# 4. 簽核服務
# ---------------------------------------------------------------------------
# 同一個節點在 task tree 裡可能出現好幾份（預測的分支、並行分支各一份），
# 畫流程圖時每個節點只能有一種顏色，取最「進行中」的那個狀態
STATE_PRIORITY = ["READY", "WAITING", "STARTED", "COMPLETED", "FUTURE", "LIKELY", "MAYBE", "CANCELLED", "ERROR"]


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class ApprovalService:

    def __init__(self, store: JsonStore, bpmn_path=BPMN_PATH):
        self.store = store
        self.serializer = BpmnWorkflowSerializer()
        self.bpmn_xml = Path(bpmn_path).read_text(encoding="utf-8")
        # lxml 不接受帶 encoding 宣告的 str，要先把 <?xml ...?> 拿掉
        parser = BpmnParser()
        parser.add_bpmn_str(re.sub(r"<\?xml.*?\?>", "", self.bpmn_xml).lstrip())
        process_id = parser.get_process_ids()[0]
        self.spec = parser.get_spec(process_id)
        self.subprocess_specs = parser.get_subprocess_specs(process_id)

    # ---- workflow 的存取：每次操作都是「讀出 → 操作 → 存回」 ----

    def _load(self, data):
        record = PurchaseRequest(**data["record"])
        # deserialize_json 會先做舊版格式的 migration，所以這裡用它而不是 from_dict
        workflow = self.serializer.deserialize_json(json.dumps(data["workflow"]))
        # script engine 不會被序列化，反序列化後一定要掛回去
        workflow.script_engine = RecordScriptEngine(record)
        return record, workflow

    def _save(self, data, record, workflow):
        data["record"] = asdict(record)
        # 存成 dict 而不是字串，打開 data/requests.json 就能直接看到 SpiffWorkflow 存了什麼
        data["workflow"] = json.loads(self.serializer.serialize_json(workflow))
        if data["status"] == "running" and workflow.completed:
            data["status"] = "done"
        self.store.save(data)

    def _open(self, request_id):
        data = self.store.get(request_id)
        if data["status"] != "running":
            raise WorkflowClosed(f"{request_id} 狀態為 {data['status']}，不能再簽核")
        return data, *self._load(data)

    # ---- 查任務 ----

    @staticmethod
    def _ready_user_tasks(workflow):
        return workflow.get_tasks(state=TaskState.READY, manual=True)

    @staticmethod
    def approvers(node_id, record):
        """
        這一關誰能動：
            - 申請人填寫：只有申請人自己
            - 直屬主管審核：申請人的直屬主管
            - 其他關：APPROVERS 的固定名單；名單裡只有申請人自己時，改由申請人的直屬主管簽
        申請人一律不能簽核自己的單
        """
        manager = {record.manager} if record.manager else set()
        if node_id == APPLICANT_NODE:
            return {record.applicant}
        if node_id == MANAGER_NODE:
            allowed = manager
        else:
            fixed = APPROVERS.get(node_id, set())
            allowed = fixed - {record.applicant}
            if fixed and not allowed:
                # 例如 erin 送的單到了法務審核，不改人的話這一關永遠沒人能簽
                allowed = manager
        return allowed - {record.applicant}

    def _authorized_task(self, workflow, record, user, node_id=None):
        """找這個人現在可以核准／退回的關卡；申請人填寫不是簽核關卡，要用 resubmit / withdraw"""
        for task in self._ready_user_tasks(workflow):
            if node_id and task.task_spec.bpmn_id != node_id:
                continue
            if task.task_spec.bpmn_id == APPLICANT_NODE:
                continue
            if user in self.approvers(task.task_spec.bpmn_id, record):
                return task
        if user == record.applicant:
            raise PermissionError(f"{user} 是申請人，不能簽核自己送出的單據")
        raise PermissionError(f"{user} 沒有可以簽的關卡")

    def _applicant_task(self, workflow):
        """申請人填寫這一關現在是不是 READY（單據在申請人手上）"""
        return next((t for t in self._ready_user_tasks(workflow) if t.task_spec.bpmn_id == APPLICANT_NODE), None)

    @staticmethod
    def _reject_target(task):
        """
        往上找「申請人填寫」那個 task：任何一關退回都回到申請人。
        流程裡沒有這一關（例如測試用的自訂 BPMN）就回傳 None，代表整張作廢
        """
        previous = task.parent
        while previous and getattr(previous.task_spec, "bpmn_id", None) != APPLICANT_NODE:
            previous = previous.parent
        return previous

    @staticmethod
    def _complete(workflow, task, user):
        # 寫進 task.data 的值會往後面的節點傳，Gateway 條件也讀得到
        task.data[f"{task.task_spec.bpmn_id}_by"] = user
        task.complete()
        workflow.do_engine_steps()

    def _submit(self, workflow, user):
        """完成「申請人填寫」，流程往下走到第一個簽核關卡"""
        task = self._applicant_task(workflow)
        if task:
            self._complete(workflow, task, user)

    @staticmethod
    def _approved_this_round(workflow):
        """
        這一輪已經核准過的人 → 他核准的關卡名稱。
        退回時 reset_from_task_id 會把「申請人填寫」底下的 task 全部清掉，所以樹上還留著的簽核 task 都是這一輪的
        """
        approved = {}
        for task in workflow.get_tasks(state=TaskState.COMPLETED, manual=True):
            user = task.data.get(f"{task.task_spec.bpmn_id}_by")
            if task.task_spec.bpmn_id != APPLICANT_NODE and user:
                approved.setdefault(user, task.task_spec.bpmn_name)
        return approved

    def _next_auto_task(self, workflow, record):
        """找一關簽核人裡有人這一輪已經核准過的，回傳 (task, 簽核人, 他之前核准的關卡)"""
        approved = self._approved_this_round(workflow)
        for task in self._ready_user_tasks(workflow):
            signers = sorted(self.approvers(task.task_spec.bpmn_id, record) & approved.keys())
            if signers:
                return task, signers[0], approved[signers[0]]
        return None

    def _auto_approve(self, data, record, workflow):
        """同一個人這一輪已經核准過，之後輪到他的關卡就自動核准，例如 carol 同時是 alice 的主管和財務長"""
        while found := self._next_auto_task(workflow, record):
            task, user, earlier = found
            self._complete(workflow, task, user)
            self._log(data, user, "自動核准", task.task_spec.bpmn_name, result=f"已在「{earlier}」核准過")

    # ---- 操作 ----

    def create(self, user, title, amount_total):
        """送出申請：建立 workflow，跑到第一個 UserTask 停下來"""
        manager = MANAGER_OF.get(user)
        if not manager:
            raise PermissionError(f"{user} 沒有設定直屬主管，不能送出申請")
        with self.store.lock:
            record = PurchaseRequest(self.store.next_id(), title, amount_total, applicant=user, manager=manager)
            workflow = BpmnWorkflow(self.spec, self.subprocess_specs, script_engine=RecordScriptEngine(record))
            workflow.do_engine_steps()
            self._submit(workflow, user)
            data = {"status": "running", "created_at": now(), "history": []}
            self._log(data, user, "送出", "申請人填寫")
            self._save(data, record, workflow)
            return record.id

    def resubmit(self, request_id, user, title, amount_total, comment=""):
        """被退回後，申請人改完標題、金額重新送出"""
        with self.store.lock:
            data, record, workflow = self._open(request_id)
            if user != record.applicant:
                raise PermissionError(f"{user} 不是申請人，不能重新送出")
            if not self._applicant_task(workflow):
                raise WrongStage(f"{request_id} 不在申請人手上，不能重新送出")
            record.title, record.amount_total, record.state = title, amount_total, "to_approve"
            self._submit(workflow, user)
            self._log(data, user, "重新送出", "申請人填寫", comment)
            self._save(data, record, workflow)

    def withdraw(self, request_id, user, comment=""):
        """申請人撤回：結案前隨時可以，整張作廢"""
        with self.store.lock:
            data, record, workflow = self._open(request_id)
            if user != record.applicant:
                raise PermissionError(f"{user} 不是申請人，不能撤回")
            stage = "、".join(t.task_spec.bpmn_name for t in self._ready_user_tasks(workflow))
            self._cancel(data, record, workflow)
            self._log(data, user, "撤回", stage, comment, result="整張作廢")
            self._save(data, record, workflow)

    @staticmethod
    def _cancel(data, record, workflow):
        workflow.cancel()
        record.state = "cancelled"
        data["status"] = "cancelled"

    def approve(self, request_id, user, comment="", node_id=None):
        with self.store.lock:
            data, record, workflow = self._open(request_id)
            task = self._authorized_task(workflow, record, user, node_id)
            self._complete(workflow, task, user)
            self._log(data, user, "核准", task.task_spec.bpmn_name, comment)
            self._auto_approve(data, record, workflow)
            self._save(data, record, workflow)

    def reject(self, request_id, user, comment="", node_id=None):
        with self.store.lock:
            data, record, workflow = self._open(request_id)
            task = self._authorized_task(workflow, record, user, node_id)
            target = self._reject_target(task)
            if target is None:
                self._cancel(data, record, workflow)
                self._log(data, user, "退回", task.task_spec.bpmn_name, comment, result="整張作廢")
            else:
                # 從「申請人填寫」往下的節點全部清掉重來，並行分支裡已經簽過的也會一起被清掉
                workflow.reset_from_task_id(target.id)
                record.state = "returned"
                self._log(data, user, "退回", task.task_spec.bpmn_name, comment,
                          result=f"退回到「{target.task_spec.bpmn_name}」")
            self._save(data, record, workflow)

    def clear(self):
        with self.store.lock:
            self.store.clear()

    @staticmethod
    def _log(data, user, action, node_name, comment="", result=""):
        data["history"].append({
            "time": now(), "user": user, "action": action,
            "node": node_name, "comment": comment, "result": result,
        })

    # ---- 查詢 ----

    def list_requests(self, user):
        rows = []
        for data in self.store.all():
            record, workflow = self._load(data)
            rows.append({
                **asdict(record),
                "status": data["status"],
                "pending": [t.task_spec.bpmn_name for t in self._ready_user_tasks(workflow)],
                "my_turn": data["status"] == "running" and self._can_act(workflow, record, user),
            })
        return rows

    def _can_act(self, workflow, record, user):
        return any(user in self.approvers(t.task_spec.bpmn_id, record) for t in self._ready_user_tasks(workflow))

    def detail(self, request_id, user):
        data = self.store.get(request_id)
        record, workflow = self._load(data)
        running = data["status"] == "running"

        pending = []
        for task in self._ready_user_tasks(workflow) if running else []:
            target = self._reject_target(task)
            approvers = sorted(self.approvers(task.task_spec.bpmn_id, record))
            pending.append({
                "node_id": task.task_spec.bpmn_id,
                "node_name": task.task_spec.bpmn_name,
                # applicant：申請人填寫，用 resubmit / withdraw；approval：簽核關卡，用 approve / reject
                "stage": "applicant" if task.task_spec.bpmn_id == APPLICANT_NODE else "approval",
                "approvers": approvers,
                "can_act": user in approvers,
                "reject_to": target.task_spec.bpmn_name if target else None,
            })

        return {
            "record": asdict(record),
            "status": data["status"],
            "created_at": data["created_at"],
            "pending": pending,
            "nodes": self._node_states(workflow),
            "taken": self._taken_flows(workflow),
            "tree": self._task_tree(workflow),
            "history": data["history"],
            "task_data": workflow.last_task.data if workflow.completed and data["status"] == "done" else None,
            "json_size": len(json.dumps(data["workflow"])),
        }

    def workflow_json(self, request_id):
        return self.store.get(request_id)["workflow"]

    @staticmethod
    def _bpmn_tasks(workflow):
        return [t for t in workflow.get_tasks() if getattr(t.task_spec, "bpmn_id", None)]

    @staticmethod
    def _bpmn_parent(task):
        parent = task.parent
        while parent and not getattr(parent.task_spec, "bpmn_id", None):
            parent = parent.parent
        return parent

    def _task_tree(self, workflow):
        """SpiffWorkflow 內部的 task tree，depth 用來縮排"""
        tasks = self._bpmn_tasks(workflow)
        base = min((t.depth for t in tasks), default=0)
        return [{
            "node_id": t.task_spec.bpmn_id,
            "node_name": t.task_spec.bpmn_name,
            "type": type(t.task_spec).__name__,
            "state": TaskState.get_name(t.state),
            "depth": t.depth - base,
        } for t in tasks]

    def _node_states(self, workflow):
        """每個 BPMN 節點一個狀態，給流程圖上色用"""
        states = {}
        for t in self._bpmn_tasks(workflow):
            name = TaskState.get_name(t.state)
            current = states.get(t.task_spec.bpmn_id)
            if current is None or STATE_PRIORITY.index(name) < STATE_PRIORITY.index(current):
                states[t.task_spec.bpmn_id] = name
        return states

    def _taken_flows(self, workflow):
        """實際走過的連線 [來源節點, 目標節點]：父節點已完成，子節點也已經走到（不是預測或 FUTURE）"""
        not_reached = TaskState.PREDICTED_MASK | TaskState.FUTURE
        taken = set()
        for t in self._bpmn_tasks(workflow):
            parent = self._bpmn_parent(t)
            if parent and parent.state == TaskState.COMPLETED and not t.has_state(not_reached):
                taken.add((parent.task_spec.bpmn_id, t.task_spec.bpmn_id))
        return [list(edge) for edge in sorted(taken)]
