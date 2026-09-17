"""E-4：MCP OAuth token 重启持久化 E2E 验证。

流程：注册客户端 → 授权（共享密钥表单）→ 换 access token → 用 token 调
tools/list 成功 → **重启 server（全新进程，同一 oauth_store.json）** → 用
同一 token 再次调用 → 必须仍然成功。若失败则 E-4 FAIL（token 只存活于
进程内存）。

附带验证 MCP_AUTH_TOKEN 固定 Bearer 跨重启天然有效（env 驱动）。
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / 'mcp_server' / 'oauth_store.json'
PORT = 23890 + (os.getpid() % 500)


def start_server(token):
    env = dict(os.environ)
    env.update({'MCP_AUTH_TOKEN': token, 'MCP_HOST': '127.0.0.1',
                'MCP_PORT': str(PORT), 'MCP_PUBLIC_HOST': 'localhost'})
    p = subprocess.Popen([sys.executable, str(ROOT / 'mcp_server' / 'server.py')],
                         env=env, cwd=str(ROOT / 'mcp_server'),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(urllib.request.Request(
                f'http://127.0.0.1:{PORT}/mcp', data=b'{}',
                headers={'Content-Type': 'application/json'}), timeout=3)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return p
        except Exception:
            pass
    raise RuntimeError('server not ready')


def stop(p):
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(8)
        except subprocess.TimeoutExpired:
            p.kill()


def base_url():
    return f'http://127.0.0.1:{PORT}'


def form_post(path, data, headers=None):
    body = urllib.parse.urlencode(data).encode()
    h = {'Content-Type': 'application/x-www-form-urlencoded'}
    h.update(headers or {})
    req = urllib.request.Request(base_url() + path, data=body, headers=h)
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def mcp_call(tool_token):
    """用给定 Bearer token 做 initialize+tools/list，返回工具名集合。"""
    url = f'http://127.0.0.1:{PORT}/mcp'
    h = {'Content-Type': 'application/json',
         'Accept': 'application/json, text/event-stream',
         'Authorization': f'Bearer {tool_token}'}
    req = urllib.request.Request(url, data=json.dumps(
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
         'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
                    'clientInfo': {'name': 'e4', 'version': '1'}}}).encode(),
        headers=h)
    resp = urllib.request.urlopen(req, timeout=20)
    sid = resp.headers.get('mcp-session-id')
    resp.read()
    h['mcp-session-id'] = sid
    req = urllib.request.Request(url, data=json.dumps(
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'}).encode(),
        headers=h)
    urllib.request.urlopen(req, timeout=20).read()
    req = urllib.request.Request(url, data=json.dumps(
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}).encode(), headers=h)
    resp = urllib.request.urlopen(req, timeout=20)
    for raw in resp:
        line = raw.decode(errors='replace').strip()
        if line.startswith('data:') and '"id":2' in line:
            r = json.loads(line[5:])
            return {t['name'] for t in r['result']['tools']}
    raise RuntimeError('no tools/list response')


def main():
    secret = f'e4-{uuid.uuid4().hex}'
    # 备份现有 store（不动生产数据）
    backup = None
    if STORE.exists():
        backup = STORE.read_bytes()
    try:
        p = start_server(secret)
        # OAuth dance（register 收 JSON，authorize/token 收表单）
        req = urllib.request.Request(
            base_url() + '/oauth/register',
            data=json.dumps({'redirect_uris':
                             'https://claude.ai/api/mcp/auth_callback',
                             'client_name': 'e4-test'}).encode(),
            headers={'Content-Type': 'application/json'})
        reg = json.loads(urllib.request.urlopen(req, timeout=20).read())
        q = urllib.parse.urlencode({
            'response_type': 'code', 'client_id': reg['client_id'],
            'redirect_uri': 'https://claude.ai/api/mcp/auth_callback',
            'state': 's1'})
        auth_page = urllib.request.urlopen(
            base_url() + f'/oauth/authorize?{q}', timeout=20).read().decode()
        assert '共享密钥' in auth_page, 'authorize 表单未出现'
        # 实际授权：POST 表单（secret 正确）→ 302 带 code
        body = urllib.parse.urlencode({
            'secret': secret, 'response_type': 'code',
            'client_id': reg['client_id'],
            'redirect_uri': 'https://claude.ai/api/mcp/auth_callback',
            'state': 's2'})
        req = urllib.request.Request(
            base_url() + '/oauth/authorize', data=body.encode(),
            headers={'Content-Type': 'application/x-www-form-urlencoded'})

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            opener.open(req, timeout=20)
            print('[FAIL] authorize 未重定向')
            sys.exit(1)
        except urllib.error.HTTPError as e:
            if e.code != 302:
                raise
            loc = e.headers['Location']
        code = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)['code'][0]
        tok = form_post('/oauth/token', {
            'grant_type': 'authorization_code', 'code': code,
            'client_id': reg['client_id'],
            'redirect_uri': 'https://claude.ai/api/mcp/auth_callback'})
        access = tok['access_token']
        tools1 = mcp_call(access)

        def call_retry(token, tries=3):
            for i in range(tries):
                try:
                    return mcp_call(token)
                except urllib.error.HTTPError as e:
                    if e.code != 401 or i == tries - 1:
                        raise
                    time.sleep(1.0)   # readiness 边缘 401 竞态，重试

        # ---- 重启 server（同 store 文件）----
        stop(p)
        time.sleep(1)
        p = start_server(secret)
        tools2 = call_retry(access)   # 同一 token，跨重启
        ok = tools1 == tools2 and 'fx_board' in tools2 and 'get_series' in tools2
        print(f"[{'PASS' if ok else 'FAIL'}] E-4 OAuth token 跨重启有效 "
              f"({len(tools2)} tools, get_pair_context={'get_pair_context' in tools2})")
        # 固定 Bearer 跨重启（env 驱动）
        tools3 = call_retry(secret)
        print(f"[{'PASS' if 'fx_board' in tools3 else 'FAIL'}] "
              f"固定 Bearer 跨重启有效")
        stop(p)
        sys.exit(0 if ok else 1)
    finally:
        # 恢复原 store
        if backup is not None:
            STORE.write_bytes(backup)
        elif STORE.exists():
            STORE.unlink()


if __name__ == '__main__':
    main()
