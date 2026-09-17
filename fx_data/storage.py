"""L0-L3 落盘与读取。parquet + 采集元数据 json，对应 §3。

审计要求：raw 不得覆盖丢失上次快照——每次采集写时间戳快照 + latest 副本，
V8 重绘检测读取最近两份快照比较。"""
import json
import re
from pathlib import Path

import pandas as pd

from . import config

_SNAP_RE = re.compile(r'__snap__(\d{8}T\d+Z(?:-\d+)?)\.parquet$')


def write_raw(df: pd.DataFrame, source: str, symbol: str, tf: str,
              meta: dict | None = None) -> Path:
    """L0 raw 落盘：时间戳快照 + latest 副本。旧快照永不覆盖。"""
    d = config.DIR_RAW / source
    d.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.utcnow().strftime('%Y%m%dT%H%M%S%fZ')
    snap = d / f'{symbol}__{tf}__snap__{stamp}.parquet'
    n = 0
    while snap.exists():                   # 同微秒碰撞兜底：永不覆盖
        n += 1
        snap = d / f'{symbol}__{tf}__snap__{stamp}-{n}.parquet'
    df.to_parquet(snap, index=False)
    latest = d / f'{symbol}__{tf}.parquet'
    df.to_parquet(latest, index=False)      # latest 只是快照的副本，真相在快照链
    if meta is not None:
        write_meta(meta)
    return snap


def raw_snapshots(source: str, symbol: str, tf: str) -> list[Path]:
    """按时间升序返回该 symbol 的全部快照路径。"""
    d = config.DIR_RAW / source
    snaps = [p for p in d.glob(f'{symbol}__{tf}__snap__*.parquet') if _SNAP_RE.search(p.name)]
    return sorted(snaps, key=lambda p: _SNAP_RE.search(p.name).group(1))


def read_raw(source: str, symbol: str, tf: str, snapshot: int = -1) -> pd.DataFrame:
    """snapshot=-1 读最新快照（等价 latest）；-2 读上一份；依此类推。"""
    snaps = raw_snapshots(source, symbol, tf)
    if not snaps:
        return pd.read_parquet(config.DIR_RAW / source / f'{symbol}__{tf}.parquet')
    return pd.read_parquet(snaps[snapshot])


def write_sidecar(obj, source: str, name: str) -> Path:
    """采集附属信息（实时点差/point 等）落盘，带时间戳版本。"""
    d = config.DIR_RAW / source
    d.mkdir(parents=True, exist_ok=True)
    if isinstance(obj, pd.DataFrame):
        obj.to_parquet(d / name, index=False)
        return d / name
    (d / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                          encoding='utf-8')
    return d / name


def read_sidecar(source: str, name: str):
    p = config.DIR_RAW / source / name
    if not p.exists():
        return None
    if p.suffix == '.parquet':
        return pd.read_parquet(p)
    return json.loads(p.read_text(encoding='utf-8'))


def raw_exists(source: str, symbol: str, tf: str) -> bool:
    return (config.DIR_RAW / source / f'{symbol}__{tf}.parquet').exists()


def write_norm(df: pd.DataFrame, symbol: str, tf: str) -> Path:
    """L1 norm 落盘：时区统一、日切统一、网格均匀化后的表。"""
    path = config.DIR_NORM / f'{symbol}__{tf}.parquet'
    df.to_parquet(path, index=False)
    return path


def read_norm(symbol: str, tf: str) -> pd.DataFrame:
    return pd.read_parquet(config.DIR_NORM / f'{symbol}__{tf}.parquet')


def norm_exists(symbol: str, tf: str) -> bool:
    return (config.DIR_NORM / f'{symbol}__{tf}.parquet').exists()


def write_qc(report: dict, name: str) -> Path:
    """L2 质量门结果落盘（json）。"""
    path = config.DIR_QC / f'{name}.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                               default=str), encoding='utf-8')
    return path


def write_derived(obj, name: str) -> Path:
    """L3 派生层落盘。DataFrame 存 parquet，其余存 json。

    name='dxy__D1' 的 parquet 附 footer 元数据（window_mode/stats_scope 等，
    A-4 审计要求），并同步写 sidecar json。"""
    if isinstance(obj, pd.DataFrame):
        path = config.DIR_DERIVED / f'{name}.parquet'
        if name == 'dxy__D1':
            import pyarrow as pa
            import pyarrow.parquet as pq
            closes = obj['close'].astype(float)
            meta = {
                'window_mode': 'append_only',
                'stats_scope': 'full_history',
                'window_rows': str(len(obj)),
                'first_session': str(obj['session_date'].iloc[0]),
                'last_session': str(obj['session_date'].iloc[-1]),
                'stats_min': repr(float(closes.min())),
                'stats_max': repr(float(closes.max())),
                'stats_mean': repr(float(closes.mean())),
                'append_only_note': '只追加新会话；源滚动窗滚出的历史行永久保留。'
                                    '首会话固定为 append-only 采用日，不随源前移。',
                'warning': 'append-only 采用日之前的历史从未入存储，全历史统计'
                           '自采用日起算；跨日比较该起点之前不可用。',
            }
            table = pa.Table.from_pandas(obj, preserve_index=False)
            table = table.replace_schema_metadata(
                {**(table.schema.metadata or {}),
                 **{k.encode(): v.encode() for k, v in meta.items()}})
            pq.write_table(table, path)
            sidecar = config.DIR_DERIVED / 'dxy__D1.meta.json'
            sidecar.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                               encoding='utf-8')
        else:
            obj.to_parquet(path, index=False)
    else:
        path = config.DIR_DERIVED / f'{name}.json'
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2,
                                   default=str), encoding='utf-8')
    return path


def append_only_merge(name: str, new: pd.DataFrame) -> pd.DataFrame:
    """A-4：append-only 合并——已存历史行永不删除，仅追加新 session_date。

    可复现规则：同 session_date 以**新表**为准（重算修正），旧表中未出现在
    新表的行全部保留（即便已滚出 MT5 源抓取窗）。"""
    path = config.DIR_DERIVED / f'{name}.parquet'
    if not path.exists():
        return new.reset_index(drop=True)
    old = pd.read_parquet(path)
    merged = pd.concat([
        old[~old['session_date'].astype(str).isin(set(new['session_date'].astype(str)))],
        new])
    return merged.sort_values('session_date').reset_index(drop=True)


def read_derived(name: str):
    path_json = config.DIR_DERIVED / f'{name}.json'
    path_pq = config.DIR_DERIVED / f'{name}.parquet'
    if path_json.exists():
        return json.loads(path_json.read_text(encoding='utf-8'))
    if path_pq.exists():
        return pd.read_parquet(path_pq)
    raise FileNotFoundError(f'derived {name} not found')


def derived_exists(name: str) -> bool:
    return ((config.DIR_DERIVED / f'{name}.json').exists()
            or (config.DIR_DERIVED / f'{name}.parquet').exists())


def write_meta(meta: dict) -> Path:
    """每次采集写一份元数据（§3.2）。"""
    ts = meta.get('collected_at_utc', 'unknown').replace(':', '-')
    path = config.DIR_RAW / 'meta' / f'{ts}.json'
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                    encoding='utf-8')
    return path


def load_all_norm(tf: str, symbols: list[str]) -> dict[str, pd.DataFrame]:
    """读取一批 symbol 的 L1 表，缺文件直接抛错。"""
    out = {}
    for s in symbols:
        out[s] = read_norm(s, tf)
    return out
