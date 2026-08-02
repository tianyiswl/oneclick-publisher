# -*- coding: utf-8 -*-
"""YouTube 官方 OAuth 与 Data API 适配器。"""

from .api import YouTubeApiClient, YouTubeApiError
from .capability import YouTubeCapability
from .credentials import (
    YOUTUBE_READONLY_SCOPE,
    YOUTUBE_REQUIRED_SCOPES,
    YOUTUBE_UPLOAD_SCOPE,
    OAuthClientConfig,
    TokenRecord,
    YouTubeCredentialStore,
)
from .oauth import YouTubeOAuthClient, YouTubeOAuthError

__all__ = [
    "OAuthClientConfig",
    "TokenRecord",
    "YOUTUBE_READONLY_SCOPE",
    "YOUTUBE_REQUIRED_SCOPES",
    "YOUTUBE_UPLOAD_SCOPE",
    "YouTubeApiClient",
    "YouTubeApiError",
    "YouTubeCapability",
    "YouTubeCredentialStore",
    "YouTubeOAuthClient",
    "YouTubeOAuthError",
]
