from typing import Annotated

from fastapi import APIRouter, Depends, Form, Security
from sqlmodel import Session

from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.models import GroupEnum
from app.internal.series.config import SeriesMode
from app.routers.api.settings.series import SeriesSettings, read_series_settings
from app.routers.api.settings.series import (
    update_series_settings as api_update_series_settings,
)
from app.util.db import get_session
from app.util.templates import catalog_response, catalog_response_toast

router = APIRouter(prefix="/series")


@router.get("/hx-form")
def read_series(
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
):
    _ = admin_user
    return catalog_response(
        "Settings.Download.Series",
        settings=read_series_settings(session),
    )


@router.post("/hx-form")
def update_series(
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
    default_mode: Annotated[SeriesMode, Form()],
    run_hour: Annotated[int, Form(ge=0, le=23)],
    max_per_run: Annotated[int, Form(ge=0)],
    user: Annotated[str, Form(min_length=1, max_length=100)],
    enabled: Annotated[bool, Form()] = False,
    language_filter: Annotated[bool, Form()] = False,
):
    settings = SeriesSettings(
        enabled=enabled,
        default_mode=default_mode,
        run_hour=run_hour,
        max_per_run=max_per_run,
        user=user.strip(),
        language_filter=language_filter,
    )
    api_update_series_settings(settings, session, admin_user)
    return catalog_response_toast(
        "Settings.Download.Series",
        "Settings updated",
        "success",
        settings=settings,
    )
