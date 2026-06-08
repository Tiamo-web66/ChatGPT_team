#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ChatGPT_team.py
================

Standalone single-file script for:

1. registering/logging into ChatGPT Web through the current enterprise SSO flow
2. completing Codex OAuth
3. saving the first usable Codex refresh token

This file is intentionally self-contained:

- no dependency on `chatgpt_register.py`
- no dependency on `codex_login_extract.py`
- no dependency on `hero_sms.py`
- no stock-upload / merchant-sync logic

Install
-------

```bash
python -m pip install --upgrade pip
python -m pip install curl_cffi
```

Minimal config
--------------

Create `ChatGPT_team.config.local.json` next to this file:

```json
{
  "proxy": "http://127.0.0.1:7890"
}
```

If you do not need a proxy, you can omit the file and run directly.

Sub2API auto-upload (optional)
------------------------------

After a successful registration, the script can automatically upload the
Codex refresh token to a Sub2API relay station via the Admin API:

```json
{
  "sub2api_auto_upload": true,
  "sub2api_admin_api_key": "sk-admin-xxxxxxxx",
  "sub2api_base_url": "https://your-sub2api.example.com",
  "sub2api_group_names": ["ChatGPT主号池", "Claude中转站"],
  "sub2api_platform": "anthropic",
  "sub2api_upstream_base_url": "https://api.anthropic.com",
  "sub2api_concurrency": 1,
  "sub2api_priority": 1,
  "sub2api_pool_mode": "random",
  "sub2api_pool_mode_retry_count": 3
}
```

- ``sub2api_auto_upload``: set to ``true`` to enable auto-upload.
- ``sub2api_admin_api_key``: the Admin API Key from Sub2API settings.
- ``sub2api_base_url``: origin only, no trailing ``/admin`` or ``/api/v1``.
- ``sub2api_group_names``: array of group **names** (not IDs). On startup the script
  queries ``GET /api/v1/admin/groups``, prints all available groups with their
  IDs, and resolves the configured names to IDs automatically.
- Upload failures are logged but do **not** fail the registration.

Output format (optional)
------------------------

By default, the script saves CPA-format token files to ``codex_tokens/``.
You can switch to Sub2API format (or output both):

```json
{
  "output_format": "sub2api",
  "sub2api_output_dir": "sub2api_tokens"
}
```

- ``output_format``: ``"cpa"`` (default), ``"sub2api"``, or ``"both"``.
- When ``sub2api_auto_upload`` is enabled, Sub2API format is **always** generated
  regardless of this setting.
- Sub2API conversion follows the CPA2sub2API spec: JWT parsed ``access_token``
  and ``id_token`` to extract email, plan_type, organization_id, and expiry.

Usage

Usage
-----

Register and save RT:

```bash
python ChatGPT_team.py --total 1 --workers 1
```

Check existing `codex_tokens/*.json` plan type using current `access_token`:

```bash
python ChatGPT_team.py --check-tokens
```

When `--check-tokens` sees a `401`, it will:

1. load the matching local session from `chatgpt_sessions/`
2. run Codex OAuth again
3. overwrite the original token file with the fresh RT/AT/ID token

Outputs
-------

- `registered_only.txt`
- `register_only_failed.txt`
- `chatgpt_sessions/`
- `codex_tokens/`
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import random
import re
import secrets
import select
import socket
import socketserver
import string
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
import urllib.request
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

from curl_cffi import requests as curl_requests

try:
    from curl_cffi.const import CurlHttpVersion
except Exception:
    CurlHttpVersion = None


CHATGPT_BASE = "https://chatgpt.com"
AUTH_BASE = "https://auth.openai.com"
WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_REDIRECT_URI = "http://localhost:1455/auth/callback"
OAUTH_SCOPE = "openid profile email offline_access api.connectors.read api.connectors.invoke"
OAUTH_ORIGINATOR = "codex_vscode"
EMAIL_DOMAIN = "@gpt.edu.sixoner.com"
REGISTERED_OUTPUT_FILE = "registered_only.txt"
FAILED_OUTPUT_FILE = "register_only_failed.txt"
CHATGPT_SESSION_DIR = "chatgpt_sessions"
CODEX_TOKEN_DIR = "codex_tokens"
DEFAULT_ONBOARDING_ROLE = "engineering"

_CONFIG_CANDIDATES = (
    "ChatGPT_team.config.local.json",
    "ChatGPT_team.config.json",
)

_print_lock = threading.Lock()
_file_lock = threading.Lock()
_PROXY_REGIONS = ["JP"]
_CONFIG_CACHE: list[dict[str, Any]] | None = None
QUIET_LOGS = True

# ---- Web 桥接回调（由 web_server.py 注入，CLI 模式不设置） ----
_LOG_CALLBACK: Callable[[str, str, str], None] | None = None
_PROGRESS_CALLBACK: Callable[[int, int, int, int], None] | None = None  # (success, fail, submitted, total)


def _startup_log(level: str, tag: str, message: str) -> None:
    with _print_lock:
        print(f"{datetime.now().strftime('%H:%M:%S')} | {level:<6} | {tag:<12} | {message}", flush=True)


def _load_proxy_from_config() -> str:
    for data in _iter_local_config_dicts():
        proxy = str(data.get("proxy") or "").strip()
        if proxy:
            return proxy
    return ""


def _config_is_dynamic() -> bool:
    for data in _iter_local_config_dicts():
        pt = str(data.get("proxy_type") or "static").strip().lower()
        if pt == "dynamic":
            return True
    return False


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    """解析 JWT token 的 payload 部分（不验证签名）

    参考 CPA2sub2API converter.mjs 的 parseJwtPayload()
    """
    if not token or not isinstance(token, str) or token.strip() == "":
        return {}
    segments = token.split(".")
    if len(segments) < 2:
        return {}
    try:
        payload = segments[1]
        payload = payload.replace("-", "+").replace("_", "/")
        padding = (4 - len(payload) % 4) % 4
        payload += "=" * padding
        decoded = base64.b64decode(payload).decode("utf-8", errors="replace")
        data = json.loads(decoded)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _to_email_key(email: str) -> str:
    """将邮箱转换为安全的文件名字符串"""
    if not email or not isinstance(email, str):
        return ""
    import re as _re
    key = email.strip().lower()
    key = _re.sub(r"[^a-z0-9]+", "_", key)
    key = key.strip("_")
    return key


def _strip_none_values(obj: Any) -> Any:
    """递归删除 dict 中值为 None 的键（对应 converter.mjs 的 stripUnavailable）"""
    if isinstance(obj, dict):
        result: dict[str, Any] = {}
        for k, v in obj.items():
            stripped = _strip_none_values(v)
            if stripped is not None and stripped != "":
                result[k] = stripped
        return result if result else {}
    if isinstance(obj, list):
        result = [_strip_none_values(item) for item in obj]
        result = [item for item in result if item is not None and item != ""]
        return result
    if obj is None or obj == "":
        return None
    return obj


def _derive_organization_id(access_jwt: dict[str, Any], id_jwt: dict[str, Any]) -> str | None:
    """从 JWT 的 https://api.openai.com/auth 中提取 organization_id

    参考 converter.mjs 的 deriveOrganizationId()
    优先选 is_default=true 的，否则选第一个有 id 的。
    """
    auth_access = access_jwt.get("https://api.openai.com/auth") if isinstance(access_jwt, dict) else None
    auth_id = id_jwt.get("https://api.openai.com/auth") if isinstance(id_jwt, dict) else None

    for auth in (auth_id, auth_access):
        if not isinstance(auth, dict):
            continue
        orgs = auth.get("organizations")
        if not isinstance(orgs, list):
            continue
        # 优先 is_default
        for org in orgs:
            if isinstance(org, dict) and org.get("is_default") and org.get("id"):
                return str(org["id"])
        # 回退到第一个有 id 的
        for org in orgs:
            if isinstance(org, dict) and org.get("id"):
                return str(org["id"])

    return None


def _convert_cpa_to_sub2api(cpa_record: dict[str, Any]) -> dict[str, Any]:
    """将单个 CPA 记录转换为 Sub2API 格式

    参考 https://github.com/gtxx3600/CPA2sub2API 的 converter.mjs convertCPARecord()
    """
    now = datetime.now(timezone.utc)
    email = str(cpa_record.get("email") or "").strip()
    access_token = str(cpa_record.get("access_token") or "").strip()
    id_token = str(cpa_record.get("id_token") or "").strip()
    refresh_token = str(cpa_record.get("refresh_token") or "").strip()
    saved_at = str(cpa_record.get("saved_at") or now.isoformat()).strip()

    # 解析 JWT
    access_jwt = _decode_jwt_payload(access_token)
    id_jwt = _decode_jwt_payload(id_token)

    # 从 access_token JWT 提取 exp
    exp_value = access_jwt.get("exp")
    expires_at: str | None = None
    expires_in: int | None = None
    if isinstance(exp_value, (int, float)) and exp_value > 0:
        exp_timestamp = int(exp_value) if int(exp_value) > 1e11 else int(exp_value) * 1000
        expires_at = datetime.fromtimestamp(exp_timestamp / 1000, tz=timezone.utc).isoformat()
        expires_in = max(0, int(exp_timestamp / 1000 - now.timestamp()))

    # 从 JWT 提取 OpenAI auth/profile 信息
    auth_access = access_jwt.get("https://api.openai.com/auth") if isinstance(access_jwt, dict) else None
    auth_id = id_jwt.get("https://api.openai.com/auth") if isinstance(id_jwt, dict) else None
    profile = access_jwt.get("https://api.openai.com/profile") if isinstance(access_jwt, dict) else None

    # 提取 email（优先 profile 的 email）
    jwt_email = ""
    if isinstance(profile, dict):
        jwt_email = str(profile.get("email") or "").strip()
    if not jwt_email:
        jwt_email = str(access_jwt.get("email") or "").strip()
    if not jwt_email:
        jwt_email = str(id_jwt.get("email") or "").strip()
    effective_email = email or jwt_email

    # 提取 plan_type
    plan_type: str | None = None
    for auth in (auth_access, auth_id):
        if isinstance(auth, dict) and auth.get("chatgpt_plan_type"):
            plan_type = str(auth["chatgpt_plan_type"])
            break

    # 提取 chatgpt_account_id / chatgpt_user_id
    chatgpt_account_id: str | None = None
    chatgpt_user_id: str | None = None
    for auth in (auth_access, auth_id):
        if isinstance(auth, dict):
            if not chatgpt_account_id and auth.get("chatgpt_account_id"):
                chatgpt_account_id = str(auth["chatgpt_account_id"])
            if not chatgpt_user_id and (auth.get("chatgpt_user_id") or auth.get("user_id")):
                chatgpt_user_id = str(auth.get("chatgpt_user_id") or auth.get("user_id"))

    # 提取 organization_id
    organization_id = _derive_organization_id(access_jwt, id_jwt)

    # 构建 credentials
    credentials: dict[str, Any] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
    }
    if effective_email:
        credentials["email"] = effective_email
    if expires_at:
        credentials["expires_at"] = expires_at
    if expires_in is not None:
        credentials["expires_in"] = expires_in
    if plan_type:
        credentials["plan_type"] = plan_type
    if organization_id:
        credentials["organization_id"] = organization_id
    if chatgpt_account_id:
        credentials["chatgpt_account_id"] = chatgpt_account_id
    if chatgpt_user_id:
        credentials["chatgpt_user_id"] = chatgpt_user_id

    # 构建 extra
    extra: dict[str, Any] = {}
    if effective_email:
        extra["email"] = effective_email
        extra["email_key"] = _to_email_key(effective_email)
    extra["last_refresh"] = saved_at

    # 构建 account
    account: dict[str, Any] = {
        "name": effective_email or "unknown",
        "platform": "openai",
        "type": "oauth",
        "concurrency": 10,
        "priority": 1,
        "credentials": _strip_none_values(credentials) or {},
        "extra": _strip_none_values(extra) or {},
    }

    # 构建完整文档
    document: dict[str, Any] = {
        "exported_at": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z",
        "proxies": [],
        "accounts": [account],
    }

    return document


def _save_sub2api_output(email: str, sub2api_doc: dict[str, Any], output_dir: str) -> Path:
    """保存单个 Sub2API 格式文件

    文件名: {email_key}.sub2api.json
    """
    email_key = _to_email_key(email) or "unknown"
    dir_path = _cwd_path(output_dir)
    file_path = dir_path / f"{email_key}.sub2api.json"
    _save_json(file_path, sub2api_doc)
    return file_path


def _get_output_format_config() -> dict[str, Any]:
    """读取输出格式配置"""
    merged: dict[str, Any] = {}
    for data in _iter_local_config_dicts():
        merged.update(data)
    fmt = str(merged.get("output_format") or "cpa").strip().lower()
    auto_upload = str(merged.get("sub2api_auto_upload") or "").lower() in {"1", "true", "yes", "on"}
    return {
        "format": fmt,
        "sub2api_output_dir": str(merged.get("sub2api_output_dir") or "sub2api_tokens").strip(),
        # 如果开启了自动上传，强制生成 Sub2API 格式
        "generate_sub2api": auto_upload or fmt in ("sub2api", "both"),
    }


# ---- sub2api 分组名称→ID 解析缓存 ----
_RESOLVED_SUB2API_GROUP_IDS: list[int] | None = None
_SUB2API_GROUP_CACHE: list[dict[str, Any]] = []


def _get_sub2api_config() -> dict[str, Any]:
    """从配置文件读取 sub2api 自动上传相关设置"""
    merged: dict[str, Any] = {}
    for data in _iter_local_config_dicts():
        merged.update(data)
    return {
        "enabled": str(merged.get("sub2api_auto_upload") or "").lower() in {"1", "true", "yes", "on"},
        "admin_api_key": str(merged.get("sub2api_admin_api_key") or "").strip(),
        "base_url": str(merged.get("sub2api_base_url") or "").strip().rstrip("/"),
        "group_names": merged.get("sub2api_group_names") or [],
        "group_ids": merged.get("sub2api_group_ids") or [],
        "platform": str(merged.get("sub2api_platform") or "anthropic").strip(),
        "upstream_base_url": str(merged.get("sub2api_upstream_base_url") or "").strip(),
        "concurrency": int(merged.get("sub2api_concurrency") or 1),
        "priority": int(merged.get("sub2api_priority") or 1),
        "pool_mode": str(merged.get("sub2api_pool_mode") or "random").strip(),
        "pool_mode_retry_count": int(merged.get("sub2api_pool_mode_retry_count") or 3),
    }


def _fetch_sub2api_groups(base_url: str, admin_key: str) -> list[dict[str, Any]]:
    """调用 Sub2API Admin API 获取所有分组列表

    返回分组列表，每项至少包含 id, name, platform 字段。
    """
    url = f"{base_url}/api/v1/admin/groups"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "x-api-key": admin_key,
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        if isinstance(data, dict):
            items = data.get("data") or data.get("items") or data.get("groups") or []
            if isinstance(items, list):
                return items
            if isinstance(data.get("data"), dict):
                items = data["data"].get("items") or data["data"].get("groups") or []
                if isinstance(items, list):
                    return items
        if isinstance(data, list):
            return data
        return []
    except Exception as exc:
        _startup_log("WARN", "Sub2API", f"获取分组列表失败: {type(exc).__name__}: {exc}")
        return []


def _resolve_sub2api_group_ids(config: dict[str, Any]) -> list[int]:
    """根据配置中的分组名称，查询 Sub2API 并解析为分组 ID 列表

    优先使用 sub2api_group_names（名称），其次使用 sub2api_group_ids（ID）。
    名称匹配不区分大小写。
    """
    global _RESOLVED_SUB2API_GROUP_IDS, _SUB2API_GROUP_CACHE

    if _RESOLVED_SUB2API_GROUP_IDS is not None:
        return _RESOLVED_SUB2API_GROUP_IDS

    group_names: list[str] = [str(n).strip() for n in (config.get("group_names") or []) if str(n).strip()]
    group_ids: list[int] = [int(i) for i in (config.get("group_ids") or []) if i]

    # 如果配置了名称，查询 API 解析
    if group_names:
        base_url = config["base_url"]
        admin_key = config["admin_api_key"]
        if not base_url or not admin_key:
            _startup_log("WARN", "Sub2API", "sub2api_base_url 或 sub2api_admin_api_key 未配置，无法查询分组")
            _RESOLVED_SUB2API_GROUP_IDS = []
            return []

        groups = _fetch_sub2api_groups(base_url, admin_key)
        _SUB2API_GROUP_CACHE = groups

        if not groups:
            _startup_log("WARN", "Sub2API", "未查询到任何分组，请检查 sub2api 后台是否已创建分组")
            _RESOLVED_SUB2API_GROUP_IDS = []
            return []

        # 打印所有可用分组
        _startup_log("INFO", "Sub2API", f"查询到 {len(groups)} 个分组:")
        for g in groups:
            gid = g.get("id", "?")
            gname = g.get("name", "?")
            gplatform = g.get("platform", "?")
            _startup_log("INFO", "Sub2API", f"  ID={gid}  名称=\"{gname}\"  平台={gplatform}")

        # 名称 → ID 映射（不区分大小写）
        name_to_id: dict[str, int] = {}
        for g in groups:
            gid = g.get("id")
            gname = str(g.get("name") or "").strip().lower()
            if gid is not None and gname:
                name_to_id[gname] = int(gid)

        resolved: list[int] = []
        for name in group_names:
            name_lower = name.lower()
            if name_lower in name_to_id:
                resolved.append(name_to_id[name_lower])
                _startup_log("INFO", "Sub2API", f"分组 \"{name}\" → ID={name_to_id[name_lower]}")
            else:
                available = [str(g.get("name") or "") for g in groups]
                _startup_log("WARN", "Sub2API", f"未找到名为 \"{name}\" 的分组，可用: {available}")

        if resolved:
            _RESOLVED_SUB2API_GROUP_IDS = resolved
            return resolved
        else:
            _startup_log("WARN", "Sub2API", "所有配置的分组名称均未匹配到，将不会绑定任何分组")
            _RESOLVED_SUB2API_GROUP_IDS = []
            return []

    # 没有配置名称，回退到直接使用 group_ids
    if group_ids:
        _RESOLVED_SUB2API_GROUP_IDS = group_ids
        return group_ids

    _RESOLVED_SUB2API_GROUP_IDS = []
    return []


def _get_effective_group_ids(config: dict[str, Any]) -> list[int]:
    """返回当前有效的分组 ID 列表（使用缓存结果）"""
    global _RESOLVED_SUB2API_GROUP_IDS
    if _RESOLVED_SUB2API_GROUP_IDS is not None:
        return _RESOLVED_SUB2API_GROUP_IDS
    return _resolve_sub2api_group_ids(config)


def _upload_account_to_sub2api(
    *,
    name: str,
    refresh_token: str,
    config: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """调用 Sub2API Admin API 上传单个上游账号并绑定分组"""
    if config is None:
        config = _get_sub2api_config()

    if not config["enabled"]:
        return False, "sub2api auto upload disabled"

    base_url = config["base_url"]
    admin_key = config["admin_api_key"]
    if not base_url or not admin_key:
        return False, "sub2api config incomplete (missing base_url or admin_api_key)"

    group_ids = _get_effective_group_ids(config)
    if not group_ids:
        return False, "sub2api no valid group IDs resolved (check sub2api_group_names in config)"

    credentials: dict[str, Any] = {
        "api_key": refresh_token,
    }
    upstream = config["upstream_base_url"]
    if upstream:
        credentials["base_url"] = upstream
    if config["pool_mode"]:
        credentials["pool_mode"] = config["pool_mode"]
    if config["pool_mode_retry_count"]:
        credentials["pool_mode_retry_count"] = config["pool_mode_retry_count"]

    body = {
        "name": name,
        "platform": config["platform"],
        "type": "apikey",
        "credentials": credentials,
        "group_ids": group_ids,
        "concurrency": config["concurrency"],
        "priority": config["priority"],
    }

    idempotency_key = str(uuid.uuid4())
    url = f"{base_url}/api/v1/admin/accounts"

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": admin_key,
                "Idempotency-Key": idempotency_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.getcode()
            raw = resp.read().decode("utf-8", errors="replace")
        if 200 <= status < 300:
            return True, f"sub2api upload OK status={status}"
        else:
            return False, f"sub2api upload failed status={status}: {_short(raw, 200)}"
    except urllib.error.HTTPError as exc:
        try:
            body_text = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body_text = ""
        return False, f"sub2api HTTP {exc.code}: {_short(body_text, 200)}"
    except Exception as exc:
        return False, f"sub2api upload exception: {type(exc).__name__}: {exc}"


def _iter_local_config_dicts() -> list[dict[str, Any]]:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    candidates: list[Path] = []
    cwd = Path.cwd()
    script_root = Path(__file__).resolve().parent
    for candidate in _CONFIG_CANDIDATES:
        candidates.append(cwd / candidate)
        if script_root != cwd:
            candidates.append(script_root / candidate)
    seen: set[Path] = set()
    out: list[dict[str, Any]] = []
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            _startup_log("WARN", "Config", f"配置读取失败 path={path} err={type(exc).__name__}: {exc}")
            continue
        if not isinstance(data, dict):
            _startup_log("WARN", "Config", f"配置格式无效 path={path}，需要 JSON object")
            continue
        _startup_log("INFO", "Config", f"已加载配置 path={path}")
        out.append(data)
    _CONFIG_CACHE = out
    return out


DEFAULT_PROXY = os.environ.get("PROXY", _load_proxy_from_config()).strip()


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _short(value: Any, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _random_local(name: str = "") -> str:
    letters = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
    if len(letters) >= 6:
        base = letters[:12]
    else:
        alphabet = "abcdefghijklmnopqrstuvwxyz"
        base = "".join(random.choice(alphabet) for _ in range(10))
    suffix = str(int(time.time() * 1000))[-4:]
    return f"{base[:12]}{suffix}"


def _mask_proxy_url(raw: str) -> str:
    original = str(raw or "").strip()
    if not original:
        return ""
    if "://" not in original:
        parts = original.split(":")
        if len(parts) >= 4:
            host = parts[0].strip()
            port = parts[1].strip()
            return f"http://***:***@{host}:{port}"
    text = original
    if "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    if parsed.username or parsed.password:
        return f"{parsed.scheme}://***:***@{host}{port}"
    return f"{parsed.scheme}://{host}{port}"


def _randomize_proxy_region(template: str, region: str | None = None) -> str:
    text = str(template or "").strip()
    if not text:
        return text
    selected = str(region or random.choice(_PROXY_REGIONS)).strip().upper() or "JP"
    return re.sub(r"region-([A-Z]{2})", f"region-{selected}", text, flags=re.I)


def _normalize_proxy_url(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    return text


@dataclass(frozen=True)
class ParsedProxy:
    raw: str
    scheme: str
    host: str
    port: int
    auth_header: str = ""


@dataclass
class ProxyChainInstance:
    local_proxy_url: str
    listen_host: str
    listen_port: int
    main_proxy: str
    upstream_proxy: str
    _server: socketserver.TCPServer
    _thread: threading.Thread

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._server.shutdown()
        with contextlib.suppress(Exception):
            self._server.server_close()
        with contextlib.suppress(Exception):
            self._thread.join(timeout=5.0)


def _parse_proxy_url(raw: str, *, default_port: int = 8080) -> ParsedProxy:
    original = str(raw or "").strip()
    if not original:
        raise ValueError("proxy url is empty")

    # Support legacy lajiao format:
    #   host:port:username:password
    # or direct host:port:user-region-JP-sid-{session}-t-5:pass
    if "://" not in original:
        parts = original.split(":")
        if len(parts) >= 4:
            host = parts[0].strip()
            port = int(parts[1].strip())
            user = parts[2].strip()
            password = ":".join(parts[3:]).strip()
            token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
            return ParsedProxy(
                raw=f"http://{host}:{port}",
                scheme="http",
                host=host,
                port=port,
                auth_header=f"Proxy-Authorization: Basic {token}\r\n",
            )

    text = _normalize_proxy_url(original)
    parsed = urlparse(text)
    scheme = (parsed.scheme or "http").lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"unsupported proxy scheme: {scheme}")
    host = parsed.hostname or ""
    if not host:
        raise ValueError(f"invalid proxy url: {raw}")
    port = int(parsed.port or (443 if scheme == "https" else default_port))
    auth_header = ""
    if parsed.username is not None:
        user = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        auth_header = f"Proxy-Authorization: Basic {token}\r\n"
    return ParsedProxy(raw=text, scheme=scheme, host=host, port=port, auth_header=auth_header)


def _read_until(sock: socket.socket, marker: bytes = b"\r\n\r\n", *, max_bytes: int = 65536) -> bytes:
    data = bytearray()
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > max_bytes:
            raise OSError("proxy header too large")
    return bytes(data)


def _connect_tcp(host: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((host, port), timeout=timeout)


def _connect_to_upstream_proxy(server: "_ProxyChainServer") -> socket.socket:
    upstream = server.upstream_proxy
    main = server.main_proxy
    timeout = server.connect_timeout
    if main is None:
        s = _connect_tcp(upstream.host, upstream.port, timeout)
        s.settimeout(timeout)
        return s
    s = _connect_tcp(main.host, main.port, timeout)
    s.settimeout(timeout)
    connect_req = (
        f"CONNECT {upstream.host}:{upstream.port} HTTP/1.1\r\n"
        f"Host: {upstream.host}:{upstream.port}\r\n"
        f"Proxy-Connection: Keep-Alive\r\n"
        f"{main.auth_header}"
        f"\r\n"
    ).encode("iso-8859-1")
    s.sendall(connect_req)
    resp = _read_until(s)
    first = resp.split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
    if " 200 " not in f" {first} " and " 2" not in f" {first[:12]}":
        with contextlib.suppress(Exception):
            s.close()
        raise OSError(f"main proxy CONNECT upstream failed: {first}")
    return s


def _strip_proxy_headers(header_lines: list[str]) -> list[str]:
    skipped = {"proxy-authorization", "proxy-connection"}
    out: list[str] = []
    for line in header_lines:
        name = line.split(":", 1)[0].strip().lower() if ":" in line else ""
        if name in skipped:
            continue
        out.append(line)
    return out


def _relay(a: socket.socket, b: socket.socket) -> None:
    for s in (a, b):
        with contextlib.suppress(Exception):
            s.settimeout(None)
    sockets = [a, b]
    while True:
        try:
            readable, _, _ = select.select(sockets, [], [], 180)
        except Exception:
            break
        if not readable:
            break
        for src in readable:
            dst = b if src is a else a
            try:
                data = src.recv(65536)
                if not data:
                    return
                dst.sendall(data)
            except Exception:
                return


class _ProxyChainServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True
    main_proxy: ParsedProxy | None
    upstream_proxy: ParsedProxy
    connect_timeout: float
    logger: Any


class _ProxyChainHandler(socketserver.BaseRequestHandler):
    request: socket.socket
    server: _ProxyChainServer

    def _client_error(self, status: str, message: str = "") -> None:
        body = (message or status).encode("utf-8", errors="ignore")
        with contextlib.suppress(Exception):
            self.request.sendall(
                f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("iso-8859-1") + body
            )

    def handle(self) -> None:
        upstream_sock: socket.socket | None = None
        try:
            self.request.settimeout(self.server.connect_timeout)
            initial = _read_until(self.request)
            if not initial:
                return
            header_bytes, _, rest = initial.partition(b"\r\n\r\n")
            header_text = header_bytes.decode("iso-8859-1", errors="replace")
            lines = header_text.split("\r\n")
            if not lines or " " not in lines[0]:
                self._client_error("400 Bad Request", "invalid proxy request")
                return
            first_line = lines[0]
            method, target, *_ = first_line.split(" ", 2)
            method = method.upper()
            upstream_sock = _connect_to_upstream_proxy(self.server)
            upstream = self.server.upstream_proxy
            if method == "CONNECT":
                req = (
                    f"CONNECT {target} HTTP/1.1\r\n"
                    f"Host: {target}\r\n"
                    f"Proxy-Connection: Keep-Alive\r\n"
                    f"{upstream.auth_header}"
                    f"\r\n"
                ).encode("iso-8859-1")
                upstream_sock.sendall(req)
                resp = _read_until(upstream_sock)
                first = resp.split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
                if " 200 " not in f" {first} " and " 2" not in f" {first[:12]}":
                    self._client_error("502 Bad Gateway", f"upstream proxy CONNECT target failed: {first}")
                    return
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\nProxy-Agent: local-chain\r\n\r\n")
                if rest:
                    upstream_sock.sendall(rest)
                _relay(self.request, upstream_sock)
                return
            clean_headers = _strip_proxy_headers(lines[1:])
            outbound = ("\r\n".join([first_line, *clean_headers]) + "\r\n" + upstream.auth_header + "\r\n").encode("iso-8859-1") + rest
            upstream_sock.sendall(outbound)
            _relay(self.request, upstream_sock)
        except Exception as exc:
            logger = getattr(self.server, "logger", None)
            if logger:
                with contextlib.suppress(Exception):
                    logger(f"链式代理连接失败: {exc}")
            self._client_error("502 Bad Gateway", str(exc))
        finally:
            with contextlib.suppress(Exception):
                if upstream_sock is not None:
                    upstream_sock.close()


def _start_proxy_chain(*, main_proxy: str = "", upstream_proxy: str, listen_host: str = "127.0.0.1", listen_port: int = 0, connect_timeout: float = 20.0, logger: Any = None) -> ProxyChainInstance:
    upstream_proxy = _randomize_proxy_region(upstream_proxy, "JP")
    upstream = _parse_proxy_url(upstream_proxy, default_port=8080)
    main = _parse_proxy_url(main_proxy, default_port=8080) if str(main_proxy or "").strip() else None
    server = _ProxyChainServer((listen_host, int(listen_port or 0)), _ProxyChainHandler)
    server.main_proxy = main
    server.upstream_proxy = upstream
    server.connect_timeout = float(connect_timeout or 20.0)
    server.logger = logger
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, name=f"proxy-chain-{port}", daemon=True)
    thread.start()
    inst = ProxyChainInstance(
        local_proxy_url=f"http://{host}:{port}",
        listen_host=str(host),
        listen_port=int(port),
        main_proxy=main.raw if main else "",
        upstream_proxy=upstream.raw,
        _server=server,
        _thread=thread,
    )
    if logger:
        logger(f"链式代理已启动 local={inst.local_proxy_url} main={_mask_proxy_url(main_proxy) or 'DIRECT'} upstream={_mask_proxy_url(upstream_proxy)}")
    return inst


def _fetch_proxy_from_api(api_url: str, *, timeout: int = 15) -> str:
    """调用代理提取 API，返回真实代理地址（自动补全 http://）

    校验返回值是否为合法的 ip:port 格式，防止 API 返回错误消息（如白名单提示）被误用。
    """
    api_url = str(api_url or "").strip()
    if not api_url:
        raise ValueError("proxy_api_url 为空")
    req = urllib.request.Request(api_url, headers={"User-Agent": "curl/8.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace").strip()
    if not raw:
        raise ValueError("代理 API 返回空内容")
    # 纯 ip:port → 补全为 http://ip:port
    if "://" not in raw:
        raw = f"http://{raw}"

    # ---- 校验返回值是否为合法代理地址 ----
    parsed = urlparse(raw)
    host = (parsed.hostname or "").strip()
    port = parsed.port
    if not host or not port:
        raise ValueError(
            f"代理 API 返回了无效的代理地址: \"{raw}\"。"
            f"请检查: 1) API 白名单是否包含当前 IP "
            f"2) 代理余额是否充足 "
            f"3) API URL 参数是否正确"
        )
    # 校验 host 部分看起来像 IP 地址
    ip_pattern = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
    if not ip_pattern.match(host):
        raise ValueError(
            f"代理 API 返回的不是合法 IP 地址: \"{raw}\"。"
            f"返回内容可能为错误消息（如白名单/余额不足提示），请检查 API 配置"
        )
    # ---- 校验结束 ----

    return raw


def _prepare_proxy_for_account(proxy: str | None, tag: str) -> tuple[str | None, ProxyChainInstance | None]:
    merged: dict[str, Any] = {}
    for item in _iter_local_config_dicts():
        merged.update(item)

    # --- 动态代理：从 API 提取真实代理 IP ---
    proxy_type = str(merged.get("proxy_type") or "static").strip().lower()
    if proxy_type == "dynamic":
        api_url = str(merged.get("proxy_api_url") or "").strip()
        api_retry = max(1, int(merged.get("proxy_api_retry") or 3))
        if not api_url:
            raise RuntimeError("proxy_type=dynamic 但 proxy_api_url 未配置")
        last_err = None
        for retry_idx in range(api_retry):
            try:
                fetched = _fetch_proxy_from_api(api_url)
                proxy = fetched
                _print_pipe("INFO", "Proxy", f"账号={tag} 动态代理获取成功 retry={retry_idx + 1}/{api_retry} proxy={_mask_proxy_url(fetched)}")
                break
            except Exception as exc:
                last_err = exc
                if retry_idx < api_retry - 1:
                    time.sleep((retry_idx + 1) * 1.0)
        else:
            raise RuntimeError(f"动态代理获取失败（已重试 {api_retry} 次）: {last_err}")
    # --- 动态代理结束 ---

    chain_enabled_raw = os.environ.get("PROXY_CHAIN_ENABLED", str(merged.get("proxy_chain_enabled") or "")).strip().lower()
    chain_upstream = os.environ.get("PROXY_CHAIN_UPSTREAM_PROXY", str(merged.get("proxy_chain_upstream_proxy") or "")).strip()
    dynamic_raw = os.environ.get("PROXY_CHAIN_DYNAMIC_PER_ACCOUNT", str(merged.get("proxy_chain_dynamic_per_account") or "")).strip().lower()
    chain_enabled = chain_enabled_raw in {"1", "true", "yes", "on"}
    dynamic_per_account = dynamic_raw in {"1", "true", "yes", "on"}
    if chain_enabled and dynamic_per_account and chain_upstream:
        session_token = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(8))
        upstream = chain_upstream.replace("{session}", session_token)
        _print_pipe("INFO", "Proxy", f"账号={tag} 已生成 粘性二级代理 session={session_token} proxy={_mask_proxy_url(upstream)}")
        chain = _start_proxy_chain(
            main_proxy=str(proxy or DEFAULT_PROXY or "").strip(),
            upstream_proxy=upstream,
            listen_host="127.0.0.1",
            listen_port=0,
            connect_timeout=20.0,
            logger=lambda msg: _print_pipe("INFO", "Proxy", f"账号={tag} {msg}"),
        )
        return chain.local_proxy_url, chain
    return (str(proxy or DEFAULT_PROXY or "").strip() or None), None


def _print_pipe(level: str, tag: str, message: str) -> None:
    # 回调始终触发（无论 QUIET_LOGS 是否过滤），供 Web 端实时推送
    if _LOG_CALLBACK is not None:
        try:
            _LOG_CALLBACK(level, tag, message)
        except Exception:
            pass
    if QUIET_LOGS and tag in {"Fingerprint", "ChatGPT", "OpenAI", "HAR", "Codex", "Session", "Config"}:
        return
    with _print_lock:
        print(f"{_ts()} | {level:<6} | {tag:<12} | {message}", flush=True)


def _set_progress_title(*, total: int, submitted: int, success: int, fail: int, active: int, waiting: bool = False) -> None:
    title = f"ChatGPT_team 运行:{active} 成功:{success} 失败:{fail} 进度:{submitted}/{total}"
    if waiting:
        title += " [等待结束]"
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(title)
    except Exception:
        pass


def _safe_cache_name(email: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.@-]+", "_", str(email or "").strip().lower())


def _cwd_path(name: str) -> Path:
    path = Path(str(name or "").strip())
    if path.is_absolute():
        return path
    return Path.cwd() / path


def _chatgpt_session_path(email: str) -> Path:
    return _cwd_path(CHATGPT_SESSION_DIR) / f"{_safe_cache_name(email)}.json"


def _codex_token_path(email: str) -> Path:
    return _cwd_path(CODEX_TOKEN_DIR) / f"{email}.json"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock, path.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


def _make_trace_headers() -> dict[str, str]:
    trace_id = random.randint(10**17, 10**18 - 1)
    parent_id = random.randint(10**17, 10**18 - 1)
    traceparent = f"00-{uuid.uuid4().hex}-{format(parent_id, '016x')}-01"
    return {
        "traceparent": traceparent,
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": str(trace_id),
        "x-datadog-parent-id": str(parent_id),
    }


def generate_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def extract_code_from_url(url: str | None) -> str | None:
    if not url or "code=" not in url:
        return None
    try:
        return parse_qs(urlparse(url).query).get("code", [None])[0]
    except Exception:
        return None


def _decode_auth_session_cookie(raw_value: str) -> dict[str, Any]:
    raw_value = str(raw_value or "").strip()
    if not raw_value:
        return {}
    for candidate in (raw_value, unquote(raw_value)):
        try:
            if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in {'"', "'"}:
                candidate = candidate[1:-1]
            payload = candidate.split(".", 1)[0]
            payload += "=" * ((4 - len(payload) % 4) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", errors="replace"))
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return {}


def _extract_first_form(html: str) -> tuple[str, dict[str, str]]:
    html = str(html or "")
    match = re.search(r"<form[^>]+action=\"([^\"]+)\"[^>]*>(.*?)</form>", html, re.I | re.S)
    if not match:
        return "", {}
    action = match.group(1)
    inner = match.group(2)
    fields: dict[str, str] = {}
    for name, value in re.findall(r'name=\"([^\"]+)\"(?:[^>]*value=\"([^\"]*)\")?', inner, re.I | re.S):
        fields[str(name)] = str(value or "")
    return action, fields


def _extract_plan_label(value: object) -> str:
    labels: list[str] = []

    def walk(obj: object, path: str = "") -> None:
        if len(labels) >= 6:
            return
        if isinstance(obj, dict):
            for key, item in obj.items():
                key_s = str(key or "")
                low = key_s.lower()
                next_path = f"{path}.{key_s}" if path else key_s
                if any(marker in low for marker in ("plan", "tier", "subscription", "account_type", "sku")):
                    if not isinstance(item, (dict, list)):
                        text = str(item or "").strip()
                        if text:
                            labels.append(f"{next_path}={text}")
                walk(item, next_path)
        elif isinstance(obj, list):
            for idx, item in enumerate(obj[:20]):
                walk(item, f"{path}[{idx}]")

    walk(value)
    return "; ".join(labels[:6]) or "-"


def _random_name() -> str:
    first_names = [
        "Ava", "Mia", "Ethan", "James", "Lucas", "Noah", "Grace", "Emma",
        "Olivia", "Mason", "Liam", "Sophia", "Amelia", "Harper", "Evelyn",
    ]
    last_names = [
        "Smith", "Johnson", "Taylor", "Martin", "Brown", "Garcia", "Young",
        "Hall", "Allen", "King", "Scott", "Lee", "Wright", "Walker", "Hill",
    ]
    return f"{random.choice(first_names)} {random.choice(last_names)}"


def _random_birthdate() -> str:
    year = random.randint(1988, 2003)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return f"{year:04d}-{month:02d}-{day:02d}"


def _generate_password() -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        password = "".join(random.choice(alphabet) for _ in range(13))
        if any(ch.islower() for ch in password) and any(ch.isupper() for ch in password) and any(ch.isdigit() for ch in password):
            return password


_SCREEN_SIZES = ["1366x768", "1440x900", "1536x864", "1920x1080", "1920x1200"]
_HARDWARE_CONCURRENCY = [8, 12, 16]


def _navigator_languages_from_accept(accept_language: str) -> str:
    langs = [part.split(";", 1)[0].strip() for part in accept_language.split(",") if part.strip()]
    return ",".join(langs[:3]) or "en-US,en"


class SentinelTokenGenerator:
    MAX_ATTEMPTS = 500000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(
        self,
        device_id: str | None = None,
        user_agent: str | None = None,
        *,
        screen_size: str | None = None,
        primary_language: str | None = None,
        accept_language: str | None = None,
        hardware_concurrency: int | None = None,
    ):
        self.device_id = device_id or str(uuid.uuid4())
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
        )
        self.screen_size = screen_size or random.choice(_SCREEN_SIZES)
        self.primary_language = primary_language or "en-US"
        self.accept_language = _navigator_languages_from_accept(accept_language or "en-US,en;q=0.9")
        self.hardware_concurrency = hardware_concurrency or random.choice(_HARDWARE_CONCURRENCY)
        self.requirements_seed = str(random.random())
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _get_config(self) -> list[Any]:
        now_str = time.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime())
        perf_now = random.uniform(1000, 50000)
        time_origin = time.time() * 1000 - perf_now
        nav_prop = random.choice([
            "vendorSub", "productSub", "vendor", "maxTouchPoints", "scheduling",
            "userActivation", "doNotTrack", "geolocation", "connection", "plugins",
            "mimeTypes", "pdfViewerEnabled", "webkitTemporaryStorage",
            "webkitPersistentStorage", "hardwareConcurrency", "cookieEnabled",
            "credentials", "mediaDevices", "permissions", "locks", "ink",
        ])
        return [
            self.screen_size,
            now_str,
            4294705152,
            random.random(),
            self.user_agent,
            "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js",
            None,
            None,
            self.primary_language,
            self.accept_language,
            random.random(),
            f"{nav_prop}-undefined",
            random.choice(["location", "implementation", "URL", "documentURI", "compatMode"]),
            random.choice(["Object", "Function", "Array", "Number", "parseFloat", "undefined"]),
            perf_now,
            self.sid,
            "",
            self.hardware_concurrency,
            time_origin,
        ]

    @staticmethod
    def _base64_encode(data: Any) -> str:
        raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def _run_check(self, start_time: float, seed: str, difficulty: str, config: list[Any], nonce: int) -> str | None:
        config[3] = nonce
        config[9] = round((time.time() - start_time) * 1000)
        data = self._base64_encode(config)
        hash_hex = self._fnv1a_32(seed + data)
        if hash_hex[: len(difficulty)] <= difficulty:
            return data + "~S"
        return None

    def generate_token(self, seed: str | None = None, difficulty: str | None = None) -> str:
        seed = seed if seed is not None else self.requirements_seed
        difficulty = str(difficulty or "0")
        start_time = time.time()
        config = self._get_config()
        for i in range(self.MAX_ATTEMPTS):
            result = self._run_check(start_time, seed, difficulty, config, i)
            if result:
                return "gAAAAAB" + result
        return "gAAAAAB" + self.ERROR_PREFIX + self._base64_encode(str(None))

    def generate_requirements_token(self) -> str:
        config = self._get_config()
        config[3] = 1
        config[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._base64_encode(config)


def fetch_sentinel_challenge(
    session: Any,
    device_id: str,
    *,
    flow: str = "authorize_continue",
    user_agent: str | None = None,
    sec_ch_ua: str | None = None,
    impersonate: str | None = None,
    screen_size: str | None = None,
    primary_language: str | None = None,
    accept_language: str | None = None,
    hardware_concurrency: int | None = None,
) -> dict[str, Any] | None:
    generator = SentinelTokenGenerator(
        device_id=device_id,
        user_agent=user_agent,
        screen_size=screen_size,
        primary_language=primary_language,
        accept_language=accept_language,
        hardware_concurrency=hardware_concurrency,
    )
    body = {"p": generator.generate_requirements_token(), "id": device_id, "flow": flow}
    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
        "Origin": "https://sentinel.openai.com",
    }
    if not impersonate:
        headers["User-Agent"] = user_agent or "Mozilla/5.0"
        if sec_ch_ua:
            headers["sec-ch-ua"] = sec_ch_ua
            headers["sec-ch-ua-mobile"] = "?0"
            headers["sec-ch-ua-platform"] = '"Windows"'
    kwargs: dict[str, Any] = {"data": json.dumps(body), "headers": headers, "timeout": 20}
    if impersonate:
        kwargs["impersonate"] = impersonate
    try:
        resp = session.post("https://sentinel.openai.com/backend-api/sentinel/req", **kwargs)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def build_sentinel_token(
    session: Any,
    device_id: str,
    *,
    flow: str = "authorize_continue",
    user_agent: str | None = None,
    sec_ch_ua: str | None = None,
    impersonate: str | None = None,
    screen_size: str | None = None,
    primary_language: str | None = None,
    accept_language: str | None = None,
    hardware_concurrency: int | None = None,
) -> str | None:
    challenge = fetch_sentinel_challenge(
        session,
        device_id,
        flow=flow,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        impersonate=impersonate,
        screen_size=screen_size,
        primary_language=primary_language,
        accept_language=accept_language,
        hardware_concurrency=hardware_concurrency,
    )
    if not challenge:
        return None
    c_value = challenge.get("token", "")
    if not c_value:
        return None
    pow_data = challenge.get("proofofwork") or {}
    generator = SentinelTokenGenerator(
        device_id=device_id,
        user_agent=user_agent,
        screen_size=screen_size,
        primary_language=primary_language,
        accept_language=accept_language,
        hardware_concurrency=hardware_concurrency,
    )
    if pow_data.get("required") and pow_data.get("seed"):
        p_value = generator.generate_token(seed=pow_data.get("seed"), difficulty=pow_data.get("difficulty", "0"))
    else:
        p_value = generator.generate_requirements_token()
    return json.dumps(
        {"p": p_value, "t": "", "c": c_value, "id": device_id, "flow": flow},
        separators=(",", ":"),
    )


@dataclass
class FingerprintProfile:
    impersonate: str
    user_agent: str
    sec_ch_ua: str
    accept_language: str
    primary_language: str
    screen_size: str
    hardware_concurrency: int


_FINGERPRINT_PROFILES = [
    FingerprintProfile(
        impersonate="chrome142",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        sec_ch_ua='"Google Chrome";v="142", "Chromium";v="142", "Not_A Brand";v="99"',
        accept_language="en-US,en;q=0.9",
        primary_language="en-US",
        screen_size="1440x900",
        hardware_concurrency=8,
    ),
    FingerprintProfile(
        impersonate="chrome131",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        sec_ch_ua='"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="99"',
        accept_language="en-GB,en;q=0.9",
        primary_language="en-GB",
        screen_size="1920x1080",
        hardware_concurrency=12,
    ),
    FingerprintProfile(
        impersonate="chrome",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        sec_ch_ua='"Google Chrome";v="142", "Chromium";v="142", "Not_A Brand";v="99"',
        accept_language="en-US,en;q=0.9",
        primary_language="en-US",
        screen_size="1536x864",
        hardware_concurrency=8,
    ),
]


class ChatGPTRegister:
    BASE = CHATGPT_BASE
    AUTH = AUTH_BASE

    def __init__(self, proxy: str | None = None, tag: str = ""):
        self.tag = str(tag or "")
        self.proxy = str(proxy or "").strip() or None
        self.device_id = str(uuid.uuid4())
        self.auth_session_logging_id = str(uuid.uuid4())
        self._callback_url = ""
        self._codex_code_verifier = ""
        self._codex_state = ""

        self.fingerprint = random.choice(_FINGERPRINT_PROFILES)
        self.impersonate = self.fingerprint.impersonate
        self.ua = self.fingerprint.user_agent
        self.sec_ch_ua = self.fingerprint.sec_ch_ua
        self.accept_language = self.fingerprint.accept_language
        self.primary_language = self.fingerprint.primary_language
        self.screen_size = self.fingerprint.screen_size
        self.hardware_concurrency = self.fingerprint.hardware_concurrency

        session_kwargs: dict[str, Any] = {"impersonate": self.impersonate}
        if CurlHttpVersion is not None:
            session_kwargs["http_version"] = CurlHttpVersion.V1_1
        self.session = curl_requests.Session(**session_kwargs)
        if self.proxy:
            self.session.proxies = {"http": self.proxy, "https": self.proxy}
        self.session.headers.update(
            {
                "User-Agent": self.ua,
                "Accept-Language": self.accept_language,
                "sec-ch-ua": self.sec_ch_ua,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            }
        )
        self.session.cookies.set("oai-did", self.device_id, domain="chatgpt.com")
        self._print(
            f"[Fingerprint] impersonate={self.impersonate} lang={self.accept_language} "
            f"screen={self.screen_size} hw={self.hardware_concurrency} proxy={self.proxy or 'DIRECT'}"
        )

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    def _print(self, message: str) -> None:
        tag = "Run"
        body = str(message or "")
        match = re.match(r"^\[([A-Za-z0-9_]+)\]\s*(.*)$", body)
        if match:
            tag = match.group(1)
            body = match.group(2)
        level = "INFO"
        if any(word in body for word in ("失败", "异常", "跳过", "error", "Error")):
            level = "WARN"
        _print_pipe(level, tag, f"账号={self.tag} {body}" if self.tag else body)

    def _log(self, step: str, method: str, url: str, status: int, body: object | None = None) -> None:
        host = (urlparse(url).hostname or "").lower()
        module = "OpenAI" if "openai.com" in host else "ChatGPT" if "chatgpt.com" in host else "Run"
        level = "INFO" if int(status or 0) < 400 else "WARN"
        if not QUIET_LOGS or level != "INFO":
            _print_pipe(level, module, f"账号={self.tag} 步骤={step} 方法={method} 地址={host}{urlparse(url).path} 状态={status}")
        if body and (not QUIET_LOGS or level != "INFO"):
            summary = _short(body, 200)
            if summary:
                _print_pipe(level, module, f"账号={self.tag} {summary}")

    def _json_or_raise(self, step: str, method: str, url: str, response: Any) -> dict[str, Any]:
        try:
            data = response.json()
        except Exception:
            text = _short(getattr(response, "text", "") or "", 300)
            raise RuntimeError(f"{step} failed: HTTP {getattr(response, 'status_code', '-')} non_json={text}")
        if not isinstance(data, dict):
            raise RuntimeError(f"{step} failed: unexpected JSON type {type(data).__name__}")
        return data

    def _get_cookie_value(self, name: str, domain_hint: str = "") -> str:
        jar = getattr(self.session.cookies, "jar", None)
        if jar is None:
            return ""
        for cookie in list(jar):
            if getattr(cookie, "name", "") != name:
                continue
            domain = str(getattr(cookie, "domain", "") or "")
            if domain_hint and domain_hint not in domain:
                continue
            return str(getattr(cookie, "value", "") or "")
        return ""

    def import_cookie_jar(self, cookies: list[dict[str, Any]]) -> int:
        added = 0
        for item in cookies or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "")
            domain = str(item.get("domain") or "")
            path = str(item.get("path") or "/")
            if not name or not domain:
                continue
            with contextlib.suppress(Exception):
                self.session.cookies.set(name, value, domain=domain, path=path)
                added += 1
        return added

    def export_cookie_jar(self) -> list[dict[str, Any]]:
        jar = getattr(self.session.cookies, "jar", None)
        if jar is None:
            return []
        out: list[dict[str, Any]] = []
        for cookie in list(jar):
            out.append(
                {
                    "name": str(getattr(cookie, "name", "") or ""),
                    "value": str(getattr(cookie, "value", "") or ""),
                    "domain": str(getattr(cookie, "domain", "") or ""),
                    "path": str(getattr(cookie, "path", "") or "/"),
                }
            )
        return out

    def visit_homepage(self) -> None:
        url = f"{self.BASE}/"
        resp = self.session.get(
            url,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8", "Upgrade-Insecure-Requests": "1"},
            allow_redirects=True,
            timeout=30,
            impersonate=self.impersonate,
        )
        self._log("0. Visit homepage", "GET", url, int(resp.status_code or 0), {"cookies": len(self.export_cookie_jar())})

    def get_csrf(self) -> str:
        url = f"{self.BASE}/api/auth/csrf"
        resp = self.session.get(url, headers={"Accept": "application/json", "Referer": f"{self.BASE}/"}, timeout=30, impersonate=self.impersonate)
        data = self._json_or_raise("1. Get CSRF", "GET", url, resp)
        token = str(data.get("csrfToken") or "")
        self._log("1. Get CSRF", "GET", url, int(resp.status_code or 0), data)
        if not token:
            raise RuntimeError("missing csrfToken")
        return token

    def signin(self, email: str, csrf: str) -> str:
        url = f"{self.BASE}/api/auth/signin/openai"
        params = {
            "prompt": "login",
            "ext-oai-did": self.device_id,
            "auth_session_logging_id": self.auth_session_logging_id,
            "ext-passkey-client-capabilities": "1111",
            "screen_hint": "login_or_signup",
            "login_hint": email,
        }
        form = {"callbackUrl": f"{self.BASE}/", "csrfToken": csrf, "json": "true"}
        resp = self.session.post(
            url,
            params=params,
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", "Referer": f"{self.BASE}/", "Origin": self.BASE},
            timeout=30,
            impersonate=self.impersonate,
        )
        data = self._json_or_raise("2. Signin", "POST", url, resp)
        auth_url = str(data.get("url") or "")
        self._log("2. Signin", "POST", url, int(resp.status_code or 0), data)
        if not auth_url:
            raise RuntimeError("signin did not return authorize url")
        return auth_url

    def authorize(self, url: str) -> str:
        resp = self.session.get(
            url,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Referer": f"{self.BASE}/", "Upgrade-Insecure-Requests": "1"},
            allow_redirects=True,
            timeout=30,
            impersonate=self.impersonate,
        )
        final_url = str(resp.url)
        self._log("3. Authorize", "GET", url, int(resp.status_code or 0), {"final_url": final_url})
        if int(resp.status_code or 0) >= 400:
            raise RuntimeError(f"authorize failed status={resp.status_code} final={final_url}")
        auth_did = self._get_cookie_value("oai-did", "auth.openai.com") or self._get_cookie_value("oai-did", ".auth.openai.com")
        if auth_did and auth_did != self.device_id:
            self.device_id = auth_did
            self._print(f"[Auth] synchronized oai-did -> {self.device_id[:8]}…")
        return final_url

    def client_auth_session_dump(self) -> tuple[int, dict[str, Any]]:
        url = f"{self.AUTH}/api/accounts/client_auth_session_dump"
        resp = self.session.get(url, headers={"Accept": "application/json", "Referer": f"{self.AUTH}/email-verification"}, timeout=30, impersonate=self.impersonate)
        try:
            data = resp.json()
        except Exception:
            data = {"text": _short(getattr(resp, "text", "") or "", 400)}
        if not isinstance(data, dict):
            data = {"data": data}
        self._log("client_auth_session_dump", "GET", url, int(resp.status_code or 0), data)
        return int(resp.status_code or 0), data

    def get_chatgpt_session_access_token(self) -> tuple[bool, dict[str, Any] | str]:
        url = f"{self.BASE}/api/auth/session"
        last_error = ""
        # 重试 10 次，指数退避：1s / 2s / 4s / 8s ... 最大 32s
        for attempt in range(1, 11):
            try:
                resp = self.session.get(url, headers={"Accept": "application/json", "Referer": f"{self.BASE}/"}, timeout=30, impersonate=self.impersonate)
                if resp.status_code == 200:
                    data = resp.json()
                    if not isinstance(data, dict):
                        last_error = f"api/auth/session non-dict response type={type(data).__name__}"
                        self._print(f"[Session] attempt={attempt} {last_error}")
                    else:
                        access_token = str(data.get("accessToken") or "").strip()
                        if access_token:
                            return True, {
                                "access_token": access_token,
                                "session_token": str(data.get("sessionToken") or "").strip(),
                                "raw_session": data,
                            }
                        # 打印返回的 key 列表方便排错
                        keys = list(data.keys())[:15]
                        last_error = f"api/auth/session missing accessToken, keys={keys}"
                        self._print(f"[Session] attempt={attempt} session 返回了 {len(data)} 个字段但无 accessToken: {keys}")
                else:
                    last_error = f"api/auth/session HTTP {resp.status_code}"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"

            # 指数退避延时
            delay = min(1.0 * (2 ** (attempt - 1)), 32.0)
            self._print(f"[Session] attempt={attempt} 失败: {last_error}，{delay:.0f}s 后重试...")

            # 每次重试前重新访问首页刷新 cookie 状态
            with contextlib.suppress(Exception):
                self.session.get(f"{self.BASE}/", headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Referer": f"{self.BASE}/", "Upgrade-Insecure-Requests": "1"}, allow_redirects=True, timeout=30, impersonate=self.impersonate)
            time.sleep(delay)
        return False, last_error

    def create_account(self, name: str, birthdate: str) -> tuple[int, dict[str, Any]]:
        url = f"{self.AUTH}/api/accounts/create_account"
        sentinel = build_sentinel_token(
            self.session,
            self.device_id,
            flow="create_account",
            user_agent=self.ua,
            sec_ch_ua=self.sec_ch_ua,
            impersonate=self.impersonate,
            screen_size=self.screen_size,
            primary_language=self.primary_language,
            accept_language=self.accept_language,
            hardware_concurrency=self.hardware_concurrency,
        )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": f"{self.AUTH}/about-you",
            "Origin": self.AUTH,
            "oai-device-id": self.device_id,
        }
        headers.update(_make_trace_headers())
        if sentinel:
            headers["openai-sentinel-token"] = sentinel
        resp = self.session.post(url, json={"name": name, "birthdate": birthdate}, headers=headers, timeout=30, impersonate=self.impersonate)
        try:
            data = resp.json()
        except Exception:
            data = {"text": _short(getattr(resp, "text", "") or "", 500)}
        if isinstance(data, dict):
            cb = str(data.get("continue_url") or data.get("url") or data.get("redirect_url") or "")
            if cb:
                self._callback_url = cb
        self._log("7. Create Account", "POST", url, int(resp.status_code or 0), data)
        return int(resp.status_code or 0), data if isinstance(data, dict) else {"data": data}

    def callback(self, url: str | None = None) -> tuple[int, dict[str, Any]]:
        final_url = url or self._callback_url
        if not final_url:
            raise RuntimeError("missing callback url")
        resp = self.session.get(final_url, headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Upgrade-Insecure-Requests": "1"}, allow_redirects=True, timeout=30, impersonate=self.impersonate)
        data = {"final_url": str(resp.url)}
        self._log("8. Callback", "GET", final_url, int(resp.status_code or 0), data)
        return int(resp.status_code or 0), data

    def oauth_authorize_codex(self) -> str:
        verifier, challenge = generate_pkce()
        state = secrets.token_urlsafe(24)
        self._codex_code_verifier = verifier
        self._codex_state = state
        params = {
            "response_type": "code",
            "client_id": OAUTH_CLIENT_ID,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "scope": OAUTH_SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "codex_cli_simplified_flow": "true",
            "id_token_add_organizations": "true",
            "originator": OAUTH_ORIGINATOR,
            "state": state,
        }
        url = f"{AUTH_BASE}/oauth/authorize?{urlencode(params)}"
        self._print("[Codex] GET /oauth/authorize")
        resp = self.session.get(
            url,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Referer": f"{CHATGPT_BASE}/", "Upgrade-Insecure-Requests": "1"},
            allow_redirects=True,
            timeout=30,
            impersonate=self.impersonate,
        )
        final_url = str(resp.url)
        self._print(f"[Codex] /oauth/authorize -> {resp.status_code} final={final_url[:120]}")
        return final_url

    def exchange_codex_code(self, code: str) -> dict[str, Any] | None:
        url = f"{AUTH_BASE}/oauth/token"
        self._print("[Codex] POST /oauth/token")
        resp = self.session.post(
            url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": OAUTH_REDIRECT_URI,
                "client_id": OAUTH_CLIENT_ID,
                "code_verifier": self._codex_code_verifier,
            },
            timeout=60,
            impersonate=self.impersonate,
        )
        self._print(f"[Codex] /oauth/token -> {resp.status_code}")
        try:
            data = resp.json()
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _follow_url_for_code(self, start_url: str, referer: str) -> str:
        current_url = str(start_url or "").strip()
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": referer,
            "Upgrade-Insecure-Requests": "1",
        }
        for _ in range(12):
            if not current_url:
                return ""
            code = extract_code_from_url(current_url)
            if code:
                return code
            try:
                resp = self.session.get(
                    current_url,
                    headers=headers,
                    allow_redirects=False,
                    timeout=30,
                    impersonate=self.impersonate,
                )
            except Exception as exc:
                maybe = re.search(r"(https?://localhost[^\s'\"]+)", str(exc))
                if maybe:
                    return extract_code_from_url(maybe.group(1)) or ""
                self._print(f"[Codex] follow url exception: {exc}")
                return ""
            current_url = str(resp.url)
            code = extract_code_from_url(current_url)
            if code:
                return code
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = str(resp.headers.get("Location") or "")
                if loc.startswith("/"):
                    loc = f"{AUTH_BASE}{loc}"
                code = extract_code_from_url(loc)
                if code:
                    return code
                if loc:
                    headers["Referer"] = current_url
                    current_url = loc
                    continue
            body = str(getattr(resp, "text", "") or "")
            if body:
                hrefs = re.findall(r'href=["\']([^"\']+)["\']', body, flags=re.I)
                moved = False
                for href in hrefs:
                    if href.startswith("/"):
                        href = f"{AUTH_BASE}{href}"
                    code = extract_code_from_url(href)
                    if code:
                        return code
                    if href.startswith("http://localhost") or href.startswith("https://localhost"):
                        return extract_code_from_url(href) or ""
                    if href.startswith(AUTH_BASE) and any(marker in href for marker in ("/api/accounts/consent", "/api/oauth/oauth2/auth", "/sign-in-with-chatgpt/")):
                        headers["Referer"] = current_url
                        current_url = href
                        moved = True
                        break
                if moved:
                    continue
            return ""
        return ""

    def _follow_codex_consent_workspace(self) -> str:
        consent_url = f"{AUTH_BASE}/sign-in-with-chatgpt/codex/consent"
        workspace_id = ""

        try:
            resp = self.session.get(
                consent_url,
                headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Referer": f"{AUTH_BASE}/sign-in-with-chatgpt/codex/consent", "Upgrade-Insecure-Requests": "1"},
                allow_redirects=False,
                timeout=30,
                impersonate=self.impersonate,
            )
            _, fields = _extract_first_form(str(getattr(resp, "text", "") or ""))
            workspace_id = str(fields.get("workspace_id") or "").strip()
        except Exception:
            pass

        if not workspace_id:
            try:
                data_resp = self.session.get(
                    consent_url + ".data",
                    headers={"Accept": "application/json", "Referer": consent_url},
                    timeout=30,
                    impersonate=self.impersonate,
                )
                data = data_resp.json()
                if isinstance(data, dict):
                    workspaces = data.get("workspaces") or data.get("organizations") or []
                    if isinstance(workspaces, list) and workspaces and isinstance(workspaces[0], dict):
                        workspace_id = str(workspaces[0].get("id") or workspaces[0].get("workspace_id") or "").strip()
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and (item.get("id") or item.get("workspace_id")):
                            workspace_id = str(item.get("id") or item.get("workspace_id") or "").strip()
                            break
            except Exception:
                pass

        if not workspace_id:
            st, dump = self.client_auth_session_dump()
            if st == 200:
                session_data = dump.get("client_auth_session") if isinstance(dump.get("client_auth_session"), dict) else dump
                workspaces = session_data.get("workspaces") if isinstance(session_data, dict) else []
                if isinstance(workspaces, list) and workspaces and isinstance(workspaces[0], dict):
                    workspace_id = str(workspaces[0].get("id") or workspaces[0].get("workspace_id") or "").strip()

        if not workspace_id:
            raise RuntimeError("workspace_id not found on consent page")

        self._print(f"[Codex] workspace/select id={workspace_id[:16]}...")
        url = f"{AUTH_BASE}/api/accounts/workspace/select"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": consent_url,
            "Origin": AUTH_BASE,
            "oai-device-id": self.device_id,
        }
        headers.update(_make_trace_headers())
        resp = self.session.post(url, json={"workspace_id": workspace_id}, headers=headers, allow_redirects=False, timeout=30, impersonate=self.impersonate)
        try:
            data = resp.json()
        except Exception:
            data = {"text": _short(getattr(resp, "text", "") or "", 500)}
        self._log("Codex workspace/select", "POST", url, int(resp.status_code or 0), data)
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = str(resp.headers.get("Location") or "")
            if loc.startswith("/"):
                loc = f"{AUTH_BASE}{loc}"
            return self._follow_url_for_code(loc, consent_url)

        if int(resp.status_code or 0) != 200 or not isinstance(data, dict):
            raise RuntimeError(f"workspace/select failed: {_short(data, 220)}")

        continue_url = str(data.get("continue_url") or "")
        if continue_url:
            follow_url = continue_url if continue_url.startswith("http") else f"{AUTH_BASE}{continue_url}" if continue_url.startswith("/") else continue_url
            code = self._follow_url_for_code(follow_url, consent_url)
            if code:
                return code

        page = data.get("page") or {}
        if isinstance(page, dict):
            payload = page.get("payload") or {}
            redirect = str(payload.get("url") or "") if isinstance(payload, dict) else ""
            if redirect:
                code = self._follow_url_for_code(redirect, consent_url)
                if code:
                    return code

        orgs = data.get("data", {}).get("orgs", []) if isinstance(data.get("data"), dict) else []
        if orgs and isinstance(orgs[0], dict):
            org_id = str(orgs[0].get("id") or "").strip()
            if org_id:
                org_url = f"{AUTH_BASE}/api/accounts/organization/select"
                org_headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Referer": consent_url,
                    "Origin": AUTH_BASE,
                    "oai-device-id": self.device_id,
                }
                org_headers.update(_make_trace_headers())
                org_resp = self.session.post(org_url, json={"org_id": org_id}, headers=org_headers, allow_redirects=False, timeout=30, impersonate=self.impersonate)
                try:
                    org_data = org_resp.json()
                except Exception:
                    org_data = {}
                org_next = str(org_data.get("continue_url") or "") if isinstance(org_data, dict) else ""
                if org_next:
                    follow_url = org_next if org_next.startswith("http") else f"{AUTH_BASE}{org_next}" if org_next.startswith("/") else org_next
                    code = self._follow_url_for_code(follow_url, consent_url)
                    if code:
                        return code

        code = self._follow_url_for_code(consent_url, consent_url)
        if code:
            return code
        raise RuntimeError("could not extract authorization code after workspace/select")


def _save_chatgpt_session_cache(*, email: str, reg: ChatGPTRegister, access_result: dict[str, Any], password: str, name: str, birthdate: str) -> None:
    access_token = str((access_result or {}).get("access_token") or "")
    data = {
        "email": email,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "chatgpt_password": password,
        "name": name,
        "birthdate": birthdate,
        "access_token": access_token,
        "session_token": str((access_result or {}).get("session_token") or ""),
        "cookies": reg.export_cookie_jar(),
    }
    path = _chatgpt_session_path(email)
    _save_json(path, data)
    reg._print(f"[Session] ChatGPT 会话已保存: {path}")


def _try_reuse_runtime_chatgpt_session(reg: ChatGPTRegister, email: str) -> tuple[bool, dict[str, Any] | str]:
    path = _chatgpt_session_path(email)
    data = _load_json(path)
    if not data:
        return False, f"missing session cache: {path}"
    count = reg.import_cookie_jar(data.get("cookies") or [])
    reg._print(f"[Session] 加载本地 ChatGPT 会话: cookies={count}")
    if count:
        ok, access_result = reg.get_chatgpt_session_access_token()
        if ok:
            return True, access_result
    cached_token = str(data.get("access_token") or "").strip()
    if cached_token:
        return True, {"access_token": cached_token, "session_token": str(data.get("session_token") or "").strip()}
    return False, f"session invalid for {email}"


def _chatgpt_json(reg: ChatGPTRegister, method: str, path: str, *, access_token: str = "", json_body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    url = f"{reg.BASE}{path}"
    headers = {
        "Accept": "application/json",
        "Origin": reg.BASE,
        "Referer": f"{reg.BASE}/",
        "oai-device-id": reg.device_id,
    }
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    headers.update(_make_trace_headers())
    func = getattr(reg.session, method.lower())
    resp = func(url, json=json_body, headers=headers, timeout=30, impersonate=reg.impersonate)
    try:
        data = resp.json()
    except Exception:
        data = {"text": _short(getattr(resp, "text", "") or "", 800)}
    return int(resp.status_code or 0), data if isinstance(data, dict) else {"data": data}


def _patch_onboarding(reg: ChatGPTRegister, access_token: str) -> None:
    st_me, me = _chatgpt_json(reg, "GET", "/backend-api/me", access_token=access_token)
    if st_me != 200:
        raise RuntimeError(f"backend-api/me failed http={st_me} body={_short(me, 300)}")
    user_id = str(me.get("id") or "").strip()
    if not user_id:
        raise RuntimeError("backend-api/me missing user id")
    st_chk, chk = _chatgpt_json(reg, "GET", "/backend-api/accounts/check/v4-2023-04-27?timezone_offset_min=-480", access_token=access_token)
    if st_chk != 200:
        raise RuntimeError(f"accounts/check failed http={st_chk} body={_short(chk, 300)}")
    accounts = chk.get("accounts") or {}
    if not isinstance(accounts, dict) or not accounts:
        raise RuntimeError("accounts/check missing accounts")
    account_id = next(iter(accounts.keys()))
    path = f"/backend-api/accounts/{account_id}/users/{user_id}"
    payload = {"onboarding_information": {"role": DEFAULT_ONBOARDING_ROLE, "departments": []}}
    st_patch, patched = _chatgpt_json(reg, "PATCH", path, access_token=access_token, json_body=payload)
    if st_patch != 200:
        raise RuntimeError(f"onboarding patch failed http={st_patch} body={_short(patched, 300)}")
    reg._print(f"[HAR] onboarding PATCH success account={account_id} user={user_id}")


def _extract_sso_connection(reg: ChatGPTRegister) -> tuple[str, int]:
    cookie_val = reg._get_cookie_value("oai-client-auth-session", "auth.openai.com") or reg._get_cookie_value("oai-client-auth-session", "openai.com")
    session_data = _decode_auth_session_cookie(cookie_val)
    sso = session_data.get("sso") if isinstance(session_data.get("sso"), dict) else {}
    conns = sso.get("connections") if isinstance(sso, dict) else []
    if isinstance(conns, list):
        for item in conns:
            if isinstance(item, dict):
                name = str(item.get("connection_name") or "").strip()
                provider = int(item.get("connection_provider") or 0)
                if name and provider:
                    return name, provider
    st, dump = reg.client_auth_session_dump()
    if st == 200:
        client_auth = dump.get("client_auth_session") if isinstance(dump.get("client_auth_session"), dict) else dump
        sso = client_auth.get("sso") if isinstance(client_auth, dict) and isinstance(client_auth.get("sso"), dict) else {}
        conns = sso.get("connections") if isinstance(sso, dict) else []
        if isinstance(conns, list):
            for item in conns:
                if isinstance(item, dict):
                    name = str(item.get("connection_name") or "").strip()
                    provider = int(item.get("connection_provider") or 0)
                    if name and provider:
                        return name, provider
    raise RuntimeError("enterprise SSO connection not found")


def _build_authorize_continue_sentinel(reg: ChatGPTRegister, page_url: str) -> str:
    token = build_sentinel_token(
        reg.session,
        reg.device_id,
        flow="authorize_continue",
        user_agent=reg.ua,
        sec_ch_ua=reg.sec_ch_ua,
        impersonate=reg.impersonate,
        screen_size=reg.screen_size,
        primary_language=reg.primary_language,
        accept_language=reg.accept_language,
        hardware_concurrency=reg.hardware_concurrency,
    )
    if not token:
        raise RuntimeError(f"authorize_continue sentinel failed at {page_url}")
    return token


def _dump_sso_page(label: str, url: str, html: str, status: int) -> Path:
    """保存 SSO 页面 HTML 到磁盘，方便排查 OpenAI 页面结构变化"""
    dir_path = _cwd_path("sso_dumps")
    dir_path.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(label or "page"))
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = dir_path / f"{ts}_{safe_label}.html"
    content = (
        f"<!-- URL: {url} -->\n"
        f"<!-- HTTP Status: {status} -->\n"
        f"<!-- Saved: {datetime.now().isoformat()} -->\n"
        f"{html}"
    )
    path.write_text(content, encoding="utf-8")
    return path


def _parse_sso_challenge(html: str, sso_url: str) -> tuple[str, str]:
    """从 SSO 授权页面中提取 challenge 和 form action

    支持多种页面结构：
    1. 传统 HTML <form> 中的 hidden input name="challenge"
    2. <script> 中嵌入的 JSON 数据 (__NEXT_DATA__, window.__DATA__ 等)
    3. data-challenge 属性
    """
    html = str(html or "")

    # ---- 方式1: 传统 HTML form ----
    action, fields = _extract_first_form(html)
    challenge = str(fields.get("challenge") or "").strip()
    if action and challenge:
        return action, challenge

    # ---- 方式2: 在 <script> 中搜索 challenge 值 ----
    # 匹配 "challenge":"xxx" 或 'challenge':'xxx' 或 challenge=xxx
    for pattern in [
        r'"challenge"\s*:\s*"([^"]+)"',
        r"'challenge'\s*:\s*'([^']+)'",
        r'challenge[=]\s*"([^"]+)"',
        r'data-challenge\s*=\s*"([^"]+)"',
    ]:
        m = re.search(pattern, html)
        if m:
            challenge = m.group(1).strip()
            if challenge:
                # 找到了 challenge 但没有 form action — 尝试构造 action
                if not action:
                    action = _extract_action_from_page(html, sso_url)
                if action:
                    return action, challenge

    # ---- 方式3: 直接在页面文本中搜索 challenge 参数 ----
    m = re.search(r'[?&]challenge=([^&\s"\'<>]+)', html)
    if m:
        challenge = m.group(1).strip()
        if challenge and action:
            return action, challenge

    return action, challenge


def _extract_action_from_page(html: str, sso_url: str) -> str:
    """从页面中提取 form action URL"""
    html = str(html or "")
    # 尝试从 <form action="..."> 提取
    m = re.search(r'<form[^>]+action\s*=\s*"([^"]+)"', html, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"<form[^>]+action\s*=\s*'([^']+)'", html, re.I)
    if m:
        return m.group(1).strip()
    # 回退到 sso_url 的路径部分
    parsed = urlparse(sso_url)
    return parsed.path or "/"


def _complete_sixoner_external_flow(reg: ChatGPTRegister, *, email: str, continue_url: str, referer: str) -> str:
    page_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": referer,
        "Upgrade-Insecure-Requests": "1",
    }
    resp = reg.session.get(continue_url, headers=page_headers, allow_redirects=True, timeout=30, impersonate=reg.impersonate)
    sso_url = str(resp.url)
    page_html = str(getattr(resp, "text", "") or "")
    http_status = int(getattr(resp, "status_code", 0) or 0)
    reg._print(f"[HAR] SSO page -> HTTP {http_status} {sso_url[:140]}")

    # ---- 非 2xx → 明确报错（常见：429 Rate limit / 5xx 服务端错误） ----
    if http_status >= 400:
        detail = _short(page_html, 300)
        raise RuntimeError(
            f"SSO 授权页返回 HTTP {http_status}: {detail}\n"
            f"URL: {sso_url}\n"
            f"提示: 429 表示被 OpenAI/WorkOS 限流，请降低并发或更换代理 IP"
        )

    approve_action, challenge = _parse_sso_challenge(page_html, sso_url)

    if not challenge:
        # 失败了 —— 保存页面 dump 方便排查
        dump_path = _dump_sso_page("sso_challenge_missing", sso_url, page_html, resp.status_code)
        reg._print(f"[HAR] SSO 页面已保存到: {dump_path}")
        # 打印页面摘要（前 1500 字符）
        summary = _short(page_html, 1500)
        reg._print(f"[HAR] SSO 页面摘要 (前1500字符): {summary}")
        raise RuntimeError(
            f"SSO approve form missing challenge: {sso_url}\n"
            f"页面已保存到: {dump_path}"
        )

    if not approve_action:
        approve_action = _extract_action_from_page(page_html, sso_url)

    reg._print(f"[HAR] SSO challenge={challenge[:20]}... action={approve_action[:80]}")

    approve_resp = reg.session.post(
        urljoin(sso_url, approve_action),
        data={"email": email, "challenge": challenge},
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": sso_url,
        },
        allow_redirects=False,
        timeout=30,
        impersonate=reg.impersonate,
    )
    approve_loc = str(approve_resp.headers.get("Location") or "")
    reg._print(f"[HAR] SSO approve -> {approve_resp.status_code} next={approve_loc[:160] or '-'}")
    if approve_resp.status_code not in (301, 302, 303, 307, 308) or not approve_loc:
        raise RuntimeError(f"SSO approve failed ({approve_resp.status_code})")

    consent_resp = reg.session.get(
        approve_loc,
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Referer": sso_url, "Upgrade-Insecure-Requests": "1"},
        allow_redirects=True,
        timeout=30,
        impersonate=reg.impersonate,
    )
    consent_url = str(consent_resp.url)
    reg._print(f"[HAR] interstitial page -> {consent_url[:160]}")
    if "/sign-in-with-chatgpt/codex/consent" in consent_url:
        return consent_url

    interstitial_action, interstitial_fields = _extract_first_form(str(getattr(consent_resp, "text", "") or ""))
    interstitial_token = str(interstitial_fields.get("interstitial_token") or "").strip()
    csrf_token = str(interstitial_fields.get("csrf_token") or "").strip()
    action_value = str(interstitial_fields.get("action") or "confirm").strip() or "confirm"
    if not interstitial_action or not interstitial_token or not csrf_token:
        raise RuntimeError(f"interstitial form missing fields: {consent_url}")

    final_resp = reg.session.post(
        urljoin(consent_url, interstitial_action),
        data={"interstitial_token": interstitial_token, "action": action_value, "csrf_token": csrf_token},
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Cache-Control": "max-age=0",
            "Origin": "null",
            "Referer": consent_url,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-User": "?1",
            "Sec-Fetch-Dest": "document",
            "Upgrade-Insecure-Requests": "1",
        },
        allow_redirects=False,
        timeout=30,
        impersonate=reg.impersonate,
    )
    loc = str(final_resp.headers.get("Location") or "")
    reg._print(f"[HAR] interstitial confirm -> {final_resp.status_code} next={loc[:160] or '-'}")
    if final_resp.status_code in (301, 302, 303, 307, 308) and loc:
        callback_resp = reg.session.get(
            loc,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Referer": consent_url, "Upgrade-Insecure-Requests": "1"},
            allow_redirects=True,
            timeout=30,
            impersonate=reg.impersonate,
        )
        final_url = str(callback_resp.url)
        reg._print(f"[HAR] callback page -> {final_url[:160]}")
        return final_url
    return str(final_resp.url)


def _complete_chatgpt_web_sso(reg: ChatGPTRegister, *, email: str, sso_url: str) -> str:
    conn_name, conn_provider = _extract_sso_connection(reg)
    sentinel = _build_authorize_continue_sentinel(reg, sso_url)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Referer": sso_url,
        "Origin": reg.AUTH,
        "oai-device-id": reg.device_id,
        "openai-sentinel-token": sentinel,
    }
    headers.update(_make_trace_headers())
    resp = reg.session.post(
        f"{reg.AUTH}/api/accounts/authorize/continue",
        json={"connection": conn_name, "connection_provider": conn_provider},
        headers=headers,
        allow_redirects=False,
        timeout=30,
        impersonate=reg.impersonate,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"text": _short(getattr(resp, "text", "") or "", 500)}
    reg._log("HAR register authorize_continue", "POST", f"{reg.AUTH}/api/accounts/authorize/continue", int(resp.status_code or 0), data)
    if int(resp.status_code or 0) != 200:
        raise RuntimeError(f"register authorize/continue failed ({resp.status_code}): {_short(data, 220)}")
    continue_url = str(data.get("continue_url") or ((data.get("page") or {}).get("payload") or {}).get("url") or "")
    if not continue_url:
        raise RuntimeError("register authorize/continue did not return continue_url")
    return _complete_sixoner_external_flow(reg, email=email, continue_url=continue_url, referer=sso_url)


def _complete_codex_oauth(reg: ChatGPTRegister, email: str) -> str:
    authorize_final = reg.oauth_authorize_codex()
    reg._print(f"[HAR] Codex authorize -> {authorize_final[:120]}")
    code = extract_code_from_url(authorize_final)
    if code:
        return code
    continue_referer = authorize_final if authorize_final.startswith(AUTH_BASE) else f"{AUTH_BASE}/log-in"
    sentinel = _build_authorize_continue_sentinel(reg, continue_referer)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Referer": continue_referer,
        "Origin": reg.AUTH,
        "oai-device-id": reg.device_id,
        "openai-sentinel-token": sentinel,
    }
    headers.update(_make_trace_headers())
    resp = reg.session.post(
        f"{reg.AUTH}/api/accounts/authorize/continue",
        json={"username": {"kind": "email", "value": email}},
        headers=headers,
        timeout=30,
        allow_redirects=False,
        impersonate=reg.impersonate,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"text": _short(getattr(resp, "text", "") or "", 500)}
    reg._log("HAR codex authorize_continue", "POST", f"{reg.AUTH}/api/accounts/authorize/continue", int(resp.status_code or 0), data)
    if int(resp.status_code or 0) != 200:
        raise RuntimeError(f"Codex authorize/continue failed ({resp.status_code}): {_short(data, 220)}")
    next_url = str(data.get("continue_url") or data.get("url") or ((data.get("page") or {}).get("payload") or {}).get("url") or "")
    page = data.get("page") or {}
    page_type = str(page.get("type") or "") if isinstance(page, dict) else ""
    reg._print(f"[HAR] codex continue -> page={page_type or '-'} next={next_url[:120]}")
    if page_type == "external_url" or "external.auth.openai.com/sso/authorize" in next_url:
        next_url = _complete_sixoner_external_flow(reg, email=email, continue_url=next_url, referer=continue_referer)
    code = extract_code_from_url(next_url)
    if code:
        return code
    if next_url:
        with contextlib.suppress(Exception):
            reg.session.get(
                next_url,
                headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Referer": continue_referer, "Upgrade-Insecure-Requests": "1"},
                allow_redirects=True,
                timeout=30,
                impersonate=reg.impersonate,
            )
    return reg._follow_codex_consent_workspace()


def _debug_auth_cookies(reg: ChatGPTRegister, stage: str) -> None:
    items = []
    for cookie in reg.export_cookie_jar():
        domain = str(cookie.get("domain") or "")
        name = str(cookie.get("name") or "")
        if "chatgpt.com" in domain or "openai.com" in domain:
            items.append(f"{name}@{domain}")
    reg._print(f"[HAR] {stage} cookies={items[:20]}")


def _get_access_token_after_callback(reg: ChatGPTRegister) -> tuple[bool, dict[str, Any] | str]:
    return reg.get_chatgpt_session_access_token()


def _query_plan_with_access_token(reg: ChatGPTRegister, access_token: str) -> tuple[int, str]:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {str(access_token or '').strip()}",
        "Referer": "https://chatgpt.com/",
        "Origin": "https://chatgpt.com",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }
    resp = reg.session.get(WHAM_USAGE_URL, headers=headers, timeout=30, impersonate=reg.impersonate)
    status = int(getattr(resp, "status_code", 0) or 0)
    text = str(getattr(resp, "text", "") or "")
    if status != 200:
        return status, _short(text, 220)
    try:
        data = resp.json()
    except Exception:
        return status, f"non_json: {_short(text, 220)}"
    return status, _extract_plan_label(data)


def _is_retryable_tls_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    markers = (
        "tls connect error",
        "invalid library",
        "openssl_internal",
        "curl: (35)",
        "sslerror",
        "wrong_version_number",
        "connection reset by peer",
        "recv failure",
    )
    return any(marker in text for marker in markers)


def _refresh_token_file_on_401(token_path: Path, token_data: dict[str, Any], proxy: str | None) -> tuple[bool, str]:
    email = str((token_data or {}).get("email") or token_path.stem).strip()
    reg = None
    chain = None
    try:
        account_proxy, chain = _prepare_proxy_for_account(proxy, f"refresh-{email}")
        reg = ChatGPTRegister(proxy=account_proxy, tag=f"refresh-{email}")
        ok, access_result = _try_reuse_runtime_chatgpt_session(reg, email)
        if not ok:
            return False, str(access_result)
        code = _complete_codex_oauth(reg, email)
        if not code:
            return False, "failed to get authorization code during refresh"
        tokens = reg.exchange_codex_code(code)
        if not tokens or not isinstance(tokens, dict):
            return False, "/oauth/token failed during refresh"
        refresh_token = str(tokens.get("refresh_token") or "").strip()
        if not refresh_token:
            return False, "missing refresh_token during refresh"
        updated = dict(token_data or {})
        updated["type"] = "codex"
        updated["email"] = email
        updated["token_source"] = "ChatGPT_team_refresh_401"
        updated["refresh_token"] = refresh_token
        updated["access_token"] = str(tokens.get("access_token") or "")
        updated["id_token"] = str(tokens.get("id_token") or "")
        updated["saved_at"] = datetime.now(timezone.utc).isoformat()
        _save_json(token_path, updated)
        _print_pipe("OK", "Check", f"email={email} 已覆盖 token 文件 path={token_path.name}")
        return True, refresh_token
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if reg is not None:
            reg.close()
        with contextlib.suppress(Exception):
            if chain is not None:
                chain.close()


def _check_single_token_file(token_path: Path, proxy: str | None) -> tuple[str, bool, bool]:
    token_data = _load_json(token_path)
    email = str(token_data.get("email") or token_path.stem).strip()
    access_token = str(token_data.get("access_token") or "").strip()
    if not access_token:
        _print_pipe("WARN", "Check", f"email={email} 缺少 access_token，跳过")
        return email, False, False

    reg = None
    chain = None
    try:
        account_proxy, chain = _prepare_proxy_for_account(proxy, f"check-{email}")
        reg = ChatGPTRegister(proxy=account_proxy, tag=f"check-{email}")
        status, detail = _query_plan_with_access_token(reg, access_token)
        if status == 200:
            _print_pipe("OK", "Check", f"email={email} plan={detail}")
            return email, True, False

        if status == 429:
            _print_pipe("WARN", "Check", f"email={email} plan_check=429，重试一次")
            time.sleep(1.0)
            status, detail = _query_plan_with_access_token(reg, access_token)
            if status == 200:
                _print_pipe("OK", "Check", f"email={email} retry plan={detail}")
                return email, True, False
            if status == 429:
                _print_pipe("WARN", "Check", f"email={email} 二次 429，开始 OAuth 刷新")
                ok, refresh_detail = _refresh_token_file_on_401(token_path, token_data, proxy)
                if not ok:
                    _print_pipe("ERR", "Check", f"email={email} 429 后刷新失败: {refresh_detail}")
                    return email, False, False
                reg2 = None
                chain2 = None
                try:
                    account_proxy2, chain2 = _prepare_proxy_for_account(proxy, f"recheck-{email}")
                    reg2 = ChatGPTRegister(proxy=account_proxy2, tag=f"recheck-{email}")
                    refreshed_data = _load_json(token_path)
                    new_at = str(refreshed_data.get("access_token") or "").strip()
                    status2, detail2 = _query_plan_with_access_token(reg2, new_at)
                    if status2 == 200:
                        _print_pipe("OK", "Check", f"email={email} refreshed plan={detail2}")
                        return email, True, True
                    _print_pipe("WARN", "Check", f"email={email} 刷新后套餐查询失败 status={status2} detail={detail2}")
                    return email, False, True
                finally:
                    if reg2 is not None:
                        reg2.close()
                    with contextlib.suppress(Exception):
                        if chain2 is not None:
                            chain2.close()

        if status == 401:
            _print_pipe("WARN", "Check", f"email={email} access_token=401，开始 OAuth 刷新")
            ok, refresh_detail = _refresh_token_file_on_401(token_path, token_data, proxy)
            if not ok:
                _print_pipe("ERR", "Check", f"email={email} 刷新失败: {refresh_detail}")
                return email, False, False
            reg2 = None
            chain2 = None
            try:
                account_proxy2, chain2 = _prepare_proxy_for_account(proxy, f"recheck-{email}")
                reg2 = ChatGPTRegister(proxy=account_proxy2, tag=f"recheck-{email}")
                refreshed_data = _load_json(token_path)
                new_at = str(refreshed_data.get("access_token") or "").strip()
                status2, detail2 = _query_plan_with_access_token(reg2, new_at)
                if status2 == 200:
                    _print_pipe("OK", "Check", f"email={email} refreshed plan={detail2}")
                    return email, True, True
                _print_pipe("WARN", "Check", f"email={email} 刷新后套餐查询失败 status={status2} detail={detail2}")
                return email, False, True
            finally:
                if reg2 is not None:
                    reg2.close()
                with contextlib.suppress(Exception):
                    if chain2 is not None:
                        chain2.close()

        _print_pipe("WARN", "Check", f"email={email} plan_check status={status} detail={detail}")
        return email, False, False
    except Exception as exc:
        _print_pipe("ERR", "Check", f"email={email} 检查异常: {type(exc).__name__}: {exc}")
        return email, False, False
    finally:
        if reg is not None:
            reg.close()
        with contextlib.suppress(Exception):
            if chain is not None:
                chain.close()


def run_token_check(proxy: str | None, workers: int = 100) -> int:
    token_dir = _cwd_path(CODEX_TOKEN_DIR)
    token_files = sorted([p for p in token_dir.glob("*.json") if p.is_file()])
    if not token_files:
        _print_pipe("WARN", "Check", "未找到 codex_tokens/*.json")
        return 1
    workers = max(1, int(workers or 100))
    _print_pipe("INFO", "Check", f"开始检查本地 token 文件: {len(token_files)} threads={workers}")
    checked = 0
    refreshed = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(token_files))), thread_name_prefix="team-check-") as executor:
        futures = {executor.submit(_check_single_token_file, token_path, proxy): token_path for token_path in token_files}
        while futures:
            done, _ = wait(list(futures.keys()), timeout=0.5, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for fut in done:
                futures.pop(fut, None)
                _email, ok, did_refresh = fut.result()
                if ok:
                    checked += 1
                else:
                    failed += 1
                if did_refresh:
                    refreshed += 1
    _print_pipe("INFO", "Check", f"完成: checked={checked} refreshed={refreshed} failed={failed}")
    return 0 if (checked or refreshed) else 1


def _register_one(idx: int, total: int, proxy: str | None, output_file: str) -> tuple[bool, str]:
    tag = f"r{idx}"
    email = ""
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        reg = None
        chain = None
        try:
            account_proxy, chain = _prepare_proxy_for_account(proxy, tag)
            reg = ChatGPTRegister(proxy=account_proxy, tag=tag)
            name = _random_name()
            birthdate = _random_birthdate()
            password = _generate_password()
            email = f"{_random_local(name)}{EMAIL_DOMAIN}"
            if attempt == 1:
                _print_pipe("INFO", tag, f"[HAR] 随机邮箱: {email}")
            else:
                _print_pipe("WARN", "Run", f"账号={tag} TLS/SSL 重试 {attempt}/3 email={email}")

            reg.visit_homepage()
            time.sleep(random.uniform(0.2, 0.5))
            csrf = reg.get_csrf()
            auth_url = reg.signin(email, csrf)
            final_url = reg.authorize(auth_url)
            final_path = urlparse(final_url).path
            final_host = (urlparse(final_url).hostname or "").lower()
            reg._print(f"[HAR] authorize -> {final_path}")

            needs_about_you = False
            if final_path == "/sso":
                final_url = _complete_chatgpt_web_sso(reg, email=email, sso_url=final_url)
                final_path = urlparse(final_url).path
                final_host = (urlparse(final_url).hostname or "").lower()
                reg._print(f"[HAR] register SSO 完成 -> {final_url[:160]}")
            elif "about-you" in final_path:
                needs_about_you = True
            elif final_host == "chatgpt.com" or final_host.endswith(".chatgpt.com"):
                reg._print("[HAR] ChatGPT Web 会话已建立")
            else:
                raise RuntimeError(f"unexpected authorize destination: {final_url}")

            if needs_about_you:
                st, data = reg.create_account(name, birthdate)
                if st != 200:
                    raise RuntimeError(f"create_account failed ({st}): {_short(data, 220)}")
                reg.callback(str(data.get("continue_url") or ""))
                reg._print("[HAR] callback 完成")

            _debug_auth_cookies(reg, "callback后")
            ok_at, at_result = _get_access_token_after_callback(reg)
            if not ok_at:
                raise RuntimeError(f"failed to get access token: {at_result}")
            access_token = str((at_result or {}).get("access_token") or "").strip()
            _save_chatgpt_session_cache(
                email=email,
                reg=reg,
                access_result=at_result if isinstance(at_result, dict) else {},
                password=password,
                name=name,
                birthdate=birthdate,
            )
            _patch_onboarding(reg, access_token)

            code = _complete_codex_oauth(reg, email)
            if not code:
                raise RuntimeError("missing authorization code")
            tokens = reg.exchange_codex_code(code)
            if not tokens or not isinstance(tokens, dict):
                raise RuntimeError("/oauth/token failed")
            refresh_token = str(tokens.get("refresh_token") or "").strip()
            if not refresh_token:
                raise RuntimeError("missing refresh_token")
            token_path = _codex_token_path(email)
            _save_json(
                token_path,
                {
                    "type": "codex",
                    "email": email,
                    "token_source": "ChatGPT_team",
                    "refresh_token": refresh_token,
                    "access_token": str(tokens.get("access_token") or ""),
                    "id_token": str(tokens.get("id_token") or ""),
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            _append_line(_cwd_path(output_file), f"{email}----{password}----rt----{refresh_token}")

            # ---- Sub2API 格式输出 ----
            output_config = _get_output_format_config()
            if output_config["generate_sub2api"]:
                sub2api_doc = _convert_cpa_to_sub2api({
                    "type": "codex",
                    "email": email,
                    "refresh_token": refresh_token,
                    "access_token": str(tokens.get("access_token") or ""),
                    "id_token": str(tokens.get("id_token") or ""),
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                })
                sub2api_path = _save_sub2api_output(email, sub2api_doc, output_config["sub2api_output_dir"])
                reg._print(f"[Sub2API] Sub2API 格式文件已保存: {sub2api_path}")

            # ---- sub2api 自动上传 ----
            sub2api_config = _get_sub2api_config()
            if sub2api_config["enabled"]:
                upload_ok, upload_msg = _upload_account_to_sub2api(
                    name=email,
                    refresh_token=refresh_token,
                    config=sub2api_config,
                )
                if upload_ok:
                    reg._print(f"[Sub2API] 自动上传成功 -> {email}")
                else:
                    reg._print(f"[Sub2API] 自动上传失败: {upload_msg}")

            _print_pipe("OK", "Summary", f"[{idx}/{total}] 注册成功 email={email} rt_saved=Y first_rt=Y")
            return True, email
        except Exception as exc:
            last_exc = exc
            if attempt < 3 and _is_retryable_tls_error(exc):
                time.sleep(1.0 * attempt)
                continue
            reason = f"[{idx}/{total}] 注册失败 email={email or '-'} err={type(exc).__name__}: {exc}"
            _append_line(_cwd_path(FAILED_OUTPUT_FILE), reason)
            _print_pipe("ERR", "Summary", reason)
            return False, email
        finally:
            if reg is not None:
                reg.close()
            with contextlib.suppress(Exception):
                if chain is not None:
                    chain.close()
    reason = f"[{idx}/{total}] 注册失败 email={email or '-'} err={type(last_exc).__name__ if last_exc else 'UnknownError'}: {last_exc or 'unknown'}"
    _append_line(_cwd_path(FAILED_OUTPUT_FILE), reason)
    _print_pipe("ERR", "Summary", reason)
    return False, email


def run_batch(total_accounts: int, max_workers: int, proxy: str | None, output_file: str) -> int:
    total_accounts = max(1, int(total_accounts))
    max_workers = max(1, int(max_workers))
    def _fire_progress(success: int, fail: int, submitted: int, total: int) -> None:
        if _PROGRESS_CALLBACK is not None:
            try:
                _PROGRESS_CALLBACK(success, fail, submitted, total)
            except Exception:
                pass

    _print_pipe("INFO", "Run", f"HAR注册模式启动：数量={total_accounts} 并发={max_workers} 代理模式={'动态API' if _config_is_dynamic() else (proxy or '无')}")

    # ---- Sub2API 分组名称预解析（启动时查询一次） ----
    sub2api_config = _get_sub2api_config()
    if sub2api_config["enabled"]:
        _resolve_sub2api_group_ids(sub2api_config)

    success = 0
    fail = 0
    submitted = 0
    stop_waiting = False
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="team-reg-") as executor:
        futures: dict[Any, int] = {}

        def _submit_next() -> bool:
            nonlocal submitted
            if stop_waiting or submitted >= total_accounts:
                return False
            submitted += 1
            fut = executor.submit(_register_one, submitted, total_accounts, proxy, output_file)
            futures[fut] = submitted
            return True

        for _ in range(min(max_workers, total_accounts)):
            _submit_next()

        _set_progress_title(total=total_accounts, submitted=submitted, success=success, fail=fail, active=len(futures))
        try:
            while futures:
                done, _ = wait(list(futures.keys()), timeout=0.5, return_when=FIRST_COMPLETED)
                if not done:
                    _set_progress_title(total=total_accounts, submitted=submitted, success=success, fail=fail, active=len(futures), waiting=stop_waiting)
                    continue
                for fut in done:
                    futures.pop(fut, None)
                    ok, _email = fut.result()
                    if ok:
                        success += 1
                    else:
                        fail += 1
                    _fire_progress(success, fail, submitted, total_accounts)
                    if not stop_waiting:
                        _submit_next()
                _set_progress_title(total=total_accounts, submitted=submitted, success=success, fail=fail, active=len(futures), waiting=stop_waiting)
        except KeyboardInterrupt:
            stop_waiting = True
            _print_pipe("WARN", "Run", "收到 Ctrl+C，不再投递新任务，等待当前注册完成…（再按一次 Ctrl+C 强制退出）")
            _set_progress_title(total=total_accounts, submitted=submitted, success=success, fail=fail, active=len(futures), waiting=True)
            try:
                while futures:
                    done, _ = wait(list(futures.keys()), timeout=0.5, return_when=FIRST_COMPLETED)
                    if not done:
                        _set_progress_title(total=total_accounts, submitted=submitted, success=success, fail=fail, active=len(futures), waiting=True)
                        continue
                    for fut in done:
                        futures.pop(fut, None)
                        ok, _email = fut.result()
                        if ok:
                            success += 1
                        else:
                            fail += 1
                        _fire_progress(success, fail, submitted, total_accounts)
                    _set_progress_title(total=total_accounts, submitted=submitted, success=success, fail=fail, active=len(futures), waiting=True)
            except KeyboardInterrupt:
                _print_pipe("WARN", "Run", "再次收到 Ctrl+C，立即退出")
                raise SystemExit(130) from None
        finally:
            _set_progress_title(total=total_accounts, submitted=submitted, success=success, fail=fail, active=0)

    _print_pipe("INFO", "Summary", f"HAR注册模式完成：成功={success}/{total_accounts} 输出={output_file}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone pure RT flow for ChatGPT/Codex team accounts")
    parser.add_argument("-p", "--proxy", default=None)
    parser.add_argument("-n", "--total", type=int, default=1)
    parser.add_argument("-w", "--workers", type=int, default=1)
    parser.add_argument("-o", "--output", default=REGISTERED_OUTPUT_FILE)
    parser.add_argument("--check-tokens", action="store_true", help="check existing codex_tokens access_token plan type; refresh on 401 and overwrite file")
    args = parser.parse_args()
    proxy = str(args.proxy or DEFAULT_PROXY or "").strip() or None
    if args.check_tokens:
        workers = args.workers if args.workers and args.workers > 0 else 100
        return run_token_check(proxy, workers=workers)
    return run_batch(args.total, args.workers, proxy, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
