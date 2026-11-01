"""A small client for the REST API. The web app never touches the database itself."""

from __future__ import annotations

from typing import Any

import httpx
import streamlit as st

from wareneinsatz.config import get_settings


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class Api:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, client: httpx.Client | None = None):
        settings = get_settings()
        key = api_key or (settings.api_key.get_secret_value() if settings.api_key else None)
        headers = {"X-API-Key": key} if key else {}
        # Tests pass FastAPI's TestClient (an httpx.Client) that talks to the app without a network.
        self.http = client or httpx.Client(base_url=base_url or settings.api_url, headers=headers, timeout=120)

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.TransportError as error:
            raise ApiError(
                0, f"The API is not reachable ({error}). Start it with: uv run wareneinsatz serve"
            ) from error
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise ApiError(response.status_code, detail if isinstance(detail, str) else str(detail))
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response.content

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        return self.request("POST", path, json=json, **kwargs)

    def put(self, path: str, json: Any) -> Any:
        return self.request("PUT", path, json=json)


def api() -> Api:
    """One client per browser session; tests put their own into st.session_state['api']."""
    if "api" not in st.session_state:
        st.session_state["api"] = Api()
    return st.session_state["api"]
