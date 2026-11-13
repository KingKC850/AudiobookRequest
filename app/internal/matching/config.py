from typing import Literal, final

from sqlmodel import Session

from app.util.cache import StringConfigCache

MatchConfigKey = Literal[
    "match_strict",
    "match_require_narrator",
    "match_allow_unverified_narrator",
    "match_fallback_search",
]


@final
class MatchConfig(StringConfigCache[MatchConfigKey]):
    """Settings for which releases an automatic download is allowed to grab."""

    def _bool(self, session: Session, key: MatchConfigKey, default: bool) -> bool:
        value = self.get_bool(session, key)
        return default if value is None else value

    def get_strict(self, session: Session) -> bool:
        """Only auto-download a release that names the full title and an author."""
        return self._bool(session, "match_strict", True)

    def set_strict(self, session: Session, strict: bool):
        self.set_bool(session, "match_strict", strict)

    def get_require_narrator(self, session: Session) -> bool:
        """Also require the release to name one of the requested narrators."""
        return self._bool(session, "match_require_narrator", False)

    def set_require_narrator(self, session: Session, require_narrator: bool):
        self.set_bool(session, "match_require_narrator", require_narrator)

    def get_allow_unverified_narrator(self, session: Session) -> bool:
        """Grab a title+author fallback hit even if the narrator is unknown."""
        return self._bool(session, "match_allow_unverified_narrator", False)

    def set_allow_unverified_narrator(self, session: Session, allow: bool):
        self.set_bool(session, "match_allow_unverified_narrator", allow)

    def get_fallback_search(self, session: Session) -> bool:
        """Retry Prowlarr with shorter queries when the full title finds nothing."""
        return self._bool(session, "match_fallback_search", True)

    def set_fallback_search(self, session: Session, fallback_search: bool):
        self.set_bool(session, "match_fallback_search", fallback_search)


match_config = MatchConfig()
