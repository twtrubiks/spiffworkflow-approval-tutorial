"""
FastAPI：提供簽核 API 和操作頁面。

執行：
    uvicorn main:app --reload
然後打開 http://127.0.0.1:8000 （API 文件在 /docs）
"""
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from store import JsonStore, RequestNotFound
from workflow import APPROVERS, MANAGER_NODE, MANAGER_OF, USERS, ApprovalService, UserId, WorkflowClosed, WrongStage

BASE_DIR = Path(__file__).parent


class CreateBody(BaseModel):
    user: UserId = "dave"
    title: str = Field("請購單", min_length=1, max_length=50)
    amount_total: int = Field(5000, gt=0, le=100_000_000)


class ActionBody(BaseModel):
    user: UserId
    comment: str = Field("", max_length=200)
    node_id: str | None = Field(None, description="要簽哪一關（BPMN 節點 id）；不給就簽這個人第一個可簽的關卡")


class ResubmitBody(BaseModel):
    user: UserId
    title: str = Field(..., min_length=1, max_length=50)
    amount_total: int = Field(..., gt=0, le=100_000_000)
    comment: str = Field("", max_length=200)


class WithdrawBody(BaseModel):
    user: UserId
    comment: str = Field("", max_length=200)


def get_service(request: Request) -> ApprovalService:
    return request.app.state.service


def create_app(data_path=BASE_DIR / "data" / "requests.json") -> FastAPI:
    app = FastAPI(title="SpiffWorkflow 簽核模擬", description="切換身分，模擬送出、核准、退回")
    app.state.service = ApprovalService(JsonStore(data_path))

    # service 丟出的例外轉成對應的 HTTP 狀態碼
    @app.exception_handler(PermissionError)
    def forbidden(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(WorkflowClosed)
    @app.exception_handler(WrongStage)
    def wrong_state(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(RequestNotFound)
    def not_found(request, exc):
        return JSONResponse({"detail": f"找不到單據 {exc}"}, status_code=404)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(BASE_DIR / "static" / "index.html")

    @app.get("/api/users", summary="所有角色、每個人的直屬主管，以及每個人可以簽哪幾關")
    def users():
        def nodes(uid):
            fixed = {n for n, us in APPROVERS.items() if uid in us}
            # 是別人的直屬主管，就簽得到「直屬主管審核」這一關
            return sorted(fixed | ({MANAGER_NODE} if uid in MANAGER_OF.values() else set()))

        return [
            {"id": uid, "role": role, "manager": MANAGER_OF.get(uid), "nodes": nodes(uid)}
            for uid, role in USERS.items()
        ]

    @app.get("/api/bpmn", summary="流程定義（BPMN XML）")
    def bpmn(service: ApprovalService = Depends(get_service)):
        return Response(service.bpmn_xml, media_type="application/xml")

    @app.get("/api/requests", summary="單據列表；my_turn 表示這個人現在有關卡可以簽")
    def list_requests(user: UserId, service: ApprovalService = Depends(get_service)):
        return service.list_requests(user)

    @app.post("/api/requests", status_code=201, summary="送出請購單；第一關由申請人的直屬主管簽，沒有主管的人不能送")
    def create_request(body: CreateBody, service: ApprovalService = Depends(get_service)):
        request_id = service.create(body.user, body.title, body.amount_total)
        return service.detail(request_id, body.user)

    @app.get("/api/requests/{request_id}", summary="單據明細：節點狀態、task tree、簽核紀錄")
    def get_request(request_id: str, user: UserId, service: ApprovalService = Depends(get_service)):
        return service.detail(request_id, user)

    @app.post("/api/requests/{request_id}/approve", summary="核准；申請人不能簽自己的單，同一輪已經核准過的人，之後輪到他的關卡自動核准")
    def approve(request_id: str, body: ActionBody, service: ApprovalService = Depends(get_service)):
        service.approve(request_id, body.user, body.comment, body.node_id)
        return service.detail(request_id, body.user)

    @app.post("/api/requests/{request_id}/reject", summary="退回給申請人修改")
    def reject(request_id: str, body: ActionBody, service: ApprovalService = Depends(get_service)):
        service.reject(request_id, body.user, body.comment, body.node_id)
        return service.detail(request_id, body.user)

    @app.post("/api/requests/{request_id}/resubmit", summary="被退回後，申請人改完重新送出")
    def resubmit(request_id: str, body: ResubmitBody, service: ApprovalService = Depends(get_service)):
        service.resubmit(request_id, body.user, body.title, body.amount_total, body.comment)
        return service.detail(request_id, body.user)

    @app.post("/api/requests/{request_id}/withdraw", summary="申請人撤回，整張作廢")
    def withdraw(request_id: str, body: WithdrawBody, service: ApprovalService = Depends(get_service)):
        service.withdraw(request_id, body.user, body.comment)
        return service.detail(request_id, body.user)

    @app.get("/api/requests/{request_id}/workflow-json", summary="SpiffWorkflow 序列化後的原始 JSON")
    def workflow_json(request_id: str, service: ApprovalService = Depends(get_service)):
        return service.workflow_json(request_id)

    @app.delete("/api/requests", status_code=204, summary="清空所有單據")
    def clear(service: ApprovalService = Depends(get_service)):
        service.clear()

    return app


app = create_app()
