from typing import Annotated

from fastapi import APIRouter, Depends, Response, Security
from pydantic import BaseModel
from sqlmodel import Session

from app.internal.auth.authentication import AnyAuth, DetailedUser
from app.internal.matching.config import match_config
from app.internal.models import GroupEnum
from app.util.db import get_session

router = APIRouter(prefix="/matching")


class MatchingSettings(BaseModel):
    strict: bool
    require_narrator: bool
    allow_unverified_narrator: bool
    fallback_search: bool


def read_matching_settings(session: Session) -> MatchingSettings:
    return MatchingSettings(
        strict=match_config.get_strict(session),
        require_narrator=match_config.get_require_narrator(session),
        allow_unverified_narrator=match_config.get_allow_unverified_narrator(session),
        fallback_search=match_config.get_fallback_search(session),
    )


@router.get("", response_model=MatchingSettings)
def get_matching_settings(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    return read_matching_settings(session)


@router.patch("", status_code=204)
def update_matching_settings(
    body: MatchingSettings,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    match_config.set_strict(session, body.strict)
    match_config.set_require_narrator(session, body.require_narrator)
    match_config.set_allow_unverified_narrator(session, body.allow_unverified_narrator)
    match_config.set_fallback_search(session, body.fallback_search)
    return Response(status_code=204)
