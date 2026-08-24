import logging
from http import HTTPStatus
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.db.errors import EvidenceReferencedError, ProjectConflictError, RecordNotFoundError
from app.documents import (
    DocumentError,
    DocumentNotFoundError,
    DocumentSecurityError,
    DocumentValidationError,
)
from app.literature.errors import LiteratureError, OpenAlexAuthRequiredError
from app.llm import LLMError

logger = logging.getLogger(__name__)


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool
    request_id: str
    details: list[dict[str, Any]] | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


def request_id(request: Request) -> str:
    return getattr(request.state, "request_id", str(uuid4()))


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    retryable: bool = False,
    details: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            retryable=retryable,
            request_id=request_id(request),
            details=details,
        )
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(exclude_none=True))


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        value = request.headers.get("X-Request-ID") or str(uuid4())
        request.state.request_id = value[:128]
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        logger.info(
            "request_id=%s method=%s path=%s status=%s",
            request.state.request_id,
            request.method,
            request.url.path,
            response.status_code,
        )
        return response


async def llm_error_handler(request: Request, exc: LLMError) -> JSONResponse:
    return error_response(
        request,
        status_code=503,
        code="MODEL_UNAVAILABLE",
        message=str(exc),
        retryable=True,
    )


async def literature_error_handler(request: Request, exc: LiteratureError) -> JSONResponse:
    if isinstance(exc, OpenAlexAuthRequiredError):
        return error_response(
            request,
            status_code=503,
            code="OPENALEX_AUTH_REQUIRED",
            message=str(exc),
            retryable=False,
        )
    return error_response(
        request,
        status_code=503 if exc.retryable else 502,
        code="LITERATURE_UNAVAILABLE" if exc.retryable else "LITERATURE_ERROR",
        message=str(exc),
        retryable=exc.retryable,
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    safe_details = [
        {"location": list(error["loc"]), "message": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return error_response(
        request,
        status_code=422,
        code="VALIDATION_ERROR",
        message="Request validation failed",
        details=safe_details,
    )


async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    message = str(exc.detail)
    code = "HTTP_ERROR"
    try:
        code = HTTPStatus(exc.status_code).name
    except ValueError:
        pass
    return error_response(request, exc.status_code, code, message)


async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("request_id=%s unhandled application error", request_id(request), exc_info=exc)
    return error_response(
        request,
        status_code=500,
        code="INTERNAL_ERROR",
        message="An unexpected internal error occurred",
        retryable=False,
    )


async def record_not_found_handler(request: Request, exc: RecordNotFoundError) -> JSONResponse:
    return error_response(request, 404, "NOT_FOUND", str(exc), retryable=False)


async def project_conflict_handler(request: Request, exc: ProjectConflictError) -> JSONResponse:
    return error_response(request, 409, "PROJECT_CONFLICT", str(exc), retryable=False)


async def evidence_referenced_handler(
    request: Request, exc: EvidenceReferencedError
) -> JSONResponse:
    return error_response(request, 409, "EVIDENCE_REFERENCED", str(exc), retryable=False)


async def document_error_handler(request: Request, exc: DocumentError) -> JSONResponse:
    if isinstance(exc, DocumentNotFoundError):
        return error_response(request, 404, "DOCUMENT_NOT_FOUND", str(exc), retryable=False)
    code = "DOCUMENT_SECURITY_ERROR" if isinstance(exc, DocumentSecurityError) else "DOCUMENT_ERROR"
    status = 400 if isinstance(exc, (DocumentSecurityError, DocumentValidationError)) else 422
    return error_response(request, status, code, str(exc), retryable=False)
