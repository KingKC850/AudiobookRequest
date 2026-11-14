from typing import Annotated

from fastapi import APIRouter, Depends, Form, Security
from sqlmodel import Session

from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.models import GroupEnum
from app.routers.api.settings.retry import RetrySettings, read_retry_settings
from app.routers.api.settings.retry import (
    update_retry_settings as api_update_retry_settings,
)
from app.util.db import get_session
from app.util.templates import catalog_response, catalog_response_toast

router = APIRouter(prefix="/retry")


@router.get("/hx-form")
def read_retry(
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
):
    _ = admin_user
    return catalog_response(
        "Settings.Download.Retry",
        settings=read_retry_settings(session),
    )


@router.post("/hx-form")
def update_retry(
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
    min_hours: Annotated[int, Form(ge=1)],
    delay_seconds: Annotated[int, Form(ge=0)],
    max_per_run: Annotated[int, Form(ge=0)],
    stuck_days: Annotated[int, Form(ge=1)],
    enabled: Annotated[bool, Form()] = False,
):
    settings = RetrySettings(
        enabled=enabled,
        min_hours=min_hours,
        delay_seconds=delay_seconds,
        max_per_run=max_per_run,
        stuck_days=stuck_days,
    )
    api_update_retry_settings(settings, session, admin_user)
    return catalog_response_toast(
        "Settings.Download.Retry",
        "Settings updated",
        "success",
        settings=settings,
    )
