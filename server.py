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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

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




# ---------- 通用数据查看器 ----------

CACHE_DIR = os.path.join(DATA_DIR, "cache")
COLMETA_FILE = os.path.join(DATA_DIR, "colmeta.json")   # 列含义缓存（用户可编辑）


def _load_colmeta() -> dict:
    """列含义缓存: {列名: {unit, desc}}。文件不存在返回空（首次使用为空，可手动编辑补充）"""
    if os.path.exists(COLMETA_FILE):
        try:
            with open(COLMETA_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


@app.get("/api/colmeta")
def api_colmeta_get():
    """读取列含义缓存"""
    return _load_colmeta()


@app.post("/api/colmeta")
async def api_colmeta_save(request: Request):
    """保存列含义缓存（整体覆盖）"""
    try:
        body = await request.body()
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("需为 {列名: {unit, desc}} 结构")
        with open(COLMETA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        return {"ok": True, "count": len(data)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"无效数据: {e}")


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
    colmeta = _load_colmeta()
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
        cols[c] = {
            "v": [None if pd.isna(x) else (int(x) if float(x).is_integer() and abs(x) < 1e15 else round(float(x), 6)) for x in v.tolist()],
            "unit": colmeta.get(c, {}).get("unit", ""),
            "desc": colmeta.get(c, {}).get("desc", ""),
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
