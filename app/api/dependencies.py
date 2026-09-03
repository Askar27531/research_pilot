from typing import Annotated

from fastapi import Depends, Request

from app.db import Database
from app.services import WorkspaceService


def get_database(request: Request) -> Database:
    return request.app.state.database


def get_workspace_service(
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> WorkspaceService:
    return WorkspaceService(request.app, database)
