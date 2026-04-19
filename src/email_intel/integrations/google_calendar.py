"""Google Calendar integration.

Uses the OAuth installed-app flow: on first run, opens a browser for consent
and caches a refresh token at `token_path`. Subsequent runs refresh silently.

If the client-secrets file is missing, `build_calendar_service` returns None
so the rest of the pipeline keeps working without calendar sync.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol, cast

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


class CalendarService(Protocol):
    """Narrow protocol of what we use from the googleapiclient Resource."""

    def insert_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]: ...


class GoogleCalendarClient:
    """Thin wrapper over googleapiclient. Only exposes insert_event for now."""

    def __init__(
        self,
        client_secrets_path: Path,
        token_path: Path,
        calendar_id: str = "primary",
    ) -> None:
        self._client_secrets_path = client_secrets_path
        self._token_path = token_path
        self._calendar_id = calendar_id
        self._service: Any | None = None

    def _credentials(self) -> Any:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]

        creds: Any | None = None
        if self._token_path.exists():
            creds = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
                str(self._token_path), SCOPES
            )

        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self._client_secrets_path), SCOPES
            )
            creds = flow.run_local_server(port=0)

        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    def _get_service(self) -> Any:
        if self._service is None:
            from googleapiclient.discovery import build  # type: ignore[import-untyped]

            creds = self._credentials()
            self._service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return self._service

    def insert_event(self, body: dict[str, Any]) -> dict[str, Any]:
        service = self._get_service()
        result = service.events().insert(calendarId=self._calendar_id, body=body).execute()
        return cast(dict[str, Any], result)


def build_calendar_client(
    client_secrets_path: Path,
    token_path: Path,
    calendar_id: str,
) -> GoogleCalendarClient | None:
    """Return a calendar client, or None if calendar is not configured.

    Missing client-secrets → skip cleanly. Other errors propagate.
    """
    if not client_secrets_path.exists():
        log.info(
            "Google Calendar disabled: client-secrets not found at %s", client_secrets_path
        )
        return None
    return GoogleCalendarClient(client_secrets_path, token_path, calendar_id)
