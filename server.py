# -*- coding: utf-8 -*-
"""
运行数据分析系统 - FastAPI 后端
启动: python server.py  →  http://localhost:8300
"""
import os
import sys
import json
import time
import datetime
import numpy as np
import pandas as pd

from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from metadata import COLUMNS, GROUPS, KNOWN_ISSUES

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
PARQUET = os.path.join(DATA_DIR, "data.parquet")
META = os.path.join(DATA_DIR, "meta.json")

app = FastAPI(title="数据查看器")

# GZip 压缩响应（大数据 JSON 传输提速）
from fastapi.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=1024)

# ---------- 前台日志（终端 print 同步存环形缓冲，前端轮询） ----------
import threading
from collections import deque

LOG_BUF = deque(maxlen=300)
_LOG_LOCK = threading.Lock()


_ORIG_STDOUT = sys.stdout


def _log(msg: str):
    """同时输出到终端和前台日志缓冲"""
    _ORIG_STDOUT.write(msg + chr(10))
    _ORIG_STDOUT.flush()
    with _LOG_LOCK:
        LOG_BUF.append({"t": datetime.datetime.now().strftime("%H:%M:%S"), "m": msg})




_df: pd.DataFrame | None = None
_meta: dict | None = None


def get_df() -> pd.DataFrame:
    global _df
    if _df is None:
        if not os.path.exists(PARQUET):
            raise HTTPException(500, "data/data.parquet 不存在，请先运行 import_data.py")
        _df = pd.read_parquet(PARQUET)
    return _df


def get_meta() -> dict:
    global _meta
    if _meta is None:
        if os.path.exists(META):
            with open(META, encoding="utf-8") as f:
                _meta = json.load(f)
        else:
            _meta = {}
    return _meta


# ---------- 工具 ----------
def ts_to_unix(v):
    """utc 列 → unix 秒"""
    if isinstance(v, (pd.Timestamp, datetime.datetime)):
        return int(v.timestamp())
    return None


def resample(df: pd.DataFrame, cols: list[str], t0: int, t1: int, step: int, agg="mean"):
    """按 step 秒聚合。返回 {col: {t:[], v:[]}}"""
    sub = df[(df["unix time"] >= t0) & (df["unix time"] <= t1)]
    if len(sub) == 0:
        return {}
    out = {}
    n = len(sub)
    # 目标点数上限 5000
    max_pts = 5000
    actual_step = step
    while (t1 - t0) / actual_step > max_pts:
        actual_step *= 2
    bins = ((sub["unix time"] - t0) // actual_step).astype(int)
    g = sub.groupby(bins)
    for c in cols:
        if c not in sub.columns:
            continue
        s = g[c]
        if agg == "mean":
            v = s.mean()
        elif agg == "max":
            v = s.max()
        elif agg == "min":
            v = s.min()
        else:
            v = s.mean()
        out[c] = {
            "t": (v.index * actual_step + t0).tolist(),
            "v": [None if pd.isna(x) else round(float(x), 4) for x in v.tolist()],
        }
    out["_step"] = actual_step
    return out


# ---------- API ----------
@app.get("/api/meta")
def api_meta():
    m = get_meta()
    return {
        "rows": m.get("rows"),
        "t_start": m.get("t_start"),
        "t_end": m.get("t_end"),
        "duration_hours": m.get("duration_hours"),
        "source_file": m.get("source_file"),
        "import_time": m.get("import_time"),
        "columns": m.get("columns", {}),
        "derived_columns": m.get("derived_columns", {}),
        "known_issues": m.get("known_issues", {}),
        "cam_note": m.get("cam_note", ""),
        "me_pwr_note": m.get("me_pwr_note", ""),
        "groups": GROUPS,
    }


@app.get("/api/overview")
def api_overview():
    """总览页：KPI + 1分钟降采样关键曲线 + 状态分段"""
    df = get_df()
    t0 = int(df["unix time"].iloc[0])
    t1 = int(df["unix time"].iloc[-1])

    # KPI
    rotor_on_frac = float(df["ROTOR_ON"].mean())
    cam_on_frac = float(df["VESSEL.STAT1.CAM"].mean())
    sys_run_frac = float(df["VESSEL.STAT0_SYS.RUNNING"].mean())
    sog_mean = float(df["VESSEL.SOG"].mean())
    tws_mean = float(df["TWS"].mean())
    foc_me_mean = float(df["PMS.FOC.ME"].mean())
    sfoc_mean = float(df["SFOC"].mean())
    dist_nm = float(df["VESSEL.SOG"].sum() / 3600)  # kn·s → nm
    rotor_pwr_mean = float(df["ROTOR_PWR_TOT"].mean())

    # 关键曲线 1min
    cols = ["VESSEL.SOG", "PMS.FOC.ME", "ME.PWR_cal", "TWS", "AN1.AWS", "ROTOR_N", "ROTOR_PWR_TOT", "VESSEL.RA1"]
    curves = resample(df, cols, t0, t1, 60)

    # 状态分段（ROTOR_ON 变化 + SYS.RUNNING 变化，段长>60s）
    seg_key = (df["ROTOR_ON"].astype(int) * 2 + df["VESSEL.STAT0_SYS.RUNNING"].astype(int)).to_numpy()
    change = np.where(np.diff(seg_key) != 0)[0]
    starts = np.concatenate([[0], change + 1])
    ends = np.concatenate([change, [len(seg_key) - 1]])
    segs = []
    ut = df["unix time"].to_numpy()
    for s, e in zip(starts, ends):
        dur = int(ut[e] - ut[s])
        if dur < 60:
            continue
        segs.append({
            "t0": int(ut[s]), "t1": int(ut[e]), "dur_min": round(dur / 60, 1),
            "rotor_on": bool(seg_key[s] >= 2), "sys_run": bool(seg_key[s] % 2 == 1),
        })
    # 合并相邻同状态短段
    merged = []
    for s in segs:
        if merged and merged[-1]["rotor_on"] == s["rotor_on"] and merged[-1]["sys_run"] == s["sys_run"]:
            merged[-1]["t1"] = s["t1"]
            merged[-1]["dur_min"] = round((s["t1"] - merged[-1]["t0"]) / 60, 1)
        else:
            merged.append(s)

    return {
        "kpi": {
            "rotor_on_frac": round(rotor_on_frac, 3),
            "cam_on_frac": round(cam_on_frac, 3),
            "sys_run_frac": round(sys_run_frac, 3),
            "sog_mean": round(sog_mean, 2),
            "tws_mean": round(tws_mean, 2),
            "foc_me_mean": round(foc_me_mean, 1),
            "sfoc_mean": round(sfoc_mean, 1),
            "dist_nm": round(dist_nm, 1),
            "rotor_pwr_mean": round(rotor_pwr_mean, 1),
        },
        "curves": curves,
        "segments": merged,
    }


@app.get("/api/timeseries")
def api_timeseries(
    cols: str = Query(..., description="逗号分隔列名"),
    t0: int = Query(...),
    t1: int = Query(...),
    step: int = Query(60, description="聚合步长(秒)"),
    agg: str = Query("mean"),
):
    df = get_df()
    col_list = [c.strip() for c in cols.split(",") if c.strip()]
    for c in col_list:
        if c not in df.columns:
            raise HTTPException(400, f"未知列: {c}")
    return resample(df, col_list, t0, t1, step, agg)


@app.get("/api/track")
def api_track(step: int = Query(60)):
    """航迹：降采样经纬度 + 状态/指标"""
    df = get_df()
    t0 = int(df["unix time"].iloc[0])
    t1 = int(df["unix time"].iloc[-1])
    cols = ["LAT_DEC", "LON_DEC", "VESSEL.SOG", "PMS.FOC.ME", "TWS", "ROTOR_ON", "VESSEL.HDT", "VESSEL.COG"]
    r = resample(df, cols, t0, t1, step)
    return r


@app.get("/api/cam")
def api_cam():
    """CAM 对比分析：旋筒开/关时段自动分段 → 配对 → 指标对比"""
    df = get_df()
    ut = df["unix time"].to_numpy()
    rotor = df["ROTOR_ON"].to_numpy().astype(int)
    sysr = df["VESSEL.STAT0_SYS.RUNNING"].to_numpy().astype(int)

    # 分段：ROTOR_ON 且 SYS.RUNNING=1 为 ON 段；ROTOR_OFF 且 SYS.RUNNING=1 为 OFF 段
    state = np.where(sysr == 1, rotor, 2)  # 2=系统停机
    change = np.where(np.diff(state) != 0)[0]
    starts = np.concatenate([[0], change + 1])
    ends = np.concatenate([change, [len(state) - 1]])

    MIN_SEG = 600  # 最短10分钟
    segs = []
    for s, e in zip(starts, ends):
        dur = int(ut[e] - ut[s])
        if dur < MIN_SEG or state[s] == 2:
            continue
        segs.append({"t0": int(ut[s]), "t1": int(ut[e]), "dur_min": round(dur / 60, 1), "on": bool(state[s] == 1)})

    # 段指标
    def seg_metrics(s, e):
        sub = df.iloc[s:e+1]
        sog = float(sub["VESSEL.SOG"].mean())
        foc = float(sub["PMS.FOC.ME"].mean())
        m = {
            "sog": sog,
            "foc_me": foc,
            "foc_tot": float(sub["PMS.FOC.TOT"].mean()),
            "me_pwr": float(sub["ME.PWR_cal"].mean()),
            "tws": float(sub["TWS"].mean()),
            "twa": float(sub["TWA"].mean()),
            "aws": float(sub["AN1.AWS"].mean()),
            "sfoc": float(sub["SFOC"].mean()),
            "rotor_pwr": float(sub["ROTOR_PWR_TOT"].mean()),
            "hdt": float(sub["VESSEL.HDT"].mean()),
            "roll": float(sub["VESSEL.RA1"].abs().mean()),
            # 恒转速测试下正确指标：单位距离油耗 kg/nm（含风帆白耗电折算可选）
            "foc_per_nm": foc / sog if sog > 1 else None,
            "foc_tot_per_nm": float(sub["PMS.FOC.TOT"].mean()) / sog if sog > 1 else None,
        }
        return {k: (round(v, 3) if v is not None else None) for k, v in m.items()}

    for seg in segs:
        # 找到对应行范围
        mask = (ut >= seg["t0"]) & (ut <= seg["t1"])
        idx = np.where(mask)[0]
        seg["metrics"] = seg_metrics(idx[0], idx[-1])

    # 配对：相邻 ON/OFF 段（前后各1小时内）
    pairs = []
    for i, seg in enumerate(segs):
        if not seg["on"]:
            continue
        # 找最近的 OFF 段
        best = None
        for j, other in enumerate(segs):
            if other["on"]:
                continue
            gap = min(abs(other["t0"] - seg["t1"]), abs(seg["t0"] - other["t1"]))
            if gap <= 7200 and (best is None or gap < best[1]):
                best = (j, gap)
        if best:
            off = segs[best[0]]
            on_m, off_m = seg["metrics"], off["metrics"]
            pair = {
                "on_seg": seg, "off_seg": off, "gap_s": best[1],
                "delta": {
                    "foc_me": round(on_m["foc_me"] - off_m["foc_me"], 2),
                    "sog": round(on_m["sog"] - off_m["sog"], 3),
                    "me_pwr": round(on_m["me_pwr"] - off_m["me_pwr"], 1),
                    "tws": round(on_m["tws"] - off_m["tws"], 2),
                    "foc_per_nm": round(on_m["foc_per_nm"] - off_m["foc_per_nm"], 3) if on_m["foc_per_nm"] and off_m["foc_per_nm"] else None,
                },
                "saving_pct": round((off_m["foc_me"] - on_m["foc_me"]) / off_m["foc_me"] * 100, 2) if off_m["foc_me"] else None,
                # 恒转速测试：单位距离油耗节约率（正=节能）
                "saving_per_nm_pct": round((off_m["foc_per_nm"] - on_m["foc_per_nm"]) / off_m["foc_per_nm"] * 100, 2) if off_m.get("foc_per_nm") and on_m.get("foc_per_nm") else None,
            }
            pairs.append(pair)

    return {"segments": segs, "pairs": pairs}


@app.get("/api/wind")
def api_wind():
    """风况：风玫瑰(真风) + AWA分布 + 传感器一致性"""
    df = get_df()
    # 真风玫瑰：TWD 16方位 × TWS 分箱
    twd = df["TWD"].to_numpy(dtype=float)
    tws = df["TWS"].to_numpy(dtype=float)
    valid = ~(np.isnan(twd) | np.isnan(tws))
    twd, tws = twd[valid], tws[valid]
    dirs = np.arange(0, 361, 22.5)
    speed_bins = [0, 3, 6, 9, 12, 15, 20, 100]
    speed_labels = ["0-3", "3-6", "6-9", "9-12", "12-15", "15-20", ">20"]
    rose = np.zeros((16, len(speed_labels)))
    for i in range(16):
        dmask = (twd >= dirs[i]) & (twd < dirs[i] + 22.5)
        for j in range(len(speed_labels)):
            smask = (tws >= speed_bins[j]) & (tws < speed_bins[j + 1])
            rose[i, j] = np.sum(dmask & smask)
    rose_list = [{"dir": dirs[i], "counts": rose[i].tolist()} for i in range(16)]

    # AWA 分布（AN1）
    awa = df["AN1.AWA"].dropna()
    awa_hist, awa_edges = np.histogram(awa, bins=36, range=(0, 360))

    # 传感器一致性：1min 均值
    t0 = int(df["unix time"].iloc[0])
    t1 = int(df["unix time"].iloc[-1])
    consistency = resample(df, ["AN1.AWS", "AN2.AWS", "MET.AWS", "AN1.AWA", "AN2.AWA", "MET.AWA"], t0, t1, 60)

    # TWA 直方图（真风来向相对艏向）
    twa = df["TWA"].dropna()
    twa_hist, twa_edges = np.histogram(twa, bins=36, range=(0, 360))

    return {
        "rose": rose_list,
        "speed_labels": speed_labels,
        "awa_hist": {"counts": awa_hist.tolist(), "edges": awa_edges.tolist()},
        "twa_hist": {"counts": twa_hist.tolist(), "edges": twa_edges.tolist()},
        "consistency": consistency,
        "tws_stats": {
            "mean": round(float(np.nanmean(tws)), 2),
            "max": round(float(np.nanmax(tws)), 2),
            "min": round(float(np.nanmin(tws)), 2),
        },
    }


@app.get("/api/rotor")
def api_rotor():
    """旋筒监控：SP-PV 跟踪 + 驱动功率 + 事件"""
    df = get_df()
    t0 = int(df["unix time"].iloc[0])
    t1 = int(df["unix time"].iloc[-1])
    cols = []
    for i in [1, 3, 4, 5]:  # ROT2 停转，跳过
        cols += [f"ROT{i}.SPD.SP", f"ROT{i}.SPD.PV", f"ROT{i}.DRV.PWR"]
    curves = resample(df, cols, t0, t1, 60)

    # SP-PV 跟踪误差（1Hz 全量统计）
    err_stats = {}
    for i in [1, 3, 4, 5]:
        sp = df[f"ROT{i}.SPD.SP"].to_numpy(dtype=float)
        pv = df[f"ROT{i}.SPD.PV"].to_numpy(dtype=float)
        active = np.abs(sp) > 5
        err = pv[active] - sp[active]
        err_stats[f"ROT{i}"] = {
            "active_frac": round(float(active.mean()), 3),
            "err_mean": round(float(np.nanmean(err)), 2),
            "err_std": round(float(np.nanstd(err)), 2),
            "err_max": round(float(np.nanmax(np.abs(err))), 2),
        }

    # 启停事件（ROTOR_N 变化）
    rn = df["ROTOR_N"].to_numpy()
    ut = df["unix time"].to_numpy()
    change = np.where(np.diff(rn) != 0)[0]
    events = []
    for c in change[:200]:
        events.append({"t": int(ut[c + 1]), "from": int(rn[c]), "to": int(rn[c + 1])})

    return {"curves": curves, "err_stats": err_stats, "events": events}


@app.get("/api/quality")
def api_quality():
    """数据质量报告"""
    df = get_df()
    issues = []
    for col, note in KNOWN_ISSUES.items():
        if col in df.columns:
            s = df[col]
            issues.append({
                "col": col, "note": note,
                "name": COLUMNS.get(col, {}).get("name", col),
                "zero_frac": round(float((s == 0).mean()), 4),
                "nan_frac": round(float(s.isna().mean()), 4),
                "min": None if pd.isna(s.min()) else round(float(s.min()), 3),
                "max": None if pd.isna(s.max()) else round(float(s.max()), 3),
                "mean": None if pd.isna(s.mean()) else round(float(s.mean()), 3),
            })
    # 全列统计
    stats = []
    for c in df.columns:
        if c in ("utc", "Time_UTC"):
            continue
        s = df[c]
        try:
            stats.append({
                "col": c,
                "name": COLUMNS.get(c, {}).get("name", c),
                "group": COLUMNS.get(c, {}).get("group", "derived"),
                "unit": COLUMNS.get(c, {}).get("unit", ""),
                "nan_frac": round(float(s.isna().mean()), 4),
                "min": None if pd.isna(s.min()) else round(float(s.min()), 3),
                "max": None if pd.isna(s.max()) else round(float(s.max()), 3),
                "mean": None if pd.isna(s.mean()) else round(float(s.mean()), 3),
            })
        except Exception:
            pass
    return {"issues": issues, "stats": stats}


@app.get("/api/segments")
def api_segments():
    """状态分段（供时序页背景带）"""
    df = get_df()
    ut = df["unix time"].to_numpy()
    rotor = df["ROTOR_ON"].to_numpy().astype(int)
    sysr = df["VESSEL.STAT0_SYS.RUNNING"].to_numpy().astype(int)
    state = np.where(sysr == 1, rotor, 2)
    change = np.where(np.diff(state) != 0)[0]
    starts = np.concatenate([[0], change + 1])
    ends = np.concatenate([change, [len(state) - 1]])
    segs = []
    for s, e in zip(starts, ends):
        if int(ut[e] - ut[s]) < 60:
            continue
        segs.append({"t0": int(ut[s]), "t1": int(ut[e]), "state": int(state[s])})
    return {"segments": segs}


# ---------- 通用数据查看器 ----------

CACHE_DIR = os.path.join(DATA_DIR, "cache")


def _read_table_bytes(buf: bytes, filename: str) -> "pd.DataFrame":
    """按扩展名读取表格：xlsx/xls 走 calamine，csv 自动喰探编码与分隔符"""
    import io
    ext = os.path.splitext(filename or "")[1].lower()
    if ext == ".csv":
        t0 = time.time()
        # 编码喰探：UTF-8（含 BOM）→ GBK → UTF-16 → latin-1 兕底
        encodings = ["utf-8-sig", "utf-8", "gbk", "utf-16", "latin-1"]
        last_err = None
        for enc in encodings:
            try:
                text_head = buf[:65536].decode(enc)
                # 分隔符检测：看首行哪个分隔符出现次数多
                first_line = text_head.split("\n")[0]
                seps = [",", "\t", ";", "|"]
                sep = max(seps, key=lambda s: first_line.count(s))
                if first_line.count(sep) == 0:
                    sep = ","  # 单列无分隔符
                df = pd.read_csv(io.BytesIO(buf), header=None, sep=sep, encoding=enc,
                                 engine="c", skip_blank_lines=True, on_bad_lines="skip")
                _log(f"  读取完成: {len(df)} 行 × {df.shape[1]} 列 (CSV {enc}, 分隔符{'\\t' if sep == chr(9) else sep})，耗时 {time.time()-t0:.1f}s")
                return df
            except (UnicodeDecodeError, UnicodeError):
                continue
            except pd.errors.ParserError:
                # 编码对了但解析出错（如坏行已 skip，这里多为分隔符问题），换下一编码意义不大，直接报
                raise HTTPException(400, f"CSV 解析失败，请检查分隔符/格式")
            except Exception as e:
                last_err = e
                continue
        raise HTTPException(400, f"无法识别 CSV 编码: {last_err}")
    # Excel：calamine 引擎（比 openpyxl 快约 8 倍）
    try:
        t0 = time.time()
        df = pd.read_excel(io.BytesIO(buf), sheet_name=0, header=None, engine="calamine")
        _log(f"  读取完成: {len(df)} 行 × {df.shape[1]} 列，耗时 {time.time()-t0:.1f}s")
        return df
    except Exception:
        # calamine 失败时回退 openpyxl（老 .xls 或特殊格式）
        try:
            t0 = time.time()
            df = pd.read_excel(io.BytesIO(buf), sheet_name=0, header=None)
            _log(f"  读取完成(openpyxl回退): {len(df)} 行 × {df.shape[1]} 列，耗时 {time.time()-t0:.1f}s")
            return df
        except Exception as e2:
            raise HTTPException(400, f"无法读取 Excel: {e2}")


def _parse_excel_bytes(buf: bytes, filename: str) -> dict:
    """解析上传的表格（Excel/CSV）：自动识别表头行、时间列，返回前端 JSON"""
    t_start = time.time()
    size_mb = len(buf) / 1024 / 1024
    _log(f"[上传] {filename} ({size_mb:.1f} MB)")

    # --- 1. 读取 ---
    df = _read_table_bytes(buf, filename)
    if df.empty or df.shape[0] < 2:
        raise HTTPException(400, "表格为空或行数不足")

    # --- 1. 识别表头行：前 5 行中非空单元格最多且类型为字符串的一行 ---
    header_row = 0
    best_score = -1
    for i in range(min(5, len(df))):
        row = df.iloc[i]
        n_str = sum(1 for v in row if isinstance(v, str) and v.strip())
        n_num = sum(1 for v in row if isinstance(v, (int, float)) and not pd.isna(v))
        score = n_str * 2 - n_num  # 字符串多、数字少 → 更像表头
        if score > best_score:
            best_score = score
            header_row = i
    headers = [str(v).strip() if v is not None and not pd.isna(v) else f"列{j+1}" for j, v in enumerate(df.iloc[header_row])]
    # 去重
    seen = {}
    for j, h in enumerate(headers):
        if h in seen:
            seen[h] += 1
            headers[j] = f"{h}_{seen[h]}"
        else:
            seen[h] = 0
    data = df.iloc[header_row + 1:].reset_index(drop=True)
    data.columns = headers

    # --- 2. 识别时间列：优先 datetime 列；若重复率高（精度不足）则找 unix 秒列 ---
    t_col = None
    t_vals = None
    n = len(data)
    # 候选1: datetime 类型列（前 6 列）
    for cand in headers[:6]:
        if cand not in data.columns:
            continue
        s = data[cand]
        if pd.api.types.is_datetime64_any_dtype(s):
            uniq = s.nunique()
            if uniq >= max(2, int(n * 0.5)) or n <= 10:
                t_col, t_vals = cand, s
                break
            # datetime 列重复率高 → 记住但继续找 unix 列
            if t_col is None:
                t_col, t_vals = cand, s
    # 候选2: unix 秒/毫秒列（数值、单调、范围合理）
    if t_vals is None or t_vals.nunique() < max(2, int(n * 0.5)):
        for cand in headers[:8]:
            if cand not in data.columns or cand == t_col:
                continue
            v = pd.to_numeric(data[cand], errors="coerce")
            if v.notna().mean() < 0.99:
                continue
            lo, hi = v.min(), v.max()
            # unix 秒 (2001~2100) 或毫秒 (2001~2100)
            is_sec = 1e9 < lo < 4.1e9 and 1e9 < hi < 4.1e9
            is_ms = 1e12 < lo < 4.1e12 and 1e12 < hi < 4.1e12
            if (is_sec or is_ms) and v.is_monotonic_increasing:
                t_vals = pd.to_datetime(v, unit="s" if is_sec else "ms", errors="coerce")
                t_col = cand
                break
    # 候选3: 可解析字符串时间（前 6 列）
    if t_col is None:
        for cand in headers[:6]:
            if cand not in data.columns:
                continue
            parsed = pd.to_datetime(data[cand], errors="coerce", format="mixed")
            if parsed.notna().mean() > 0.9:
                t_col, t_vals = cand, parsed
                break
    if t_col is None:
        raise HTTPException(400, "未找到可识别的时间列（需在前几列中，且 90% 以上可解析为时间）")

    # --- 3. 数值列提取（全量，不做抽样，忠于原始数据）---
    t0 = time.time()
    cols = {}
    for c in data.columns:
        if c == t_col:
            continue
        v = pd.to_numeric(data[c], errors="coerce")
        if v.notna().sum() == 0:
            continue  # 全非数值，跳过
        meta = COLUMNS.get(c, {})
        cols[c] = {
            "v": [None if pd.isna(x) else (int(x) if float(x).is_integer() and abs(x) < 1e15 else round(float(x), 6)) for x in v.tolist()],
            "unit": meta.get("unit", ""),
            "desc": meta.get("name", ""),
        }
    if not cols:
        raise HTTPException(400, "未找到数值数据列")
    _log(f"  列提取: {len(cols)} 个数值列（全量无抽样），耗时 {time.time()-t0:.1f}s")

    t_ms = (t_vals.astype("datetime64[ns]").astype("int64") // 10**6).tolist()  # → 毫秒
    result = {
        "file": filename,
        "rows": len(data),
        "tCol": t_col,
        "t": t_ms,
        "cols": cols,
    }
    _log(f"  解析完成: 时间列={t_col}, 共耗时 {time.time()-t_start:.1f}s")
    return result


@app.post("/api/upload")
async def api_upload(file: bytes = File(...)):
    """接收 Excel 文件字节流，自动解析返回"""
    # filename 从 header 里拿不到（octet-stream），前端已传文件名参数
    return _parse_excel_bytes(file, "uploaded.xlsx")


@app.get("/api/logs")
def api_logs(after: int = Query(-1)):
    """前台日志轮询：返回 after 之后的日志"""
    with _LOG_LOCK:
        items = list(LOG_BUF)
    if after < 0:
        return {"items": items, "next": len(items)}
    return {"items": items[after:], "next": len(items)}


# ---------- 会话恢复（网页重开时恢复上次数据表和状态） ----------
SESSION_FILE = os.path.join(DATA_DIR, "session.json")


def _save_session(hash_: str, data: dict):
    """记录最近一次加载的文件（缓存哈希 + 元信息）"""
    try:
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump({"hash": hash_, "file": data.get("file"), "rows": data.get("rows"), "time": datetime.datetime.now().isoformat(timespec="seconds")}, f, ensure_ascii=False)
    except Exception:
        pass


def _load_session():
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


@app.get("/api/session")
def api_session_get():
    """网页打开时查询：有无上次会话可恢复"""
    s = _load_session()
    if not s:
        return {"has": False}
    # 缓存文件还在才能恢复
    cache_fp = os.path.join(CACHE_DIR, s["hash"] + ".json")
    if not os.path.exists(cache_fp):
        return {"has": False}
    return {"has": True, **s}


@app.get("/api/session/restore")
def api_session_restore():
    """恢复上次会话：返回完整数据 + UI 状态"""
    s = _load_session()
    if not s:
        raise HTTPException(404, "无可恢复会话")
    cache_fp = os.path.join(CACHE_DIR, s["hash"] + ".json")
    if not os.path.exists(cache_fp):
        raise HTTPException(404, "缓存已不存在")
    with open(cache_fp, encoding="utf-8") as f:
        data = json.load(f)
    _log(f"[会话] 恢复上次数据: {s['file']}")
    return data


@app.post("/api/upload2")
async def api_upload2(file: UploadFile = File(...)):
    """multipart 方式上传（带文件名）：解析并写入缓存"""
    import hashlib
    t0 = time.time()
    buf = await file.read()
    _log(f"[上传] 接收完成: {file.filename}, {len(buf)/1024/1024:.1f} MB, 传输 {time.time()-t0:.1f}s")
    result = _parse_excel_bytes(buf, file.filename or "uploaded.xlsx")
    # 写缓存（以 sha256 为键）+ 记录会话
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        h = hashlib.sha256(buf).hexdigest()
        with open(os.path.join(CACHE_DIR, h + ".json"), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        _log(f"  缓存已写入: {h[:16]}...")
        _save_session(h, result)
    except Exception as e:
        _log(f"  缓存写入失败(不影响使用): {e}")
    return result


@app.get("/api/cache/{hash}")
def api_cache_get(hash: str):
    """查缓存：命中则直接返回解析结果"""
    import re
    if not re.fullmatch(r"[0-9a-f]{64}", hash):
        raise HTTPException(400, "无效的哈希")
    fp = os.path.join(CACHE_DIR, hash + ".json")
    if not os.path.exists(fp):
        raise HTTPException(404, "缓存未命中")
    _log(f"[缓存] 命中 {hash[:16]}...，直接返回")
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


@app.get("/api/cache")
def api_cache_list():
    """列出所有缓存"""
    if not os.path.isdir(CACHE_DIR):
        return {"items": []}
    items = []
    for fn in os.listdir(CACHE_DIR):
        if fn.endswith(".json"):
            fp = os.path.join(CACHE_DIR, fn)
            try:
                with open(fp, encoding="utf-8") as f:
                    d = json.load(f)
                items.append({
                    "hash": fn[:-5],
                    "file": d.get("file"),
                    "rows": d.get("rows"),
                    "cols": len(d.get("cols", {})),
                    "size_kb": round(os.path.getsize(fp) / 1024),
                    "mtime": datetime.datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M"),
                })
            except Exception:
                pass
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"items": items}


@app.delete("/api/cache/{hash}")
def api_cache_del(hash: str):
    """删除指定缓存"""
    import re
    if not re.fullmatch(r"[0-9a-f]{64}", hash):
        raise HTTPException(400, "无效的哈希")
    fp = os.path.join(CACHE_DIR, hash + ".json")
    if os.path.exists(fp):
        os.remove(fp)
        return {"ok": True}
    raise HTTPException(404, "缓存不存在")


# ---------- UI 状态持久化（列名/选中列/缩放） ----------
UISTATE_DIR = os.path.join(DATA_DIR, "uistate")


def _uistate_path(hash: str) -> str:
    return os.path.join(UISTATE_DIR, hash + ".json")


class UIState(BaseModel):
    file: str = ""
    colNames: dict = {}
    selected: list = []
    zoom: list | None = None


@app.get("/api/uistate/{hash}")
def api_uistate_get(hash: str):
    import re
    if not re.fullmatch(r"[0-9a-f]{64}", hash):
        raise HTTPException(400, "无效的哈希")
    fp = _uistate_path(hash)
    if not os.path.exists(fp):
        raise HTTPException(404, "无保存状态")
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


@app.post("/api/uistate/{hash}")
async def api_uistate_save(hash: str, request: Request, beacon: int = 0):
    import re
    if not re.fullmatch(r"[0-9a-f]{64}", hash):
        raise HTTPException(400, "无效的哈希")
    # 兼容 sendBeacon（text/plain）与 fetch（application/json）两种 Content-Type
    try:
        body = await request.body()
        st = UIState(**json.loads(body))
    except Exception as e:
        raise HTTPException(400, f"无效的状态数据: {e}")
    os.makedirs(UISTATE_DIR, exist_ok=True)
    with open(_uistate_path(hash), "w", encoding="utf-8") as f:
        json.dump(st.model_dump(), f, ensure_ascii=False)
    return {"ok": True}


@app.get("/advanced")
def advanced(page: str = ""):
    """高级分析页（旧版 7 页面）"""
    fp = os.path.join(BASE_DIR, "static", "advanced.html")
    with open(fp, encoding="utf-8") as f:
        html = f.read()
    if page:
        inject = "<script>window.__PRESET_PAGE__=" + json.dumps(page) + ";</script>"
        marker = '<script src="/static/js/advanced.js"></script>'
        html = html.replace(marker, inject + marker)
    return HTMLResponse(html)


# ---------- 静态 ----------
@app.get("/")
def index():
    """数据查看器（新版简洁界面）"""
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


if __name__ == "__main__":
    import uvicorn
    print("启动: http://localhost:8300")
    print("关闭本窗口即停止服务")
    uvicorn.run(app, host="127.0.0.1", port=8300, log_level="warning")
