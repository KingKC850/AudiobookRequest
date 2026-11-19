from typing import Annotated

from fastapi import APIRouter, Depends, Response, Security
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.internal.auth.authentication import AnyAuth, DetailedUser
from app.internal.models import GroupEnum
from app.internal.series.config import SeriesMode, series_config
from app.util.db import get_session

router = APIRouter(prefix="/series")


class SeriesSettings(BaseModel):
    enabled: bool
    default_mode: SeriesMode
    run_hour: int = Field(ge=0, le=23)
    max_per_run: int = Field(ge=0)
    user: str = Field(min_length=1, max_length=100)
    language_filter: bool


def read_series_settings(session: Session) -> SeriesSettings:
    return SeriesSettings(
        enabled=series_config.get_enabled(session),
        default_mode=series_config.get_default_mode(session),
        run_hour=series_config.get_run_hour(session),
        max_per_run=series_config.get_max_per_run(session),
        user=series_config.get_user(session),
        language_filter=series_config.get_language_filter(session),
    )


@router.get("", response_model=SeriesSettings)
def get_series_settings(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    return read_series_settings(session)


@router.patch("", status_code=204)
def update_series_settings(
    body: SeriesSettings,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    series_config.set_enabled(session, body.enabled)
    series_config.set_default_mode(session, body.default_mode)
    series_config.set_run_hour(session, body.run_hour)
    series_config.set_max_per_run(session, body.max_per_run)
    series_config.set_user(session, body.user)
    series_config.set_language_filter(session, body.language_filter)
    return Response(status_code=204)
