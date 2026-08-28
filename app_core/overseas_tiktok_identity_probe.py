# -*- coding: utf-8 -*-
"""Non-persistent, value-free diagnostics for TikTok identity-source drift."""

from __future__ import annotations

from typing import Any, Mapping


_MAX_COUNT = 1000
_ROUTES = frozenset({"homepage", "auth", "other", "invalid"})
_APP_STATES = frozenset(
    {
        "missing",
        "invalid",
        "present_no_handle",
        "present_one",
        "present_multiple",
    }
)
_PATH_FAMILIES = frozenset(
    {
        "none",
        "app_context",
        "user_detail",
        "app_context_and_user_detail",
    }
)

_IDENTITY_STRUCTURE_EVALUATOR = r"""
() => {
  const MAX_COUNT = 1000;
  const cap = value => Math.min(MAX_COUNT, Math.max(0, Number(value) || 0));
  const empty = (route = 'invalid') => ({
    route,
    appContext: {state: 'missing', candidateCount: 0, unique: false},
    topRegion: {candidateCount: 0, unique: false},
    semanticControl: {controlCount: 0, candidateCount: 0, unique: false},
    rehydration: {pathFamily: 'none', familyCount: 0, candidateCount: 0, unique: false},
  });
  const publicHandle = value => {
    if (typeof value !== 'string') return '';
    const normalized = value.trim().replace(/^@/, '').toLowerCase();
    return /^[a-z0-9._-]{1,64}$/.test(normalized) ? normalized : '';
  };
  const handlesFromUser = user => {
    const values = new Set();
    if (!user || typeof user !== 'object' || Array.isArray(user)) return values;
    for (const field of ['uniqueId', 'unique_id']) {
      if (!Object.prototype.hasOwnProperty.call(user, field)) continue;
      const handle = publicHandle(user[field]);
      if (handle) values.add(handle);
    }
    return values;
  };
  const handleFromAnchor = anchor => {
    if (!anchor || typeof anchor.pathname !== 'string') return '';
    const match = anchor.pathname.match(/^\/@([A-Za-z0-9._-]{1,64})\/?$/);
    return match ? publicHandle(match[1]) : '';
  };
  const addAnchors = (root, target) => {
    if (!root || typeof root.querySelectorAll !== 'function') return;
    const anchors = [];
    if (typeof root.matches === 'function' && root.matches('a[href]')) anchors.push(root);
    root.querySelectorAll('a[href]').forEach(anchor => anchors.push(anchor));
    for (const anchor of anchors) {
      const handle = handleFromAnchor(anchor);
      if (handle) target.add(handle);
    }
  };
  try {
    const protocol = String(location.protocol || '').toLowerCase();
    const host = String(location.hostname || '').toLowerCase().replace(/\.$/, '');
    const path = String(location.pathname || '').toLowerCase();
    const isTikTok = protocol === 'https:' && (host === 'tiktok.com' || host === 'www.tiktok.com');
    const auth = isTikTok && ['login', 'challenge', 'verify', 'captcha', 'security']
      .some(token => path.includes(token));
    const route = auth ? 'auth' : (isTikTok && (path === '' || path === '/') ? 'homepage' : 'other');
    const result = empty(route);

    const topHandles = new Set();
    document.querySelectorAll('header, nav, aside').forEach(region => addAnchors(region, topHandles));
    result.topRegion.candidateCount = cap(topHandles.size);
    result.topRegion.unique = topHandles.size === 1;

    const semanticHandles = new Set();
    let semanticControlCount = 0;
    const semanticPattern = /profile|account|avatar/i;
    document.querySelectorAll('[aria-label], [data-e2e], [class]').forEach(control => {
      const metadata = [
        control.getAttribute('aria-label'),
        control.getAttribute('data-e2e'),
        control.getAttribute('class'),
      ].filter(value => typeof value === 'string').join(' ');
      if (!semanticPattern.test(metadata)) return;
      semanticControlCount += 1;
      const anchors = new Set();
      if (typeof control.matches === 'function' && control.matches('a[href]')) anchors.add(control);
      if (typeof control.closest === 'function') {
        const closest = control.closest('a[href]');
        if (closest) anchors.add(closest);
      }
      if (typeof control.querySelector === 'function') {
        const child = control.querySelector('a[href]');
        if (child) anchors.add(child);
      }
      for (const anchor of anchors) {
        const handle = handleFromAnchor(anchor);
        if (handle) semanticHandles.add(handle);
      }
    });
    result.semanticControl.controlCount = cap(semanticControlCount);
    result.semanticControl.candidateCount = cap(semanticHandles.size);
    result.semanticControl.unique = semanticHandles.size === 1;

    const scripts = document.querySelectorAll('script#__UNIVERSAL_DATA_FOR_REHYDRATION__');
    if (scripts.length === 0) return result;
    if (scripts.length !== 1) {
      result.appContext.state = 'invalid';
      return result;
    }
    let documentData;
    try {
      documentData = JSON.parse(scripts[0].textContent || '');
    } catch (_error) {
      result.appContext.state = 'invalid';
      return result;
    }
    const scope = documentData && documentData.__DEFAULT_SCOPE__;
    if (!scope || typeof scope !== 'object' || Array.isArray(scope)) {
      result.appContext.state = 'invalid';
      return result;
    }

    const familyHandles = new Set();
    let familyCount = 0;
    let hasAppContextFamily = false;
    let hasUserDetailFamily = false;
    const appContext = scope['webapp.app-context'];
    const appUser = appContext && appContext.user;
    if (appContext && typeof appContext === 'object' && !Array.isArray(appContext)) {
      hasAppContextFamily = true;
      familyCount += 1;
      if (!appUser || typeof appUser !== 'object' || Array.isArray(appUser)) {
        result.appContext.state = 'invalid';
      } else {
        const appHandles = handlesFromUser(appUser);
        appHandles.forEach(handle => familyHandles.add(handle));
        result.appContext.candidateCount = cap(appHandles.size);
        result.appContext.unique = appHandles.size === 1;
        result.appContext.state = appHandles.size === 0
          ? 'present_no_handle'
          : (appHandles.size === 1 ? 'present_one' : 'present_multiple');
      }
    }

    const userDetail = scope['webapp.user-detail'];
    const detailUser = userDetail && userDetail.userInfo && userDetail.userInfo.user;
    if (detailUser && typeof detailUser === 'object' && !Array.isArray(detailUser)) {
      hasUserDetailFamily = true;
      familyCount += 1;
      handlesFromUser(detailUser).forEach(handle => familyHandles.add(handle));
    }
    result.rehydration.pathFamily = hasAppContextFamily && hasUserDetailFamily
      ? 'app_context_and_user_detail'
      : (hasAppContextFamily ? 'app_context' : (hasUserDetailFamily ? 'user_detail' : 'none'));
    result.rehydration.familyCount = cap(familyCount);
    result.rehydration.candidateCount = cap(familyHandles.size);
    result.rehydration.unique = familyHandles.size === 1;
    return result;
  } catch (_error) {
    return empty('invalid');
  }
}
"""


def _safe_count(value: object) -> int:
    if type(value) is not int or value < 0:
        return 0
    return min(int(value), _MAX_COUNT)


def _safe_context(payload: object) -> dict[str, Any]:
    if type(payload) is not dict:
        payload = {}
    route = payload.get("route")
    if type(route) is not str or route not in _ROUTES:
        route = "invalid"

    app_raw = payload.get("appContext")
    if type(app_raw) is not dict:
        app_raw = {}
    app_state = app_raw.get("state")
    if type(app_state) is not str or app_state not in _APP_STATES:
        app_state = "invalid"
    app_count = _safe_count(app_raw.get("candidateCount"))

    top_raw = payload.get("topRegion")
    if type(top_raw) is not dict:
        top_raw = {}
    top_count = _safe_count(top_raw.get("candidateCount"))

    semantic_raw = payload.get("semanticControl")
    if type(semantic_raw) is not dict:
        semantic_raw = {}
    semantic_controls = _safe_count(semantic_raw.get("controlCount"))
    semantic_count = _safe_count(semantic_raw.get("candidateCount"))

    rehydration_raw = payload.get("rehydration")
    if type(rehydration_raw) is not dict:
        rehydration_raw = {}
    rehydration_families = _safe_count(rehydration_raw.get("familyCount"))
    rehydration_count = _safe_count(rehydration_raw.get("candidateCount"))
    path_family = rehydration_raw.get("pathFamily")
    if type(path_family) is not str or path_family not in _PATH_FAMILIES:
        path_family = "none"

    return {
        "route": route,
        "appContext": {
            "state": app_state,
            "candidateCount": app_count,
            "unique": app_count == 1,
        },
        "topRegion": {
            "candidateCount": top_count,
            "unique": top_count == 1,
        },
        "semanticControl": {
            "controlCount": semantic_controls,
            "candidateCount": semantic_count,
            "unique": semantic_count == 1,
        },
        "rehydration": {
            "pathFamily": path_family,
            "familyCount": rehydration_families,
            "candidateCount": rehydration_count,
            "unique": rehydration_count == 1,
        },
    }


async def probe_tiktok_identity_structure(page) -> dict[str, Any]:
    """Return only a fixed, value-free structure summary for one page."""

    try:
        raw = await page.evaluate(_IDENTITY_STRUCTURE_EVALUATOR)
    except Exception:
        raw = None
    return _safe_context(raw)


def _has_diagnostic_structure(context: Mapping[str, Any]) -> bool:
    if context.get("route") != "homepage":
        return False
    app = context.get("appContext") or {}
    if app.get("state") in {
        "invalid",
        "present_no_handle",
        "present_one",
        "present_multiple",
    }:
        return True
    top = context.get("topRegion") or {}
    semantic = context.get("semanticControl") or {}
    rehydration = context.get("rehydration") or {}
    return any(
        int(value or 0) > 0
        for value in (
            top.get("candidateCount"),
            semantic.get("controlCount"),
            semantic.get("candidateCount"),
            rehydration.get("familyCount"),
            rehydration.get("candidateCount"),
        )
    )


def combine_tiktok_identity_probes(
    first: object,
    second: object,
) -> dict[str, Any] | None:
    """Build the only in-memory pair schema; return None for no evidence."""

    first_safe = _safe_context(first)
    second_safe = _safe_context(second)
    if not (
        _has_diagnostic_structure(first_safe)
        and _has_diagnostic_structure(second_safe)
    ):
        return None
    return {
        "schemaVersion": 1,
        "first": first_safe,
        "second": second_safe,
        "consistent": first_safe == second_safe,
    }


def sanitize_tiktok_identity_probe(payload: object) -> dict[str, Any] | None:
    """Rebuild an untrusted pair without retaining unknown keys or values."""

    if type(payload) is not dict or payload.get("schemaVersion") != 1:
        return None
    return combine_tiktok_identity_probes(
        payload.get("first"),
        payload.get("second"),
    )


def summarize_tiktok_identity_probe(payload: object) -> str:
    """Render one fixed-label line containing only enums, counts and a bool."""

    safe = sanitize_tiktok_identity_probe(payload)
    if safe is None:
        return ""
    first = safe["first"]
    second = safe["second"]
    consistent = "true" if safe["consistent"] else "false"
    return (
        f"app={first['appContext']['state']}/{second['appContext']['state']};"
        f"top={first['topRegion']['candidateCount']}/{second['topRegion']['candidateCount']};"
        f"controls={first['semanticControl']['controlCount']}/{second['semanticControl']['controlCount']};"
        f"semantic={first['semanticControl']['candidateCount']}/{second['semanticControl']['candidateCount']};"
        f"paths={first['rehydration']['pathFamily']}/{second['rehydration']['pathFamily']};"
        f"families={first['rehydration']['familyCount']}/{second['rehydration']['familyCount']};"
        f"rehydration={first['rehydration']['candidateCount']}/{second['rehydration']['candidateCount']};"
        f"consistent={consistent}"
    )


__all__ = [
    "combine_tiktok_identity_probes",
    "probe_tiktok_identity_structure",
    "sanitize_tiktok_identity_probe",
    "summarize_tiktok_identity_probe",
]
