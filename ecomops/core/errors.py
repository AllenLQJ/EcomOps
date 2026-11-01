from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = "", details: dict | None = None):
        self.status, self.code, self.message, self.details = status, code, message or code, details or {}


def body(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(_: Request, e: ApiError):
        return JSONResponse(body(e.code, e.message, e.details), status_code=e.status)

    @app.exception_handler(RequestValidationError)
    async def _val(_: Request, e: RequestValidationError):
        errs = [{"loc": x.get("loc"), "msg": x.get("msg")} for x in e.errors()[:5]]
        return JSONResponse(body("INVALID_REQUEST", "invalid request", {"errors": errs}), status_code=400)
