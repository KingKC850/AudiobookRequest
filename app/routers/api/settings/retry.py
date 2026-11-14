from typing import Annotated

from fastapi import APIRouter, Depends, Response, Security
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.internal.auth.authentication import AnyAuth, DetailedUser
from app.internal.models import GroupEnum
from app.internal.retry.state import retry_config
from app.util.db import get_session

router = APIRouter(prefix="/retry")


class RetrySettings(BaseModel):
    enabled: bool
    min_hours: int = Field(ge=1)
    delay_seconds: int = Field(ge=0)
    max_per_run: int = Field(ge=0)
    stuck_days: int = Field(default=3, ge=1)


def read_retry_settings(session: Session) -> RetrySettings:
    return RetrySettings(
        enabled=retry_config.get_enabled(session),
        min_hours=retry_config.get_min_hours(session),
        delay_seconds=retry_config.get_delay_seconds(session),
        max_per_run=retry_config.get_max_per_run(session),
        stuck_days=retry_config.get_stuck_days(session),
    )


@router.get("", response_model=RetrySettings)
def get_retry_settings(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    return read_retry_settings(session)


@router.patch("", status_code=204)
def update_retry_settings(
    body: RetrySettings,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    retry_config.set_enabled(session, body.enabled)
    retry_config.set_min_hours(session, body.min_hours)
    retry_config.set_delay_seconds(session, body.delay_seconds)
    retry_config.set_max_per_run(session, body.max_per_run)
    retry_config.set_stuck_days(session, body.stuck_days)
    return Response(status_code=204)
