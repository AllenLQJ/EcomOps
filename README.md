# EcomOps · 智购中台

B 端风控审批与运营实训平台（FastAPI + LangGraph + PostgreSQL + Vue 3），EcomPilot 的对端服务。

规格见 EcomPilot 仓库的 `ecompilot_project_roadmap.md`（项目四）。本仓库刚初始化，尚无实现。

与 EcomPilot 的契约：
- 接收 `POST /internal/v1/claims/submit`
- 审批通过后调用 EcomPilot `POST /internal/v1/refunds/execute`，超时用 `GET /internal/v1/refunds/status` 对账
