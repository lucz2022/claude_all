"""Data analysis MCP server with fixed Bearer Token auth.

Serves read / clean / analyze tools over the derived-data directory
(default: C:\\data\\claude_all\\data\\derived). Designed to run behind
Cloudflare Tunnel: bind to 127.0.0.1 only, never expose the port directly.

Run:
    uvicorn server:app --host 127.0.0.1 --port 8420
    (or: python server.py)
"""
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# MCP 只做 adapter：领域计算/摘要一律调用 fx_data 已验收实现（单一事实源）
from fx_data.api import get_env_state, get_series as api_get_series  # noqa: E402
from fx_data.summary import (  # noqa: E402
    board_compare as api_board_compare,
    board_summary,
    render_board_text,
    render_compare_text,
)

load_dotenv(BASE_DIR / ".env")

AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
if not AUTH_TOKEN:
    # Fail closed at startup instead of silently serving without auth.
    raise SystemExit("MCP_AUTH_TOKEN is not set - put it in mcp_server/.env")

DATA_ROOT = Path(
    os.environ.get("MCP_DATA_ROOT", r"C:\data\claude_all\data\derived")
).resolve()
ALLOWED_EXT = {".json", ".jsonl", ".ndjson", ".csv", ".tsv", ".parquet"}


# ---------------------------------------------------------------- auth layer

# OAuth 发现/注册/授权/换Token端点放行（claude.ai 网页端 Custom Connector 需要）
_OAUTH_OPEN_PREFIXES = ("/.well-known/", "/oauth/")


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """固定 Bearer Token 或本服务签发的 OAuth access token；其余一律 401。"""

    async def dispatch(self, request, call_next):
        if request.url.path.startswith(_OAUTH_OPEN_PREFIXES):
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        ok = auth.startswith("Bearer ") and (
            hmac.compare_digest(auth[7:], AUTH_TOKEN) or _check_oauth_token(auth[7:])
        )
        if not ok:
            resp = JSONResponse({"error": "unauthorized"}, status_code=401)
            resp.headers["WWW-Authenticate"] = f'Bearer resource_metadata="{ISSUER}/.well-known/oauth-protected-resource"'
            return resp
        return await call_next(request)


# ---------------------------------------------------------------- path guard

def _resolve(path: str, allow_glob: bool = False) -> Path:
    """Resolve `path` against DATA_ROOT and refuse anything escaping it."""
    p = Path(path)
    if not p.is_absolute():
        p = DATA_ROOT / p
    p = p.resolve()
    if p != DATA_ROOT and DATA_ROOT not in p.parents:
        raise ValueError(f"path escapes data root: must stay under {DATA_ROOT}")
    if not allow_glob or not any(ch in p.name for ch in "*?"):
        if p.suffix.lower() not in ALLOWED_EXT:
            raise ValueError(
                f"extension {p.suffix!r} not allowed; allowed: {sorted(ALLOWED_EXT)}"
            )
    return p


def _read_json(name: str) -> dict:
    with open(_resolve(name), encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- mcp server

mcp = MCPServer("data-analysis-mcp")


@mcp.tool()
def list_data_files() -> str:
    """List all data files available under the data root, with size and modified time."""
    rows = []
    for p in sorted(DATA_ROOT.rglob("*")):
        if p.is_file() and p.suffix.lower() in ALLOWED_EXT:
            st = p.stat()
            rows.append(
                f"{p.relative_to(DATA_ROOT)}\t{st.st_size:,} B\t"
                f"{datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M}"
            )
    if not rows:
        return f"no data files under {DATA_ROOT}"
    return "\n".join(rows)


@mcp.tool()
def read_json(path: str, max_chars: int = 6000) -> str:
    """Pretty-print a JSON file (path relative to data root or absolute).

    Output is truncated to max_chars so huge files don't flood the context.
    """
    p = _resolve(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        out = json.dumps(data, ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        # probably JSONL - fall back to raw text preview
        out = p.read_text(encoding="utf-8")
    return out if len(out) <= max_chars else out[:max_chars] + f"\n...[truncated {len(out) - max_chars} chars]"


@mcp.tool()
def read_parquet(path: str, n: int = 10, tail: bool = True) -> str:
    """Inspect a parquet file: schema, dtypes and head/tail rows."""
    import pandas as pd

    p = _resolve(path)
    df = pd.read_parquet(p)
    part = df.tail(n) if tail else df.head(n)
    return (
        f"file: {p.name}\nshape: {df.shape[0]} rows x {df.shape[1]} cols\n"
        f"columns/dtypes:\n{df.dtypes.to_string()}\n\n"
        f"{'last' if tail else 'first'} {len(part)} rows:\n{part.to_string()}"
    )


_SQL_FORBIDDEN = re.compile(
    r"\b(copy|attach|export|install|load|create|insert|update|delete|drop|alter|pragma|call)\b",
    re.IGNORECASE,
)
_SQL_PATH_TOKEN = re.compile(r"'([^']*\.(?:parquet|csv|tsv|json|jsonl|ndjson)(?:\*\*)?)'")


@mcp.tool()
def sql_query(sql: str, max_rows: int = 50) -> str:
    """Run a read-only DuckDB query over data files.

    SELECT/WITH only. File paths inside SQL are resolved against the data root,
    e.g. SELECT * FROM read_parquet('dxy__D1.parquet') LIMIT 5;
    Globs are allowed: read_parquet('*.parquet').
    """
    import duckdb

    stripped = sql.strip().rstrip(";")
    if not re.match(r"^(select|with)\b", stripped, re.IGNORECASE):
        raise ValueError("only SELECT / WITH statements are allowed")
    if _SQL_FORBIDDEN.search(stripped):
        raise ValueError("statement contains a forbidden (non-read-only) keyword")

    # rewrite every quoted file token to an absolute path inside DATA_ROOT
    def _abs(match: re.Match) -> str:
        resolved = _resolve(match.group(1), allow_glob=True)
        return "'" + str(resolved).replace("'", "''") + "'"

    sql_final = _SQL_PATH_TOKEN.sub(_abs, stripped)
    con = duckdb.connect(":memory:")
    try:
        df = con.execute(sql_final).fetch_df()
    finally:
        con.close()
    shown = df.head(max_rows)
    msg = f"{df.shape[0]} rows"
    if df.shape[0] > max_rows:
        msg += f" (showing first {max_rows})"
    return f"{msg}\n{shown.to_string(index=False)}"


@mcp.tool()
def clean_csv(path: str, dedupe: bool = True, dropna_rows: bool = False) -> str:
    """Clean a CSV file: optional dedupe and empty-row drop.

    Writes <name>_cleaned.csv next to the source and returns the output path.
    """
    import pandas as pd

    p = _resolve(path)
    df = pd.read_csv(p)
    before = len(df)
    if dropna_rows:
        df = df.dropna(how="any")
    if dedupe:
        df = df.drop_duplicates()
    out = p.with_name(p.stem + "_cleaned.csv")
    df.to_csv(out, index=False)
    return f"{p.name}: {before} -> {len(df)} rows, written to {out}"


@mcp.tool()
def clean_json(path: str, dedupe_key: str = None) -> str:
    """Clean a JSON file (list-of-dict): optional dedupe by key.

    Writes <name>_cleaned.json next to the source and returns the output path.
    """
    p = _resolve(path)
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    if dedupe_key and isinstance(data, list):
        seen, deduped = set(), []
        for item in data:
            if isinstance(item, dict):
                k = item.get(dedupe_key)
                if k not in seen:
                    seen.add(k)
                    deduped.append(item)
        data = deduped
    out = p.with_name(p.stem + "_cleaned.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return f"cleaned {p.name} -> {out}"


# ------------------------------------------------------- domain tools (fx)
# 全部为 fx_data 的薄 adapter——不得在 server 内维护第二套金融摘要逻辑。

@mcp.tool()
def fx_board(window: int = 20) -> str:
    """Currency strength board text summary (window=20 or 50).

    Header carries staleness_hours / dispersion / board_vol / residual_rms;
    per currency: z / z_short / delta / state / purity (purity<0.60 marked (!));
    ends with momentum-turn ranking by |delta|.
    """
    if window not in (20, 50):
        raise ValueError("window 必须为 20 或 50")
    return render_board_text(board_summary(window))


@mcp.tool()
def fx_env() -> str:
    """Market environment snapshot: regime, reflation inputs, gauge decomposition,
    self-reference warning, staleness. Rendered from fx_data.env (single source)."""
    env = get_env_state()
    warn = env.get("gauge_self_reference_warning") or []
    lines = [
        f"schema_version: {env.get('schema_version')}",
        f"regime_type: {env['regime_type']}   asof: {env.get('asof')}",
        f"weather_gauge: {env['weather_gauge']:.2f}   vix_level: {env.get('vix_level')}",
        f"effective_session: {env.get('effective_session')}"
        f"   staleness_hours: {env.get('staleness_hours')}"
        f"   staleness_status: {env.get('staleness_status')}",
        "",
        "ratios:",
    ]
    for name, r in env.get("ratios", {}).items():
        lines.append(
            f"  {name}: level={r['level']:.6g}  slope_5d={r['slope_5d']:+.4f}"
            f"  state={r['state']}"
        )
    ri = env.get("regime_inputs", {})
    lines += [
        "",
        "regime_inputs:",
        f"  reflation_proxy: {ri.get('reflation_proxy')}",
        f"  reflation_value: {ri.get('reflation_value')}",
        f"  reflation_state: {ri.get('reflation_state')}",
        f"  reflation_direction_convention: {ri.get('reflation_direction_convention')}",
        f"  reflation_threshold: {ri.get('reflation_threshold')}",
        f"  reflation_data_source: {ri.get('reflation_data_source')}",
        f"  reflation_real_source: {ri.get('reflation_real_source')}",
        f"  reflation_coverage_note: {ri.get('reflation_coverage_note')}",
        f"  risk_proxy: {ri.get('risk_proxy')}  risk_value: {ri.get('risk_value')}",
    ]
    gsc = env.get("gauge_sum_check", {})
    lines += [
        "",
        f"gauge_sum_check: abs_diff={gsc.get('abs_diff')}"
        f"  sum={gsc.get('sum_contrib')}  gauge={gsc.get('weather_gauge')}",
        "gauge_components:",
    ]
    for c in env.get("gauge_components", []):
        lines.append(
            f"  {c['name']}: label={c.get('label')}  z20={c.get('z20')}"
            f"  z50={c.get('z50')}  contrib={c.get('contrib')}"
            + (f"  [{c['note']}]" if c.get("note") else "")
        )
    lines += [
        "",
        "gauge_underlying_exposure:",
    ]
    for u, e in sorted(env.get("gauge_underlying_exposure", {}).items()):
        lines.append(
            f"  {u}: n_components={e['n_components']}  components={e['components']}"
            f"  net_sign={e['net_sign']:+d}  abs_weight={e['abs_weight']:.2f}"
        )
    lines += [
        f"gauge_underlying_concentration: {env.get('gauge_underlying_concentration')}",
    ]
    if warn:
        lines.append(f"⚠ gauge_self_reference_warning: {', '.join(warn)} — 分析该标的时本 gauge 不独立")
    else:
        lines.append("gauge_self_reference_warning: []")
    lines += [
        "",
        "coefficient: " + json.dumps(env.get("coefficient", {}), ensure_ascii=False),
        f"calendar_intersection_ratio: {env.get('calendar_intersection_ratio')}",
    ]
    srcs = env.get("sources")
    if srcs:
        lines.append("sources: " + json.dumps(srcs, ensure_ascii=False))
    return "\n".join(lines)


@mcp.tool()
def fx_board_compare() -> str:
    """Compare w20 vs w50 boards: z / z_short / Δ(w20) / Δ(w50) / sign consistency.

    Rows where the two windows' Δ signs disagree are marked 冲突(!) and listed.
    """
    return render_compare_text(api_board_compare(20, 50))


@mcp.tool()
def dxy_series(n: int = 30, since: str = None) -> str:
    """DXY daily series (append-only full history).

    Returns window_mode / window_rows / first_session / stats_scope / warning /
    stats(min,max,mean) / available / rows via fx_data.get_series("DXY").
    """
    obj = api_get_series("DXY", tf="D1", n=n, since=since)
    stats = obj.get("stats", {})
    lines = [
        f"window_mode: {obj.get('window_mode')}   stats_scope: {obj.get('stats_scope')}",
        f"window_rows: {obj.get('window_rows')}   first_session: {obj.get('first_session')}",
        f"available: {obj.get('available')}   row_count: {obj.get('row_count')}"
        f"   truncated: {obj.get('truncated')}",
        f"stats.min: {stats.get('min'):.4f}   stats.max: {stats.get('max'):.4f}"
        f"   stats.mean: {stats.get('mean'):.4f}",
        f"warning: {obj.get('warning')}",
        f"staleness_hours: {obj.get('staleness_hours')}  status: {obj.get('staleness_status')}",
        "",
    ]
    for r in obj.get("rows", []):
        lines.append(f"{r['session_date']}  {r['close']:.4f}")
    return "\n".join(lines)


@mcp.tool()
def get_series(
    symbol: str,
    tf: str = "D1",
    n: int = 120,
    since: str | None = None,
    asof: str | None = None,
) -> str:
    """Raw OHLCV price series (reverse-audit channel; no derived indicators).

    Symbols: XAUUSD/XTIUSD/XBRUSD/US500/HG(连续)/DXY + main FX crosses; tf D1|H1.
    Rows carry session_date/ts_utc(UTC-Z)/OHLCV + provenance fields only.
    参数错误以 {"error": "..."} 正常返回（含可用 symbol / tf 清单）——
    不抛异常：SDK 在长 SSE 会话上的工具异常路径存在不回包问题（实测），
    结构化错误既可靠又便于消费方解析。
    """
    try:
        obj = api_get_series(symbol=symbol, tf=tf, n=n, since=since, asof=asof)
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    return json.dumps(obj, ensure_ascii=False, indent=2)


# ------------------------------------------------------------------ assemblage

# Host 白名单：本地直连 + Cloudflare Tunnel 域名（SDK 的 DNS 重绑定防护）
# allowed_origins 放行 claude.ai 网页端（网页端会带 Origin 头调用 MCP）
PUBLIC_HOST = os.environ.get("MCP_PUBLIC_HOST", "data.noip.bid")
ISSUER = os.environ.get("MCP_PUBLIC_URL", f"https://{PUBLIC_HOST}").rstrip("/")
app = mcp.streamable_http_app(
    transport_security=TransportSecuritySettings(
        allowed_hosts=[
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
            PUBLIC_HOST,
            f"{PUBLIC_HOST}:*",
        ],
        allowed_origins=["https://claude.ai"],
    )
)
app.user_middleware.insert(0, Middleware(BearerAuthMiddleware))
# Starlette may have already built the stack; force a rebuild with our layer first.
if getattr(app, "middleware_stack", None) is not None:
    app.middleware_stack = app.build_middleware_stack()


# ------------------------- minimal OAuth 2.1 provider (claude.ai web) --------
# 流程：客户端无凭证请求 /mcp → 401+资源元数据 → /.well-found 发现 → /oauth/register
# 动态注册 → 浏览器打开 /oauth/authorize（输入共享密钥=MCP_AUTH_TOKEN 确认身份）
# → 授权码+PKCE 换 access token → 用 access token 调用 MCP。
# 客户端注册与已签发 token 持久化到 oauth_store.json（重启不丢，claude.ai 不用重连）。

OAUTH_STORE_PATH = BASE_DIR / "oauth_store.json"
ACCESS_TOKEN_TTL = 30 * 24 * 3600
AUTH_CODE_TTL = 300


def _load_oauth_store() -> dict:
    try:
        data = json.loads(OAUTH_STORE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    return {
        "registrations": data.get("registrations", {}),
        "tokens": data.get("tokens", {}),
    }


_OAUTH_STORE = _load_oauth_store()
_AUTH_CODES: dict = {}  # 授权码只存内存，短时效


def _save_oauth_store() -> None:
    OAUTH_STORE_PATH.write_text(
        json.dumps(_OAUTH_STORE, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def _check_oauth_token(token: str) -> bool:
    info = _OAUTH_STORE["tokens"].get(token)
    if not info or info.get("expires", 0) < time.time():
        _OAUTH_STORE["tokens"].pop(token, None)
        return False
    return True


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _pkce_match(verifier: str, challenge: str, method: str) -> bool:
    if method.upper() == "S256":
        return _b64url(hashlib.sha256(verifier.encode()).digest()) == challenge
    return hmac.compare_digest(verifier, challenge)


def _oauth_error(err: str, status: int = 400) -> JSONResponse:
    resp = JSONResponse({"error": err}, status_code=status)
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def oauth_metadata(request: Request):
    return JSONResponse(
        {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/oauth/authorize",
            "token_endpoint": f"{ISSUER}/oauth/token",
            "registration_endpoint": f"{ISSUER}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256", "plain"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["mcp:read"],
        }
    )


async def oauth_protected_resource(request: Request):
    return JSONResponse(
        {
            "resource": f"{ISSUER}/mcp",
            "authorization_servers": [ISSUER],
        }
    )


async def oauth_register(request: Request):
    try:
        body = await request.json()
    except Exception:
        return _oauth_error("invalid_client_metadata")
    redirect_uris = body.get("redirect_uris") or []
    if not redirect_uris:
        return _oauth_error("invalid_client_metadata: redirect_uris required")
    client_id = secrets.token_urlsafe(16)
    record = {
        "client_id": client_id,
        "client_name": body.get("client_name", ""),
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "client_id_issued_at": int(time.time()),
    }
    _OAUTH_STORE["registrations"][client_id] = record
    _save_oauth_store()
    return JSONResponse(record, status_code=201)


_AUTHORIZE_FORM = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MCP 授权</title></head>
<body style="font-family:system-ui,-apple-system,'Segoe UI',sans-serif;background:#f6f7f9">
<div style="max-width:420px;margin:80px auto;background:#fff;padding:32px;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08)">
<h3 style="margin-top:0">data-analysis-mcp 授权请求</h3>
<p style="color:#555">应用 <b>{client}</b> 正在请求访问你的 MCP 数据服务。<br>请输入共享密钥确认身份（即服务端 .env 中的 <code>MCP_AUTH_TOKEN</code>）。</p>
<form method="post" action="/oauth/authorize">
{hidden}
<input type="password" name="secret" placeholder="共享密钥" autocomplete="off"
       style="width:100%;padding:10px;margin-top:8px;border:1px solid #ccc;border-radius:6px;box-sizing:border-box" required>
<button type="submit" style="margin-top:14px;padding:9px 28px;background:#d97706;color:#fff;border:0;border-radius:6px;cursor:pointer">授权</button>
</form></div></body></html>"""


def _auto_reg(client_id: str, redirect_uri: str) -> dict | None:
    """已注册客户端直接用；未知 client_id 但回调址是 claude.ai 的自动补注册
    （claude.ai 会缓存 client_id，服务端清库/重装后仍能完成授权；安全由共享密钥把关）。"""
    reg = _OAUTH_STORE["registrations"].get(client_id)
    if reg:
        return reg if redirect_uri in reg.get("redirect_uris", []) else None
    if redirect_uri.startswith("https://claude.ai/"):
        reg = {
            "client_id": client_id,
            "client_name": "(auto-registered)",
            "redirect_uris": [redirect_uri],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "client_id_issued_at": int(time.time()),
        }
        _OAUTH_STORE["registrations"][client_id] = reg
        _save_oauth_store()
        return reg
    return None


async def oauth_authorize_get(request: Request):
    q = request.query_params
    reg = _auto_reg(q.get("client_id", ""), q.get("redirect_uri", ""))
    if not reg:
        return _oauth_error("invalid_client or redirect_uri", 401)
    keep = ["response_type", "client_id", "redirect_uri", "code_challenge",
            "code_challenge_method", "state", "scope"]
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{html.escape(q.get(k, ""))}">' for k in keep
    )
    return HTMLResponse(
        _AUTHORIZE_FORM.format(client=html.escape(reg.get("client_name") or client_id), hidden=hidden)
    )


async def oauth_authorize_post(request: Request):
    form = await request.form()
    g = lambda k: str(form.get(k, ""))
    if not hmac.compare_digest(g("secret"), AUTH_TOKEN):
        return HTMLResponse("<h3>密钥错误</h3><p><a href='javascript:history.back()'>返回重试</a></p>", status_code=401)
    client_id, redirect_uri = g("client_id"), g("redirect_uri")
    reg = _auto_reg(client_id, redirect_uri)
    if not reg:
        return _oauth_error("invalid_client or redirect_uri", 401)
    code = secrets.token_urlsafe(32)
    _AUTH_CODES[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": g("code_challenge"),
        "code_challenge_method": g("code_challenge_method") or "plain",
        "expires": time.time() + AUTH_CODE_TTL,
    }
    sep = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{sep}code={urllib.parse.quote(code)}"
    if g("state"):
        location += "&state=" + urllib.parse.quote(g("state"))
    return RedirectResponse(location, status_code=302)


async def oauth_token(request: Request):
    form = await request.form()
    g = lambda k: str(form.get(k, ""))
    if g("grant_type") != "authorization_code":
        return _oauth_error("unsupported_grant_type")
    info = _AUTH_CODES.pop(g("code"), None)
    if not info or info["expires"] < time.time():
        return _oauth_error("invalid_grant")
    if g("client_id") != info["client_id"] or g("redirect_uri") != info["redirect_uri"]:
        return _oauth_error("invalid_grant")
    if info["code_challenge"]:
        if not _pkce_match(g("code_verifier"), info["code_challenge"], info["code_challenge_method"]):
            return _oauth_error("invalid_grant: PKCE verification failed")
    access = secrets.token_urlsafe(32)
    _OAUTH_STORE["tokens"][access] = {
        "client_id": info["client_id"],
        "expires": int(time.time()) + ACCESS_TOKEN_TTL,
    }
    _save_oauth_store()
    resp = JSONResponse(
        {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL,
            "scope": "mcp:read",
        }
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


for _route in (
    Route("/.well-known/oauth-authorization-server", oauth_metadata),
    Route("/.well-known/oauth-authorization-server/mcp", oauth_metadata),
    Route("/.well-known/oauth-protected-resource", oauth_protected_resource),
    Route("/.well-known/oauth-protected-resource/mcp", oauth_protected_resource),
    Route("/oauth/register", oauth_register, methods=["POST"]),
    Route("/oauth/authorize", oauth_authorize_get, methods=["GET"]),
    Route("/oauth/authorize", oauth_authorize_post, methods=["POST"]),
    Route("/oauth/token", oauth_token, methods=["POST"]),
):
    app.router.routes.append(_route)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("MCP_PORT", "8420")),
    )
