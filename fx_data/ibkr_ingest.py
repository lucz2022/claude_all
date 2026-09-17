"""IBKR 增补源采集（§1.1 / 附录 A）：HG 铜期货 legs + VIX 指数。

- 本机盈透网关 127.0.0.1:4002（config.IBKR_GATEWAY_*），只读（行情/契约查询）。
- 时间戳铁律（§2.3）：formatDate=2 直接取 UTC epoch，ib_async 返回 tz-aware UTC。
- 到期月合约静默变僵尸报价而非报错——已到期腿按设计保留旧数据（历史拼接用），
  未到期腿停更立即抛错（guard 带 ltd 感知）。
- 已到期 legs 的 conId 通过 reqContractDetails(includeExpired=True) 实时发现，
  不臆造、不硬编码历史 conid。
- 任一 leg/VIX 采集失败默认抛错（不静默沿用旧数据）；--allow-partial 显式放宽。
"""
import numpy as np
import pandas as pd

from . import config, storage
from .symbol_guard import guard_expiring_symbols

try:
    from ib_async import IB, Contract, Future
except ImportError:
    IB = None
    Contract = None
    Future = None

IBKR_VIX_DURATION = '2 Y'      # 覆盖与 MT5 对齐的两年历史（V7 恢复严格分母）
LEG_DURATION = '1 Y'           # 每个 leg 取到期前一年的活跃窗口


def _connect(client_id: int = 77) -> 'IB':
    if IB is None:
        raise RuntimeError('ib_async 包未安装')
    ib = IB()
    ib.connect(config.IBKR_GATEWAY_HOST, config.IBKR_GATEWAY_PORT,
               clientId=client_id, timeout=15)
    return ib


def _bars_to_df(bars, source: str, price_kind: str) -> pd.DataFrame:
    """ib_async BarDataList → 标准 H1 表。ts_utc 必须为 tz-aware（§2.3 铁律 1）。"""
    if not bars:
        raise RuntimeError('no bars returned')
    df = pd.DataFrame({
        'ts_utc': [b.date for b in bars],
        'open': [b.open for b in bars],
        'high': [b.high for b in bars],
        'low': [b.low for b in bars],
        'close': [b.close for b in bars],
        'volume': [b.volume if b.volume and b.volume > 0 else np.nan for b in bars],
    })
    df['ts_utc'] = pd.to_datetime(df['ts_utc'], utc=True)
    if df['ts_utc'].dt.tz is None:
        raise RuntimeError('IBKR 时间戳不是 tz-aware，违反 §2.3 铁律 1')
    df['source'] = source
    df['price_kind'] = price_kind
    df = df.sort_values('ts_utc').reset_index(drop=True)
    # 剔除进行中的 bar（endDateTime='' 时末根可能是未完成小时），只落盘已收盘 bar
    now = pd.Timestamp.utcnow()
    closed = df['ts_utc'] + pd.Timedelta(hours=1) <= now
    return df[closed.to_numpy()].reset_index(drop=True)


def _req_h1(ib, contract, what_to_show: str, duration: str,
            end_datetime: pd.Timestamp | None = None,
            retries: int = 2) -> pd.DataFrame:
    """IBKR 限制：>365 天的时长必须用年单位（如 '1 Y'），否则错误 321 静默返回空。"""
    errs: list[str] = []

    def _on_err(reqId, code, s, c):
        if code not in _NOISE_CODES:
            errs.append(f'{code}: {s}')

    ib.errorEvent += _on_err
    try:
        bars = None
        for attempt in range(retries + 1):
            errs.clear()
            bars = ib.reqHistoricalData(
                contract,
                endDateTime=(end_datetime.strftime('%Y%m%d %H:%M:%S UTC')
                             if end_datetime is not None else ''),
                durationStr=duration,
                barSizeSetting='1 hour', whatToShow=what_to_show,
                useRTH=False, formatDate=2, keepUpToDate=False)
            if bars:
                break
            if attempt < retries:
                import time
                time.sleep(3 + 3 * attempt)  # HMDS 查询节流
    finally:
        ib.errorEvent -= _on_err
    if not bars:
        raise RuntimeError(
            f'no bars returned ({what_to_show}, {duration}, end={end_datetime}); '
            f'ibkr errors: {errs[-3:] or "无"}')
    return _bars_to_df(bars, source='ibkr', price_kind=what_to_show.lower())


_NOISE_CODES = {2104, 2106, 2107, 2108, 2158, 2100, 2101, 2102}


def discover_hg_legs(ib, months_back: int = 30,
                     months_forward: int = 14) -> dict[str, tuple[int, str]]:
    """发现 HG 活跃月合约（含已到期）。返回 {localSymbol: (conId, 'YYYYMMDD')}。

    conId 来自 reqContractDetails(includeExpired=True) 的真实应答——不臆造。
    months_back: 保留到期日不早于该月数的腿（历史拼接深度）。
    months_forward: 只取 14 个月内到期的腿——连续合约的到期规则最多用到
    活跃腿+相邻季；更远的腿（如 2029-2031）HMDS 无历史数据，亦无用途。"""
    cds = ib.reqContractDetails(Future(symbol='HG', exchange='COMEX',
                                       includeExpired=True))
    if not cds:
        raise RuntimeError('HG 契约发现为空——检查网关/权限')
    now = pd.Timestamp.utcnow()
    now = now.tz_localize('UTC') if now.tzinfo is None else now.tz_convert('UTC')
    lo = now - pd.DateOffset(months=months_back)
    hi = now + pd.DateOffset(months=months_forward)
    legs = {}
    for cd in cds:
        c = cd.contract
        lsm, ltd = c.localSymbol, c.lastTradeDateOrContractMonth
        if not lsm or len(lsm) != 4 or lsm[2] not in config.HG_ACTIVE_MONTHS:
            continue
        if len(ltd) != 8:
            continue
        t = pd.Timestamp(ltd, tz='UTC')
        if t < lo or t > hi:
            continue
        legs[lsm] = (int(c.conId), str(ltd))
    if not legs:
        raise RuntimeError('过滤后无 HG legs（months_back 过短?）')
    return legs


def fetch_hg_leg(ib, symbol: str, conid: int, ltd: str,
                 duration: str = LEG_DURATION) -> pd.DataFrame:
    """单个 HG 期货 leg。已到期腿用 endDateTime=到期日+3 天取其活跃窗口。"""
    expired = pd.Timestamp(ltd, tz='UTC') < pd.Timestamp.now('UTC')
    contract = Future(conId=conid, exchange='COMEX', includeExpired=True)
    ib.qualifyContracts(contract)
    end = pd.Timestamp(ltd, tz='UTC') + pd.Timedelta(days=3) if expired else None
    df = _req_h1(ib, contract, 'TRADES', duration, end_datetime=end)
    if df['volume'].isna().all():
        print(f'[WARN] {symbol}: 成交量全缺，检查 COMEX 数据权限')
    return df


def fetch_vix(ib, duration: str = IBKR_VIX_DURATION) -> pd.DataFrame:
    """VIX 指数 @CBOE (IND)，无成交量 → volume 置 NaN（§3.1）。"""
    contract = Contract(conId=config.IBKR_VIX_CONID, exchange='CBOE')
    ib.qualifyContracts(contract)
    last_err = None
    for wts in ('TRADES', 'MIDPOINT'):
        try:
            df = _req_h1(ib, contract, wts, duration)
            df['volume'] = np.nan
            df['price_kind'] = 'last' if wts == 'TRADES' else 'mid'
            return df
        except Exception as e:  # noqa: BLE001 - 权限/品种组合逐个尝试
            last_err = e
    raise RuntimeError(f'VIX 采集失败: {last_err}')


def export_ibkr(legs: dict[str, tuple[int, str]] | None = None,
                discover: bool = True, duration: str = LEG_DURATION,
                vix_duration: str = IBKR_VIX_DURATION,
                save: bool = True, guard: bool = True,
                allow_partial: bool = False) -> dict[str, pd.DataFrame]:
    """采集 HG legs + VIX 并落盘 L0 raw（快照式）。返回 {symbol: H1 df}（含 VIX）。

    legs: {localSymbol: (conId, 'YYYYMMDD')}；None 且 discover=True 时实时发现
    （含已到期腿，保证连续合约历史深度覆盖 V7 严格分母）。"""
    ib = _connect()
    try:
        if legs is None:
            legs = discover_hg_legs(ib) if discover else {
                k: (v[0] if isinstance(v, (tuple, list)) else v,
                    _ltd_from_sidecar(k) or '')
                for k, v in config.IBKR_HG_LEGS.items()}
        out, last_bar_ts, ltd_map, failures = {}, {}, {}, []
        meta = {
            'collected_at_utc': pd.Timestamp.utcnow().isoformat(),
            'canonical_cut_utc': config.CANONICAL_CUT_UTC,
            'gateway': f'{config.IBKR_GATEWAY_HOST}:{config.IBKR_GATEWAY_PORT}',
            'symbols': [], 'source': 'ibkr',
            'pipeline_version': config.PIPELINE_VERSION,
            'legs': {k: {'conid': v[0], 'ltd': v[1]} for k, v in legs.items()},
        }
        for sym, (conid, ltd) in sorted(legs.items()):
            try:
                if not ltd:
                    raise RuntimeError('无 last_trading_date（发现失败?）')
                df = fetch_hg_leg(ib, sym, conid, ltd, duration)
                out[sym] = df
                last_bar_ts[sym] = df['ts_utc'].max()
                ltd_map[sym] = ltd
                meta['symbols'].append(sym)
                if save:
                    storage.write_raw(df, 'ibkr', sym, 'H1')
                    sidecar = config.DIR_RAW / 'ibkr' / f'{sym}__ltd.txt'
                    sidecar.write_text(ltd, encoding='utf-8')
            except Exception as e:  # noqa: BLE001
                failures.append(f'{sym}: {e}')
                print(f'[FAIL] HG leg {sym}: {e}')
        try:
            vix = fetch_vix(ib, vix_duration)
            out['VIX'] = vix
            meta['symbols'].append('VIX')
            if save:
                storage.write_raw(vix, 'ibkr', 'VIX', 'H1')
        except Exception as e:  # noqa: BLE001
            failures.append(f'VIX: {e}')
            print(f'[FAIL] VIX: {e}')
        if failures and not allow_partial:
            raise RuntimeError(
                f'IBKR 采集失败（不允许静默沿用旧数据）: {failures}；'
                f'如确需部分采集请显式 --allow-partial')
        if save:
            storage.write_meta(meta)
        if guard:
            # 僵尸拦截只针对未到期腿；已到期腿的旧数据是设计内的历史拼接材料
            guard_expiring_symbols(last_bar_ts, ltd_map=ltd_map)
        return out
    finally:
        ib.disconnect()


def _ltd_from_sidecar(symbol: str) -> str | None:
    p = config.DIR_RAW / 'ibkr' / f'{symbol}__ltd.txt'
    return p.read_text(encoding='utf-8').strip() if p.exists() else None
