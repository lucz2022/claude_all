"""data MCP v1.1 FINAL acceptance（任务书 §13-§24）。

两层：ENGINE（内部 API，unit validation）+ MCP E2E（真实 server + tools/call）。

E2E 启动真实 mcp_server/server.py（localhost 随机端口 + 临时 token，不连公网），
经 MCP streamable-http 协议调用 tools/list 与 tools/call。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from acceptance_rules_v1_1 import ALLOWED_ROW, BANNED, ROW_REQUIRED_D1  # noqa: E402

RESULTS = []
BLOCKING = []


def rec(item, status, detail=''):
    RESULTS.append((item, status, detail))
    print(f'{item} {status}' + (f'  -- {detail}' if detail else ''), flush=True)
    if status == 'FAIL':
        BLOCKING.append(item)


# ================================================================ MCP client
class McpHttpClient:
    """极简 MCP streamable-http 客户端（JSON-RPC over HTTP POST）。"""

    def __init__(self, url, token):
        self.url = url
        self.token = token
        self.session_id = None
        self._id = 0

    def _post(self, payload, raw_error=False):
        data = json.dumps(payload).encode()
        headers = {'Content-Type': 'application/json',
                   'Accept': 'application/json, text/event-stream',
                   'Authorization': f'Bearer {this_token}'}
        if self.session_id:
            headers['mcp-session-id'] = self.session_id
        req = urllib.request.Request(self.url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                sid = resp.headers.get('mcp-session-id')
                if sid:
                    self.session_id = sid
                ct = resp.headers.get('Content-Type', '')
                if 'text/event-stream' in ct:
                    # streamable-http：逐行读 SSE，拿到本请求 id 的 data 即返回
                    want = f'"id":{payload.get("id")}'
                    lines = []
                    for raw in resp:
                        line = raw.decode(errors='replace').rstrip(chr(10))
                        lines.append(line)
                        if line.startswith('data:') and want in line:
                            try:
                                return json.loads(line[5:].strip())
                            except json.JSONDecodeError:
                                continue
                    return self._parse_sse('\n'.join(lines))
                body = resp.read().decode()
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            if raw_error:
                return {'_http_error': e.code, '_body': body}
            raise RuntimeError(f'HTTP {e.code}: {body[:400]}')

    @staticmethod
    def _parse_sse(body):
        """text/event-stream：取最后一个 data: JSON（响应体）。"""
        for line in reversed(body.splitlines()):
            if line.startswith('data:'):
                payload = line[5:].strip()
                try:
                    obj = json.loads(payload)
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    continue
        return {}

    def rpc(self, method, params=None, raw_error=False):
        self._id += 1
        payload = {'jsonrpc': '2.0', 'id': self._id, 'method': method}
        if params is not None:
            payload['params'] = params
        if self.session_id:
            # headers 只能经 Request 传，会话头在 _post 里补
            pass
        return self._post(payload, raw_error=raw_error)

    def initialize(self):
        r = self._post({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize',
                        'params': {'protocolVersion': '2025-03-26',
                                   'capabilities': {},
                                   'clientInfo': {'name': 'acceptance',
                                                  'version': '1.0'}}})
        # notifications/initialized
        data = json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}).encode()
        headers = {'Content-Type': 'application/json',
                   'Accept': 'application/json, text/event-stream',
                   'Authorization': f'Bearer {this_token}'}
        if self.session_id:
            headers['mcp-session-id'] = self.session_id
        req = urllib.request.Request(self.url, data=data, headers=headers)
        urllib.request.urlopen(req, timeout=30).read()
        return r

    def list_tools(self):
        return self.rpc('tools/list')

    def call(self, name, args=None):
        return self.rpc('tools/call', {'name': name,
                                       'arguments': args or {}})


def tool_text(resp):
    """提取 tools/call 返回的 text 内容。"""
    content = resp.get('result', {}).get('content', [])
    for c in content:
        if c.get('type') == 'text':
            return c['text']
    raise RuntimeError(f'no text content: {json.dumps(resp)[:300]}')


def tool_error(resp):
    """tools/call 的 isError 或结构化错误文本。"""
    if resp.get('result', {}).get('isError'):
        return tool_text(resp)
    return None


# ================================================================ server 启动
import random
PORT = random.randint(20000, 24000)
this_token = None
proc = None


def start_server():
    global this_token, proc
    this_token = f'acc-{uuid.uuid4().hex}'
    env = dict(os.environ)
    env.update({
        'MCP_AUTH_TOKEN': this_token,
        'MCP_DATA_ROOT': str(ROOT / 'data' / 'derived'),
        'MCP_HOST': '127.0.0.1',
        'MCP_PORT': str(PORT),
        'MCP_PUBLIC_HOST': 'localhost',
    })
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / 'mcp_server' / 'server.py')],
        env=env, cwd=str(ROOT / 'mcp_server'),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    url = f'http://127.0.0.1:{PORT}/mcp'
    for _ in range(60):
        time.sleep(0.5)
        if proc.poll() is not None:
            out = proc.stdout.read().decode(errors='replace')
            raise RuntimeError(f'server exited early:\n{out[-800:]}')
        try:
            urllib.request.urlopen(urllib.request.Request(
                url, data=b'{}', headers={'Content-Type': 'application/json'}),
                timeout=3)
        except urllib.error.HTTPError as e:
            if e.code == 401:      # 已起来且鉴权在工作
                return url
        except Exception:
            continue
    raise RuntimeError('server not ready')


def stop_server():
    global proc
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    proc = None


# ================================================================ helpers
def collect_keys(obj, acc=None):
    acc = set() if acc is None else acc
    if isinstance(obj, dict):
        for k, v in obj.items():
            acc.add(str(k))
            collect_keys(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            collect_keys(v, acc)
    return acc


def parse_compare_rows(text):
    """逐行 parse compare 输出的 Δ(a)/Δ(b)/符号。"""
    rows = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 6 and parts[0].isalpha() and len(parts[0]) == 3:
            try:
                da, db = float(parts[3]), float(parts[4])
            except ValueError:
                continue
            sign = parts[5]
            rows[parts[0]] = (da, db, sign)
    return rows


def main():
    print('=== data MCP v1.1 FINAL acceptance ===', flush=True)
    # 运行元数据（双跑对比时忽略本节，只比 PASS/FAIL/PENDING 结论）
    import subprocess as _sp, os as _os, uuid as _uuid
    _sha = _sp.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                   capture_output=True, text=True).stdout.strip()
    print(f'RUN_ID: {_uuid.uuid4().hex[:8]}', flush=True)
    print(f'PID: {_os.getpid()}', flush=True)
    print(f'SERVER_PORT: {PORT}', flush=True)
    print(f'STARTED_AT_UTC: {pd.Timestamp.now("UTC").isoformat()}', flush=True)
    print(f'COMMIT: {_sha}', flush=True)
    print('', flush=True)

    # ---------------- ENGINE（内部 API 层，unit validation） ----------------
    from fx_data.api import get_env_state, get_series, get_strength_board
    from fx_data.summary import board_summary

    env = get_env_state()
    ok = (env.get('schema_version') == '1.1'
          and 'REFLATION' in env['regime_type']
          and env['regime_inputs']['reflation_value']
          == env['ratios']['cu_au']['slope_5d'])
    rec('A-1 ENGINE', 'PASS' if ok else 'FAIL', env['regime_type'])

    b20 = get_strength_board(20)
    b50 = get_strength_board(50)
    ok = all(abs(c['delta'] - (c['z_short'] - c['z'])) < 1e-9
             and abs(c['purity'] - max(c['membership'].values())) < 1e-9
             and c['state_ambiguous'] == (c['purity'] < 0.60)
             for b in (b20, b50) for c in b['currencies'])
    rec('A-2 ENGINE', 'PASS' if ok else 'FAIL', 'JSON invariants')

    # A-5 NONE：四条件（无累计状态/只依赖窗口/前缀无关/幂等）
    import inspect
    import fx_data.board as bd
    bsrc = inspect.getsource(bd)
    no_cum = 'cumsum' not in bsrc and 'cumprod' not in bsrc
    two_pt = 'np.log(c[-1]) - np.log(c[-1 - window])' in bsrc
    r1 = subprocess.run([sys.executable,
                         str(ROOT / 'tests' / 'test_a5_prefix_independence.py')],
                        capture_output=True, text=True, cwd=str(ROOT))
    prefix_ok = r1.returncode == 0
    b20b = get_strength_board(20)
    idem = all(abs(a['z'] - b['z']) < 1e-12 for a, b in
               zip(b20['currencies'], b20b['currencies']))
    rec('A-5 ENGINE', 'PASS' if (no_cum and two_pt and prefix_ok and idem) else 'FAIL',
        f'NONE: no_cum={no_cum}, two_point={two_pt}, prefix_indep={prefix_ok}, '
        f'idempotent={idem}')

    # A-4 跨 session 状态机（Day1=永久 baseline，绝不覆盖；Day2=当前验证样本）
    from fx_data import storage
    import pyarrow.parquet as pq
    md = pq.read_schema(ROOT / 'data/derived/dxy__D1.parquet').metadata
    import subprocess as _sp
    _sha = _sp.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                   capture_output=True, text=True).stdout.strip()[:12]
    current = {'schema': 'A4-dxy-cross-session-v1',
               'captured_at_utc': pd.Timestamp.now('UTC').isoformat(),
               'commit': _sha,
               'rows': int(md[b'window_rows']),
               'first_session': md[b'first_session'].decode(),
               'last_session': md[b'last_session'].decode(),
               'stats_min': float(md[b'stats_min']),
               'stats_max': float(md[b'stats_max']),
               'stats_mean': float(md[b'stats_mean'])}
    day1_path = ROOT / 'docs/v1.1-acceptance/A4_dxy_day1.json'
    day2_path = ROOT / 'docs/v1.1-acceptance/A4_dxy_day2.json'
    # replay 验证（simulation，不冒充真实跨日）
    import shutil
    tmp = ROOT / 'data/derived/_a4r'
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    orig = storage.config.DIR_DERIVED
    try:
        storage.config.DIR_DERIVED = tmp
        storage.write_derived(pd.DataFrame(
            {'session_date': [current['first_session'], 'x'],
             'close': [1.0, 2.0]}), 'p__D1')
        m2 = storage.append_only_merge('p__D1', pd.DataFrame(
            {'session_date': ['x', 'y'], 'close': [2.1, 3.0]}))
        replay = (len(m2) == 3 and str(m2['session_date'].iloc[0])
                  == current['first_session'])
    finally:
        storage.config.DIR_DERIVED = orig
        shutil.rmtree(tmp, ignore_errors=True)

    if not day1_path.exists():
        # 状态 1：Day1 不存在 → 首次捕获（此后绝不覆盖）
        day1_path.write_text(json.dumps(current, ensure_ascii=False, indent=2),
                             encoding='utf-8')
        day1 = current
        rec('A-4-REALTIME', 'PENDING_REAL_NEXT_SESSION',
            f"Day1 captured: {current['rows']}r "
            f"{current['first_session']}..{current['last_session']}; "
            f"replay={replay}")
    else:
        day1 = json.loads(day1_path.read_text(encoding='utf-8'))
        if current['last_session'] == day1['last_session']:
            # 状态 2：无新 session → 保持 Day1，不写 Day2
            rec('A-4-REALTIME', 'PENDING_REAL_NEXT_SESSION',
                f"no new real session; Day1 preserved: {day1['rows']}r "
                f"{day1['first_session']}..{day1['last_session']}; "
                f"current={current['rows']}r; replay={replay}")
        elif current['last_session'] > day1['last_session']:
            # 状态 3：真实下一 session → 写 Day2 并做正式跨日验收
            day2_path.write_text(json.dumps(current, ensure_ascii=False,
                                            indent=2), encoding='utf-8')
            day2 = current
            stored = storage.read_derived('dxy__D1')
            first_still_exists = (day1['first_session']
                                  in set(stored['session_date'].astype(str)))
            real_ok = (replay
                       and day2['rows'] > day1['rows']
                       and day2['first_session'] == day1['first_session']
                       and day2['last_session'] > day1['last_session']
                       and first_still_exists)
            rec('A-4-REALTIME', 'PASS' if real_ok else 'FAIL',
                f"Day1={day1['rows']}r/"
                f"{day1['first_session']}..{day1['last_session']} "
                f"Day2={day2['rows']}r/"
                f"{day2['first_session']}..{day2['last_session']} "
                f"first_preserved={first_still_exists}")
        else:
            rec('A-4-REALTIME', 'FAIL',
                'current last_session regressed behind Day1 '
                f"({current['last_session']} < {day1['last_session']})")

    # ---------------- MCP E2E ----------------
    print('', flush=True)
    print('MCP E2E', flush=True)
    url = None
    try:
        url = start_server()
        cli = None
        for _attempt in range(3):        # 401 竞态重试（readiness 边缘偶发）
            try:
                cli = McpHttpClient(url, this_token)
                cli.initialize()
                break
            except RuntimeError as e:
                if '401' not in str(e) or _attempt == 2:
                    raise
                time.sleep(1.0)

        # tools/list
        tl = cli.list_tools()
        names = {t['name'] for t in tl.get('result', {}).get('tools', [])}
        need = {'fx_board', 'fx_env', 'fx_board_compare', 'dxy_series', 'get_series'}
        rec('TOOLS-LIST', 'PASS' if need <= names else 'FAIL',
            f'{sorted(names)}')
        (ROOT / 'docs/v1.1-acceptance/mcp_tools_list.txt').write_text(
            '\n'.join(sorted(names)), encoding='utf-8')

        if 'get_series' not in names:
            for a in ('A-6', 'A-7', 'A-8 B1', 'A-8 B2', 'A-8 B3', 'A-8 B4'):
                rec(a, 'FAIL', 'get_series 未注册')

        # A-1/A-9 E2E：fx_env
        env_txt = tool_text(cli.call('fx_env'))
        (ROOT / 'docs/v1.1-acceptance/mcp_fx_env.txt').write_text(
            env_txt, encoding='utf-8')
        a1 = ('schema_version: 1.1' in env_txt
              and 'REFLATION' in env_txt and 'RATES_' not in env_txt
              and 'reflation_value' in env_txt and 'SLOPE_EPS=0.002' in env_txt)
        rec('A-1', 'PASS' if a1 else 'FAIL', 'tools/call fx_env')
        a9 = ('gauge_underlying_concentration: 0.75' in env_txt
              and 'gauge_self_reference_warning: XAUUSD' in env_txt
              and '⚠' in env_txt
              and 'VIX' not in env_txt.split('gauge_underlying_exposure:')[1]
              .split('gauge_underlying_concentration')[0])
        rec('A-9', 'PASS' if a9 else 'FAIL',
            'concentration/warning/⚠ + VIX 不在 exposure')

        # A-2 E2E：fx_board
        board_txt = tool_text(cli.call('fx_board', {'window': 20}))
        (ROOT / 'docs/v1.1-acceptance/mcp_fx_board_w20.txt').write_text(
            board_txt, encoding='utf-8')
        bang = sum(1 for line in board_txt.splitlines()
                   if line[:3].isalpha() and len(line.split()) >= 6 and '(!)' in line)
        hdr = all(s in board_txt for s in ('staleness_hours', 'dispersion',
                                           'board_vol', 'residual_rms'))
        rank = board_txt.split('动能转折排行 (|Δ| desc): ')[-1].split(' | ')
        top2 = [r.split()[0] for r in rank[:2]]
        a2 = hdr and bang == 4 and top2[:2] == ['JPY', 'USD'] \
            and '动能转折排行' in board_txt and 'purity' in board_txt
        rec('A-2', 'PASS' if a2 else 'FAIL',
            f'(!)x{bang}, top2={top2}, header ok={hdr}')

        # A-3 E2E：fx_board_compare 逐行符号独立核算
        cmp_txt = tool_text(cli.call('fx_board_compare'))
        (ROOT / 'docs/v1.1-acceptance/mcp_fx_board_compare.txt').write_text(
            cmp_txt, encoding='utf-8')
        rows = parse_compare_rows(cmp_txt)
        a3 = len(rows) == 8
        expected_conflicts = set()
        for ccy, (da, db, sign) in rows.items():
            same = (da == 0 or db == 0) or ((da > 0) == (db > 0))
            want = '一致' if same else '冲突(!)'
            if want == '冲突(!)':
                expected_conflicts.add(ccy)
            if want not in sign:
                a3 = False
        rec('A-3', 'PASS' if a3 else 'FAIL',
            f'逐行符号独立核算 {len(rows)} 行; 冲突={sorted(expected_conflicts)}')

        # A-4 METADATA（E2E）：dxy_series 元数据口径（与 A-4-REALTIME 分状态）
        dxy_txt = tool_text(cli.call('dxy_series', {'n': 5}))
        a4m = ('window_mode: append_only' in dxy_txt
               and 'stats_scope: full_history' in dxy_txt
               and 'first_session: 2025-03-04' in dxy_txt)
        rec('A-4-METADATA', 'PASS' if a4m else 'FAIL',
            'dxy_series 元数据（append_only/full_history/first_session）')

        # A-6 E2E：完整 contract
        x120 = json.loads(tool_text(cli.call(
            'get_series', {'symbol': 'XAUUSD', 'tf': 'D1', 'n': 120})))
        (ROOT / 'docs/v1.1-acceptance/mcp_get_series_XAUUSD_D1_n120.json'
         ).write_text(json.dumps(x120, ensure_ascii=False, indent=2),
                      encoding='utf-8')
        rows_ok = all(ROW_REQUIRED_D1 <= r.keys() for r in x120['rows'])
        utc_ok = all(r['ts_utc'].endswith('Z') for r in x120['rows'])
        utc_ok = utc_ok and all(
            (lambda ts: ts.tzinfo is not None and ts.tz_convert('UTC') == ts)(
                pd.Timestamp(r['ts_utc']))
            for r in x120['rows'][:10])
        b_ok = (x120['session_boundary_utc'] == 22)
        xti = json.loads(tool_text(cli.call(
            'get_series', {'symbol': 'XTIUSD', 'n': 120})))
        hgs = json.loads(tool_text(cli.call(
            'get_series', {'symbol': 'HG', 'n': 120})))
        gor = x120['rows'][-1]['close'] / xti['rows'][-1]['close']
        cua = hgs['rows'][-1]['close'] / x120['rows'][-1]['close']
        c1 = x120['rows'][-1]['session_date'] == env['effective_session']
        c2 = abs(gor / env['ratios']['gor']['level'] - 1) < 1e-3
        c3 = abs(cua / env['ratios']['cu_au']['level'] - 1) < 1e-3
        rec('A-6', 'PASS' if (rows_ok and utc_ok and b_ok and c1 and c2 and c3)
            else 'FAIL',
            f'contract={rows_ok}, UTC-Z={utc_ok}, boundary={b_ok}, '
            f'session={c1}, GOR={abs(gor/env["ratios"]["gor"]["level"]-1):.1e}, '
            f'CU/AU={abs(cua/env["ratios"]["cu_au"]["level"]-1):.1e}')

        # A-7 E2E：递归全树扫描
        x10 = json.loads(tool_text(cli.call(
            'get_series', {'symbol': 'XAUUSD', 'tf': 'D1', 'n': 10})))
        (ROOT / 'docs/v1.1-acceptance/mcp_get_series_XAUUSD_D1_n10.json'
         ).write_text(json.dumps(x10, ensure_ascii=False, indent=2),
                      encoding='utf-8')
        all_keys = collect_keys(x10)
        bad = all_keys & BANNED
        row_extra = (set(x10['rows'][0].keys()) - ALLOWED_ROW)
        rec('A-7', 'PASS' if (not bad and not row_extra and len(x10['rows']) == 10)
            else 'FAIL',
            f'banned fields found: {sorted(bad)}; row extra: {sorted(row_extra)}; '
            f'全树 {len(all_keys)} keys 递归')
        (ROOT / 'docs/v1.1-acceptance/A7_recursive_field_scan.txt').write_text(
            f'A-7 MCP E2E 递归扫描（get_series XAUUSD D1 n=10 经 tools/call）\n'
            f'BANNED({len(BANNED)}): {sorted(BANNED)}\n'
            f'全树 keys({len(all_keys)}): {sorted(all_keys)}\n'
            f'banned found: {sorted(bad)}\n'
            f'rows 字段 ⊆ ALLOWED_ROW: {not row_extra}\n'
            f'结论: {"PASS" if not bad and not row_extra else "FAIL"}\n',
            encoding='utf-8')

        # A-8 E2E：四边界（get_series 参数错误以 {"error": ...} 结构化返回，
        # 不抛异常——规避 SDK 长 SSE 会话上工具异常不回包的实测问题）
        b1 = json.loads(tool_text(cli.call(
            'get_series', {'symbol': 'FAKEXYZ'}))).get('error', '')
        rec('A-8 B1', 'PASS' if ('XAUUSD' in b1 and 'FAKEXYZ' in b1) else 'FAIL',
            b1[:80])
        big = json.loads(tool_text(cli.call(
            'get_series', {'symbol': 'XAUUSD', 'n': 99999})))
        rec('A-8 B2', 'PASS' if (big['row_count'] == big['available'] > 100
                                 and big['truncated'] is False) else 'FAIL',
            f"avail={big['available']}")
        b3 = json.loads(tool_text(cli.call(
            'get_series', {'symbol': 'XAUUSD', 'tf': 'M5'}))).get('error', '')
        rec('A-8 B3', 'PASS' if ('D1' in b3 and 'H1' in b3) else 'FAIL',
            b3[:60])
        b4 = json.loads(tool_text(cli.call(
            'get_series', {'symbol': 'HG', 'n': 2000})))
        rec('A-8 B4', 'PASS' if any(r['partial'] for r in b4['rows']) else 'FAIL',
            f"partial rows present ({sum(r['partial'] for r in b4['rows'])})")
        (ROOT / 'docs/v1.1-acceptance/A8_boundaries_mcp.txt').write_text(
            f'B1: {(b1 or "")[:200]}\n\n'
            f"B2: row_count={big['row_count']} available={big['available']} "
            f"truncated={big['truncated']}\n\n"
            f'B3: {(b3 or "")[:200]}\n\n'
            f"B4: partial=true rows={sum(r['partial'] for r in b4['rows'])}"
            f"/{len(b4['rows'])}（正常透出）\n", encoding='utf-8')
    finally:
        stop_server()

    # ---------------- R-1..R-7（engine 基础上 R-7 分窗） ----------------
    print('', flush=True)
    rec('R-1', 'PASS' if env['gauge_sum_check']['abs_diff'] < 1e-9 else 'FAIL',
        f"abs_diff={env['gauge_sum_check']['abs_diff']}")
    rec('R-2', 'PASS' if any('循环论证' in c for c in
                             env['regime_inputs']['caveats']) else 'FAIL', '')
    rr = env['regime_inputs']['reflation_real_source']
    rec('R-3', 'PASS' if (rr.startswith('unavailable') and '不冒充' in rr)
        else 'FAIL', rr[:40] + '…')
    vix = [c for c in env['gauge_components'] if c['name'] == 'vix'][0]
    rec('R-4', 'PASS' if (vix['contrib'] == 0.0 and vix.get('note')) else 'FAIL', '')
    rec('R-5', 'PASS' if (env['staleness_hours'] is not None
                          and env['staleness_status'] == 'ok') else 'FAIL',
        f"{env['staleness_hours']}h")
    rec('R-6', 'PASS' if (len(b20['quality']['residual_by_pair']) == 28
                          and len(b50['quality']['residual_by_pair']) == 28)
        else 'FAIL', 'w20/w50 各 28 pairs')
    for tag, b in (('R-7-w20', b20), ('R-7-w50', b50)):
        vals = [abs(v) for v in b['quality']['residual_by_pair'].values()]
        med = sorted(vals)[len(vals) // 2]
        ok = (b['quality']['residual_rms'] < 5e-4 and max(vals) <= 10 * med)
        rec(tag, 'PASS' if ok else 'FAIL',
            f"rms={b['quality']['residual_rms']:.2e}, "
            f"max/median={max(vals) / med:.1f}")

    # ---------------- 汇总 ----------------
    n_fail = sum(1 for _i, s, _d in RESULTS if s == 'FAIL')
    pending = [i for i, s, _d in RESULTS if s.startswith('PENDING')]
    print('', flush=True)
    print(f'BLOCKING FAILURES: {n_fail}')
    print('PENDING: ' + (', '.join(pending) if pending else 'none'))
    print('ALL PASS' if n_fail == 0 and not pending
          else ('READY_EXCEPT_A4_REAL_TIME_CONFIRMATION'
                if n_fail == 0 else 'NOT READY'))
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == '__main__':
    main()
