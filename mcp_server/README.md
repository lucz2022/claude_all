# 数据分析 MCP 服务（Cloudflare Tunnel + Bearer Token）

为 `C:\data\claude_all\data\derived` 目录提供远程数据清洗/分析 MCP 工具，
供 Claude Code 通过公网 HTTPS 调用。认证采用固定 Bearer Token（数据敏感度低，不走完整 OAuth 2.1）。

## 架构

```
Claude Code ──HTTPS + Bearer Token──▶ Cloudflare Tunnel (data.noip.bid)
                                        │
                                        ▼
                          cloudflared (本机) ──▶ 127.0.0.1:22335
                                        │
                          BearerAuthMiddleware（常量时间比较，错误一律 401）
                                        │
                          MCPServer (mcp 2.x, Streamable HTTP)
                                        │
                          pandas / duckdb 工具（路径白名单锁死在数据目录内）
```

## 文件说明

| 文件 | 作用 |
|---|---|
| `server.py` | MCP Server + Bearer 认证中间件 + 10 个工具 |
| `.env` | Token 与监听配置（**已加入 .gitignore，勿提交/外传**） |
| `requirements.txt` | Python 依赖 |
| `start.bat` | 手动启动服务 |
| `claude-code.mcp.json` | Claude Code 客户端配置模板（占位符需替换） |

## 工具清单

通用：`list_data_files` / `read_json` / `read_parquet` / `sql_query`(DuckDB 只读) / `clean_csv` / `clean_json`
领域：`fx_board`(货币强弱板 w20/w50) / `fx_env`(市场环境快照) / `fx_board_compare`(快慢板对比) / `dxy_series`(美元指数日线)

安全约束：

- 服务只监听 `127.0.0.1:22335`，外部无法绕过隧道直连
- 所有文件路径解析后必须落在 `MCP_DATA_ROOT` 内，扩展名白名单：json/jsonl/ndjson/csv/tsv/parquet
- `sql_query` 仅允许 SELECT/WITH，并重写/校验 SQL 中引用的文件路径，禁写关键词直接拒绝
- Token 用 `hmac.compare_digest` 常量时间比较

## 启动

```bash
pip install -r requirements.txt          # 已安装
python -m uvicorn server:app --host 127.0.0.1 --port 22335   # 或 start.bat
```

## Cloudflare Tunnel（由使用方自行部署）

本服务只负责 `127.0.0.1:22335`。Tunnel 侧接入要点：

- **ingress / Public Hostname 目标填：`http://127.0.0.1:22335`**
- 对外路径就是 MCP 端点：`https://<你的域名>/mcp`
- 认证由本服务自己完成（Bearer Token 401 拦截），Tunnel 无需配置 Access 策略

若用 CLI 方式（cloudflared 已装在 `C:\Program Files (x86)\cloudflared\cloudflared.exe`，winget 安装）：

```bash
CF="/c/Program Files (x86)/cloudflared/cloudflared.exe"

"$CF" tunnel login                                   # 选择 noip.bid
"$CF" tunnel create data-mcp
"$CF" tunnel route dns data-mcp data.noip.bid
"$CF" tunnel run data-mcp                            # 配置见下方 config.yml
```

`~/.cloudflared/config.yml`：

```yaml
tunnel: data-mcp
credentials-file: C:\Users\Administrator\.cloudflared\<TUNNEL-ID>.json
ingress:
  - hostname: data.noip.bid
    service: http://127.0.0.1:22335
  - service: http_status:404
```

常驻（可选，管理员权限）：`"$CF" service install`。

MCP 服务本身的常驻可用任务计划程序（开机启动 `start.bat`）或 NSSM 注册为服务。

## Claude Code 接入

方式一（命令行，token 在 mcp_server/.env 里）：

```bash
claude mcp add data-analysis --transport http --url https://data.noip.bid/mcp \
  --header "Authorization: Bearer <TOKEN>"
```

方式二（项目 `.mcp.json`）：把 `claude-code.mcp.json` 内容拷入项目根目录的 `.mcp.json`，
将 `__TOKEN__` 替换为 `.env` 里的 `MCP_AUTH_TOKEN`。

## 自检清单

1. 本地：`curl -X POST http://127.0.0.1:22335/mcp ...`（不带 Token）→ 401
2. 公网（Tunnel 就绪后）：`curl -H "Authorization: Bearer <TOKEN>" https://data.noip.bid/mcp` → MCP 握手响应
3. 公网不带 Token → 401
4. MCP 服务常驻（`start.bat` / 任务计划）；`claude mcp list` 状态正常

## Token 轮换

```bash
python -c "import secrets; print(secrets.token_hex(32))"   # 新 token
# 更新 .env 后重启 uvicorn，并同步更新 Claude Code 配置里的 header
```

## claude.ai 网页端 Custom Connector（已实现）

服务端内置最小 OAuth 2.1 Provider，与固定 Bearer Token 并存：

- `/.well-known/oauth-authorization-server`（含 `/mcp` 后缀变体）：授权服务器元数据
- `/.well-known/oauth-protected-resource`：资源元数据（401 时引导客户端发现）
- `POST /oauth/register`：RFC 7591 动态客户端注册
- `GET/POST /oauth/authorize`：授权页，输入共享密钥（= `.env` 的 `MCP_AUTH_TOKEN`）确认身份
- `POST /oauth/token`：授权码 + PKCE 换 access token（30 天有效）

网页端接入：claude.ai → Settings → Connectors → 添加自定义连接器，
URL 填 `https://data.noip.bid/mcp`，保存后弹出授权页时输入共享密钥即可。
客户端注册与已签发 token 持久化在 `oauth_store.json`，服务重启不丢失。
