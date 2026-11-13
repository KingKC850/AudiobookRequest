from typing import Annotated

from fastapi import APIRouter, Depends, Form, Security
from sqlmodel import Session

from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.models import GroupEnum
from app.routers.api.settings.matching import (
    MatchingSettings,
    read_matching_settings,
)
from app.routers.api.settings.matching import (
    update_matching_settings as api_update_matching_settings,
)
from app.util.db import get_session
from app.util.templates import catalog_response, catalog_response_toast

router = APIRouter(prefix="/matching")


@router.get("/hx-form")
def read_matching(
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
):
    _ = admin_user
    return catalog_response(
        "Settings.Download.Matching",
        settings=read_matching_settings(session),
    )


@router.post("/hx-form")
def update_matching(
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
    strict: Annotated[bool, Form()] = False,
    require_narrator: Annotated[bool, Form()] = False,
    allow_unverified_narrator: Annotated[bool, Form()] = False,
    fallback_search: Annotated[bool, Form()] = False,
):
    settings = MatchingSettings(
        strict=strict,
        require_narrator=require_narrator,
        allow_unverified_narrator=allow_unverified_narrator,
        fallback_search=fallback_search,
    )
    api_update_matching_settings(settings, session, admin_user)
    return catalog_response_toast(
        "Settings.Download.Matching",
        "Settings updated",
        "success",
        settings=settings,
    )
