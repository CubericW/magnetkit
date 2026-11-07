#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""magnet —— MagnetKit 的后端：命令行、后台服务，以及图形界面用的本地 API。

下载引擎是开源的 rqbit（https://github.com/ikatson/rqbit），本脚本通过它的 HTTP API 实现：
  * 解析磁力链接 / 种子并列出文件；任何下载开始前都必须经过用户确认
  * 边下边播（IINA / mpv / VLC）、任务管理
  * 后台监控：完成通知、做种策略、清理未选文件的残缺文件、后台解析（解析好后等待确认）、更新 tracker
  * 给 MagnetKit.app 提供本地 API（只监听 127.0.0.1，需要令牌）

只依赖 Python 3.9+ 标准库。
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import fnmatch
import hashlib
import http.client
import http.server
import json
import os
import plistlib
import re
import secrets
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

VERSION = "2.0.0"
HOME = Path.home()
APP_DIR = Path(__file__).resolve().parent
UI_DIR = APP_DIR / "ui"

# MAGNETKIT_HOME 用于测试 / 多实例：把配置、状态、日志都放进这个目录
_ROOT = os.environ.get("MAGNETKIT_HOME")
if _ROOT:
    CONFIG_PATH = Path(_ROOT) / "config.json"
    STATE_DIR = Path(_ROOT) / "state"
    LOG_DIR = Path(_ROOT) / "logs"
else:
    CONFIG_PATH = HOME / ".config" / "magnetkit" / "config.json"
    STATE_DIR = HOME / "Library" / "Application Support" / "MagnetKit"
    LOG_DIR = HOME / "Library" / "Logs" / "MagnetKit"
TORRENT_CACHE = STATE_DIR / "torrents"      # 解析过的元数据缓存（<infohash>.torrent）
STREAM_MARKS = STATE_DIR / "streams"        # 正在边下边播的任务标记，做种策略会避开它们
TRACKERS_FILE = STATE_DIR / "trackers.txt"
QUEUE_FILE = STATE_DIR / "queue.json"       # 解析任务（解析中 / 待确认 / 失败）
WATCH_STATE_FILE = STATE_DIR / "watch-state.json"
UI_INFO_FILE = STATE_DIR / "ui.json"        # 图形界面 API 的端口和令牌（权限 600）
UPNP_LOG = LOG_DIR / "rqbit-upnp.log"       # 开启 UPnP 时 rqbit 的端口映射日志

LAUNCH_AGENTS = HOME / "Library" / "LaunchAgents"
LABEL_RQBIT = "com.magnetkit.rqbit"
LABEL_WATCHER = "com.magnetkit.watcher"
LABELS = (LABEL_RQBIT, LABEL_WATCHER)
APP_BUNDLE = HOME / "Applications" / "MagnetKit.app"
APP_ID = "com.magnetkit.handler"
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
              "LaunchServices.framework/Support/lsregister")
USER_AGENT = f"MagnetKit/{VERSION} (+https://github.com/ikatson/rqbit)"
JOB_TTL = 7 * 86400                          # 待确认 / 失败的解析任务保留 7 天

DEFAULT_CONFIG: Dict[str, Any] = {
    "download_dir": "~/Magnet",
    "api_host": "127.0.0.1",
    "api_port": 3030,
    "ui_port": 3031,
    "listen_port": 4240,
    "username": "magnet",
    "password": "",
    "metadata_timeout": 60,
    "public_trackers": True,
    "max_extra_trackers": 100,
    "tracker_urls": [
        "https://cf.trackerslist.com/best.txt",
        "https://cdn.jsdelivr.net/gh/XIU2/TrackersListCollection/best.txt",
        "https://raw.githubusercontent.com/XIU2/TrackersListCollection/master/best.txt",
        "https://cdn.jsdelivr.net/gh/ngosang/trackerslist@master/trackers_best.txt",
    ],
    "seed_ratio": 1.0,
    "seed_minutes": 60,
    "after_seeding": "remove",
    "notify": True,
    "player": "auto",
    "upnp": False,
    "utp": False,
    "download_limit": "",
    "upload_limit": "",
    "queue_retry_hours": 24,
    "rqbit_path": "",
    "rqbit_log_level": "warn",
    "extra_rqbit_args": [],
}

CONFIG_HELP = {
    "download_dir": "下载目录（改后需重启服务）",
    "seed_ratio": "下载完成后做种到的分享率（0 = 不按分享率）",
    "seed_minutes": "下载完成后最多做种几分钟（-1 = 不限时间）",
    "after_seeding": "做种结束后：remove（移出列表并清理）/ pause / keep（一直做种）",
    "notify": "是否发送系统通知",
    "player": "播放器：auto / iina / mpv / vlc / quicktime",
    "download_limit": "下载限速，如 5M（每秒），留空不限",
    "upload_limit": "上传限速，如 1M（每秒），留空不限",
    "public_trackers": "给磁力链接追加公共 tracker（不影响私有种子）",
    "upnp": "是否用 UPnP 在路由器上开放端口（默认关闭）",
    "utp": "是否启用 uTP（rqbit 实验功能）",
    "queue_retry_hours": "后台解析最多尝试多少小时",
    "metadata_timeout": "命令行等待元数据的秒数",
    "max_extra_trackers": "最多追加多少个公共 tracker",
    "api_port": "rqbit HTTP API 端口（仅监听本机）",
    "ui_port": "图形界面本地 API 端口（仅监听本机）",
    "listen_port": "BT 监听端口",
}
RESTART_KEYS = {"download_dir", "api_host", "api_port", "ui_port", "listen_port", "username", "password", "upnp",
                "utp", "download_limit", "upload_limit", "rqbit_path", "rqbit_log_level", "extra_rqbit_args"}
# 图形界面「设置」页可以改的项，以及它们的类型
UI_FIELDS: Dict[str, Any] = {
    "download_dir": "path",
    "seed_ratio": "float",
    "seed_minutes": "int",
    "after_seeding": ("remove", "pause", "keep"),
    "notify": "bool",
    "player": ("auto", "iina", "mpv", "vlc", "quicktime"),
    "download_limit": "size",
    "upload_limit": "size",
    "public_trackers": "bool",
    "upnp": "bool",
    "utp": "bool",
    "queue_retry_hours": "float",
}


# ───────────────────────────── 通用工具 ─────────────────────────────

class MagnetError(Exception):
    """面向用户的错误（直接显示 message）。"""


class ServiceDown(MagnetError):
    pass


class ApiError(MagnetError):
    pass


class ConnectionLost(ApiError):
    """请求进行到一半连接断了（通常是下载服务重启）。"""


class RequestTimeout(MagnetError):
    pass


class MetaTimeout(MagnetError):
    pass


_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _COLOR else s


def bold(s: str) -> str: return _c("1", s)
def dim(s: str) -> str: return _c("2", s)
def red(s: str) -> str: return _c("31", s)
def green(s: str) -> str: return _c("32", s)
def yellow(s: str) -> str: return _c("33", s)
def cyan(s: str) -> str: return _c("36", s)


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def hsize(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def parse_size(s: str) -> int:
    """'50M' / '1.5G' / '500k' / '2GB' → 字节；纯数字按 MB 理解。"""
    m = re.fullmatch(r"\s*([\d.]+)\s*([kmgt]?)i?b?\s*", str(s).lower())
    if not m:
        raise MagnetError(f"看不懂的大小：{s}（例：500K、50M、1.5G）")
    num, unit = float(m.group(1)), m.group(2) or "m"
    return int(num * 1024 ** "bkmgt".index(unit))


def hduration(secs: Optional[float]) -> str:
    if secs is None:
        return "-"
    secs = int(secs)
    if secs < 60:
        return f"{secs}秒"
    if secs < 3600:
        return f"{secs // 60}分{secs % 60:02d}秒"
    if secs < 86400:
        return f"{secs // 3600}小时{secs % 3600 // 60:02d}分"
    return f"{secs // 86400}天{secs % 86400 // 3600}小时"


def hago(ts: float) -> str:
    d = time.time() - ts
    if d < 60:
        return "刚刚"
    if d < 3600:
        return f"{int(d // 60)} 分钟前"
    if d < 86400:
        return f"{int(d // 3600)} 小时前"
    return f"{int(d // 86400)} 天前"


def tilde(p: Any) -> str:
    s = str(p)
    h = str(HOME)
    return "~" + s[len(h):] if s == h or s.startswith(h + "/") else s


def dwidth(s: str) -> int:
    w = 0
    for ch in s:
        if unicodedata.combining(ch) or ch in "\ufe0f\u200d":
            continue
        w += 2 if unicodedata.east_asian_width(ch) in "WF" else 1
    return w


def wpad(s: str, width: int) -> str:
    return s + " " * max(0, width - dwidth(s))


def wrpad(s: str, width: int) -> str:
    return " " * max(0, width - dwidth(s)) + s


def clean(s: str) -> str:
    """去掉控制字符；把用来伪装扩展名的 RLO 字符显示出来。"""
    out = []
    for ch in s:
        if ch == "\u202e":
            out.append("⟲")
        elif unicodedata.category(ch) in ("Cc", "Cf"):
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def shorten(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n // 2 - 1] + "…" + s[-(n - n // 2):]


def bar(frac: float, width: int = 12) -> str:
    frac = max(0.0, min(1.0, frac))
    full = int(round(frac * width))
    return "█" * full + "░" * (width - full)


def write_atomic(path: Path, data: Any, mode: Optional[int] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def dedupe(items: List[str]) -> List[str]:
    seen, out = set(), []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def http_get(url: str, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(50 * 1024 * 1024)
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        # 系统 Python 偶尔缺证书 / 被 CDN 拦截时，退回 curl（走系统信任链）
        curl = subprocess.run(["curl", "-fsSL", "--max-time", str(int(timeout)), "-A", USER_AGENT, url],
                              capture_output=True)
        if curl.returncode == 0 and curl.stdout:
            return curl.stdout
        raise MagnetError(f"下载失败：{url}（{e}）")


_log_file: Optional[Path] = None


def setup_log(name: str, max_bytes: int = 5 * 1024 * 1024) -> None:
    global _log_file
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _log_file = LOG_DIR / name
    with contextlib.suppress(OSError):
        if _log_file.stat().st_size > max_bytes:
            _log_file.replace(_log_file.with_name(name + ".1"))


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    if _log_file is None:
        eprint(line)
        return
    with contextlib.suppress(OSError):
        with open(_log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ───────────────────────────── 配置 ─────────────────────────────

def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text("utf-8")))
        except ValueError as e:
            raise MagnetError(f"配置文件格式错误：{CONFIG_PATH}（{e}）")
    cfg["download_dir"] = os.path.abspath(os.path.expanduser(str(cfg["download_dir"])))
    return cfg


def read_raw_config() -> Dict[str, Any]:
    return read_json(CONFIG_PATH, {}) if CONFIG_PATH.exists() else {}


def save_raw_config(raw: Dict[str, Any]) -> None:
    write_atomic(CONFIG_PATH, json.dumps(raw, ensure_ascii=False, indent=2) + "\n", mode=0o600)


def coerce_config(key: str, value: Any) -> Any:
    """校验并规范化图形界面提交的配置值。"""
    kind = UI_FIELDS.get(key)
    if kind is None:
        raise MagnetError(f"不能修改这一项：{key}")
    if isinstance(kind, tuple):
        if value not in kind:
            raise MagnetError(f"{key} 只能是：{' / '.join(kind)}")
        return value
    if kind == "bool":
        if not isinstance(value, bool):
            raise MagnetError(f"{key} 应该是开 / 关")
        return value
    if kind in ("int", "float"):
        try:
            num = int(value) if kind == "int" else float(value)
        except (TypeError, ValueError):
            raise MagnetError(f"{key} 需要填数字")
        if key == "seed_minutes" and num < -1:
            raise MagnetError("做种时间最小是 -1（不限时间）")
        if key == "seed_ratio" and num < 0:
            raise MagnetError("分享率不能是负数")
        if key == "queue_retry_hours" and num <= 0:
            raise MagnetError("后台解析时长要大于 0")
        return num
    if kind == "size":
        s = str(value or "").strip()
        if s:
            parse_size(s)
        return s
    if kind == "path":
        p = os.path.expanduser(str(value or "").strip())
        if not p or not os.path.isabs(p):
            raise MagnetError("下载目录要写完整路径，例如 ~/Magnet")
        return tilde(os.path.abspath(p))
    raise MagnetError(f"不支持的配置类型：{key}")


# ───────────────────────────── bencode / 种子解析 ─────────────────────────────

def _bdecode(data: bytes, i: int) -> Tuple[Any, int]:
    c = data[i:i + 1]
    if c == b"i":
        j = data.index(b"e", i)
        return int(data[i + 1:j]), j + 1
    if c == b"l":
        i += 1
        out = []
        while data[i:i + 1] != b"e":
            v, i = _bdecode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        d = {}
        while data[i:i + 1] != b"e":
            k, i = _bdecode(data, i)
            v, i = _bdecode(data, i)
            d[k] = v
        return d, i + 1
    if c.isdigit():
        j = data.index(b":", i)
        n = int(data[i:j])
        j += 1
        if j + n > len(data):
            raise ValueError("字符串越界")
        return data[j:j + n], j + n
    raise ValueError(f"位置 {i} 不是合法的 bencode")


def bdecode_torrent(data: bytes) -> Tuple[Dict[bytes, Any], Tuple[int, int]]:
    """解析种子顶层字典，同时记下 info 字段的原始字节范围（info hash 就是它的 SHA-1）。"""
    if data[:1] != b"d":
        raise ValueError("不是 bencode 字典")
    i, top, span = 1, {}, None
    while data[i:i + 1] != b"e":
        k, i = _bdecode(data, i)
        start = i
        v, i = _bdecode(data, i)
        top[k] = v
        if k == b"info":
            span = (start, i)
    if span is None or not isinstance(top.get(b"info"), dict):
        raise ValueError("缺少 info 字段")
    return top, span


def bencode(v: Any) -> bytes:
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, str):
        v = v.encode("utf-8")
    if isinstance(v, (bytes, bytearray)):
        return b"%d:%s" % (len(v), bytes(v))
    if isinstance(v, (list, tuple)):
        return b"l" + b"".join(bencode(x) for x in v) + b"e"
    if isinstance(v, dict):
        items = sorted((k.encode("utf-8") if isinstance(k, str) else k, val) for k, val in v.items())
        return b"d" + b"".join(bencode(k) + bencode(val) for k, val in items) + b"e"
    raise TypeError(f"无法 bencode：{type(v)}")


def _text(b: Any, enc: Optional[str] = None) -> str:
    """种子里的字符串多数是 UTF-8；老的中文种子常是 GBK，这里按 声明编码 → UTF-8 → GB18030 依次尝试。"""
    if isinstance(b, str):
        return b
    if not isinstance(b, (bytes, bytearray)):
        return str(b)
    for e in (enc, "utf-8", "gb18030"):
        if not e:
            continue
        try:
            return bytes(b).decode(e)
        except (UnicodeDecodeError, LookupError):
            pass
    return bytes(b).decode("utf-8", "replace")


CATEGORIES = [
    # (键, 图标, 中文名, 扩展名)
    ("video", "🎬", "视频", "mp4 mkv avi mov wmv flv webm m4v ts m2ts mts rmvb rm mpg mpeg 3gp vob ogv f4v divx"),
    ("audio", "🎵", "音频", "mp3 flac aac m4a wav ogg opus ape wma alac dsf dff aiff aif ac3 dts mka"),
    ("sub", "💬", "字幕", "srt ass ssa sub idx vtt sup smi lrc"),
    ("image", "🖼", "图片", "jpg jpeg png gif bmp webp heic tif tiff"),
    ("archive", "🗜", "压缩包/镜像", "zip rar 7z tar gz tgz bz2 xz zst iso dmg img"),
    ("doc", "📄", "文档", "pdf epub mobi azw3 txt nfo md doc docx xls xlsx ppt pptx rtf chm djvu cbz cbr"),
    ("exe", "⚠️", "程序/快捷方式",
     "exe msi bat cmd com scr pif vbs vbe jse wsf wsh ps1 lnk url hta cpl reg jar apk pkg command"),
]
EXT_MAP = {ext: key for key, _, _, exts in CATEGORIES for ext in exts.split()}
CAT_ICON = {key: icon for key, icon, _, _ in CATEGORIES}
CAT_NAME = {key: name for key, _, name, _ in CATEGORIES}
CAT_NAME["other"] = "其他"
CAT_ALIASES = {
    "video": "video", "videos": "video", "视频": "video",
    "audio": "audio", "music": "audio", "音频": "audio", "音乐": "audio",
    "sub": "sub", "subs": "sub", "subtitle": "sub", "subtitles": "sub", "字幕": "sub",
    "image": "image", "images": "image", "pic": "image", "图片": "image",
    "archive": "archive", "压缩包": "archive",
    "doc": "doc", "docs": "doc", "文档": "doc",
    "exe": "exe", "程序": "exe",
}


class TFile:
    __slots__ = ("idx", "path", "length", "padding")

    def __init__(self, idx: int, path: str, length: int, padding: bool = False):
        self.idx, self.path, self.length, self.padding = idx, path, int(length), padding

    @property
    def num(self) -> int:          # 给人看的编号（从 1 开始）
        return self.idx + 1

    @property
    def base(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def ext(self) -> str:
        return self.base.rsplit(".", 1)[-1].lower() if "." in self.base else ""

    @property
    def category(self) -> str:
        return EXT_MAP.get(self.ext, "other")

    @property
    def suspicious(self) -> bool:
        return self.category == "exe" or "\u202e" in self.path

    @property
    def icon(self) -> str:
        return "⚠️" if self.suspicious else CAT_ICON.get(self.category, "📎")


class TorrentMeta:
    def __init__(self, raw: bytes, origin: str = "file", magnet_trackers: Optional[List[str]] = None):
        try:
            top, span = bdecode_torrent(raw)
        except (ValueError, IndexError) as e:
            raise MagnetError(f"不是有效的种子文件（{e}）")
        info = top[b"info"]
        enc = _text(top.get(b"encoding") or b"") or None
        self.raw, self.info_span, self.origin = raw, span, origin
        self.magnet_trackers = list(magnet_trackers or [])
        self.info_hash = hashlib.sha1(raw[span[0]:span[1]]).hexdigest()
        self.name = _text(info.get(b"name.utf-8") or info.get(b"name") or b"", enc) or self.info_hash
        self.private = info.get(b"private") == 1
        self.piece_length = int(info.get(b"piece length") or 0)
        self.comment = _text(top.get(b"comment.utf-8") or top.get(b"comment") or b"", enc)
        trackers: List[str] = []
        if top.get(b"announce"):
            trackers.append(_text(top[b"announce"]))
        for tier in top.get(b"announce-list") or []:
            if isinstance(tier, list):
                trackers.extend(_text(t) for t in tier)
        self.trackers = dedupe(trackers)
        files: List[TFile] = []
        if isinstance(info.get(b"files"), list):
            for i, f in enumerate(info[b"files"]):
                parts = [_text(c, enc) for c in (f.get(b"path.utf-8") or f.get(b"path") or [])]
                path = "/".join(p for p in parts if p) or f"file{i}"
                attr = f.get(b"attr") or b""
                pad = (isinstance(attr, bytes) and b"p" in attr) or path.startswith(".pad/") \
                    or "_____padding_file_" in path
                files.append(TFile(i, path, int(f.get(b"length") or 0), pad))
        elif b"length" in info:
            files.append(TFile(0, self.name, int(info[b"length"])))
        else:
            raise MagnetError("这是纯 BitTorrent v2 种子，rqbit 目前只支持 v1 / 混合种子")
        self.files = files

    @property
    def real_files(self) -> List[TFile]:
        return [f for f in self.files if not f.padding]

    @property
    def total(self) -> int:
        return sum(f.length for f in self.real_files)


def torrent_with_trackers(meta: TorrentMeta, trackers: List[str]) -> bytes:
    """保留 info 原始字节（info hash 不变），换上新的 tracker 列表。键按字典序：announce < announce-list < info。"""
    s, e = meta.info_span
    out = b"d"
    if trackers:
        out += bencode("announce") + bencode(trackers[0])
        out += bencode("announce-list") + bencode([[t] for t in trackers])
    return out + bencode("info") + meta.raw[s:e] + b"e"


# ───────────────────────────── 文件选择 ─────────────────────────────

def recommended(files: List[TFile]) -> Set[int]:
    return {f.idx for f in files if not f.padding and not f.suspicious}


def select_files(spec: str, files: List[TFile]) -> Set[int]:
    """选择语法：1,3-5 编号 | video 类别 | *.mkv 通配 | 关键字 | !xxx 排除 | rec 推荐 | all 全部 | largest 最大。"""
    real = [f for f in files if not f.padding]
    by_num = {f.num: f for f in real}
    include: Set[int] = set()
    exclude: Set[int] = set()
    has_include = False
    for tok in re.split(r"[,\s，、;；]+", (spec or "").strip()):
        if not tok:
            continue
        neg = tok[0] in "!！^"
        if neg:
            tok = tok[1:]
            if not tok:
                continue
        low = tok.lower()
        if low in ("all", "全部", "*"):
            hit = {f.idx for f in real}
        elif low in ("rec", "推荐", "default", "默认"):
            hit = recommended(real)
        elif low in ("largest", "最大", "biggest"):
            hit = {max(real, key=lambda f: f.length).idx} if real else set()
        elif low in CAT_ALIASES:
            hit = {f.idx for f in real if f.category == CAT_ALIASES[low]}
        elif re.fullmatch(r"\d+(-\d+)?", tok):
            a, _, b = tok.partition("-")
            lo, hi = sorted((int(a), int(b or a)))
            hit = {by_num[n].idx for n in range(lo, hi + 1) if n in by_num}
            if not hit:
                raise MagnetError(f"没有编号为 {tok} 的文件")
        else:
            pat = low if any(ch in tok for ch in "*?[") else f"*{low}*"
            hit = {f.idx for f in real if fnmatch.fnmatchcase(f.path.lower(), pat)}
        if neg:
            exclude |= hit
        else:
            include |= hit
            has_include = True
    if not has_include:
        include = {f.idx for f in real}
    return include - exclude


# ───────────────────────────── 输入源 ─────────────────────────────

_HASH_RE = re.compile(r"[0-9a-fA-F]{40}|[A-Za-z2-7]{32}")


def _hash_hex(h: str) -> str:
    return h.lower() if len(h) == 40 else base64.b32decode(h.upper()).hex()


class Source:
    def __init__(self, kind: str, value: str, info_hash: Optional[str] = None,
                 name: Optional[str] = None, trackers: Optional[List[str]] = None):
        self.kind, self.value, self.info_hash = kind, value, info_hash
        self.name, self.trackers = name, list(trackers or [])

    @property
    def label(self) -> str:
        return self.name or (self.info_hash[:16] if self.info_hash else Path(self.value).name)


def looks_like_source(s: str) -> bool:
    s = s.strip().strip("'\"")
    low = s.lower()
    return (low.startswith(("magnet:", "http://", "https://", "file://")) or bool(_HASH_RE.fullmatch(s))
            or low.endswith(".torrent"))


def parse_source(s: str) -> Source:
    s = s.strip().strip("'\"").replace("&amp;", "&")
    if not s:
        raise MagnetError("缺少磁力链接或种子文件")
    low = s.lower()
    if _HASH_RE.fullmatch(s):
        s, low = "magnet:?xt=urn:btih:" + s, "magnet:"
    if low.startswith("magnet:"):
        q = urllib.parse.parse_qs(s.split("?", 1)[1] if "?" in s else "")
        ih = None
        for xt in q.get("xt", []):
            m = re.fullmatch(r"urn:btih:(" + _HASH_RE.pattern + ")", xt.strip(), re.I)
            if m:
                ih = _hash_hex(m.group(1))
                break
        if not ih:
            raise MagnetError("这个磁力链接里没有 BitTorrent v1 的 info hash（xt=urn:btih:…）")
        return Source("magnet", s, ih, (q.get("dn") or [None])[0], q.get("tr", []))
    if low.startswith(("http://", "https://")):
        return Source("url", s, name=urllib.parse.unquote(s.rsplit("/", 1)[-1]).removesuffix(".torrent"))
    if low.startswith("file://"):
        s = urllib.parse.unquote(urllib.parse.urlsplit(s).path)
    p = Path(os.path.expanduser(s))
    if p.is_file():
        return Source("file", str(p.resolve()), name=p.stem)
    raise MagnetError(f"认不出来：{shorten(s, 80)}\n需要磁力链接、40 位 info hash、.torrent 文件或它的网址")


# ───────────────────────────── rqbit API ─────────────────────────────

class Api:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.host, self.port = cfg["api_host"], int(cfg["api_port"])
        self.base = f"http://{self.host}:{self.port}"
        token = base64.b64encode(f"{cfg['username']}:{cfg['password']}".encode()).decode()
        self.headers = {"Authorization": "Basic " + token}

    def call(self, method: str, path: str, body: Any = None, timeout: float = 15,
             ctype: Optional[str] = None, raw: bool = False) -> Any:
        headers = dict(self.headers)
        data = body
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif isinstance(body, str):
            data = body.encode("utf-8")
            headers["Content-Type"] = ctype or "text/plain"
        elif isinstance(body, bytes):
            headers["Content-Type"] = ctype or "application/octet-stream"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = r.read()
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if e.code == 401:
                raise ApiError("rqbit 拒绝访问（401）：配置里的密码和运行中的服务不一致，试试 magnet service restart")
            try:
                msg = json.loads(text).get("human_readable") or text
            except ValueError:
                msg = text
            raise ApiError(f"rqbit 返回错误 {e.code}：{msg}".strip())
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise RequestTimeout("请求超时")
            if isinstance(reason, ConnectionRefusedError) or "refused" in str(reason).lower():
                raise ServiceDown("rqbit 下载服务没有运行")
            raise ApiError(f"连不上 rqbit：{reason}")
        except (socket.timeout, TimeoutError):
            raise RequestTimeout("请求超时")
        except (ConnectionError, http.client.HTTPException) as e:
            raise ConnectionLost(f"和 rqbit 的连接中断：{e}")
        if raw:
            return payload
        return json.loads(payload) if payload else None

    def ping(self, timeout: float = 3) -> Dict[str, Any]:
        return self.call("GET", "/", timeout=timeout)

    def torrents(self, stats: bool = False) -> List[Dict[str, Any]]:
        return self.call("GET", "/torrents" + ("?with_stats=true" if stats else ""))["torrents"]

    def details(self, ref: Any) -> Dict[str, Any]:
        return self.call("GET", f"/torrents/{ref}")

    def stats(self, ref: Any) -> Dict[str, Any]:
        return self.call("GET", f"/torrents/{ref}/stats/v1")

    def resolve_magnet(self, uri: str, timeout: float) -> bytes:
        return self.call("POST", "/torrents/resolve_magnet", uri, timeout=timeout, raw=True)

    def add(self, body: bytes, params: Dict[str, str]) -> Dict[str, Any]:
        return self.call("POST", "/torrents?" + urllib.parse.urlencode(params), body,
                         ctype="application/x-bittorrent", timeout=120)

    def action(self, ref: Any, name: str) -> None:
        self.call("POST", f"/torrents/{ref}/{name}", b"", timeout=60)

    def set_files(self, ref: Any, idxs: Set[int]) -> None:
        self.call("POST", f"/torrents/{ref}/update_only_files", {"only_files": sorted(idxs)}, timeout=60)

    def stream_url(self, info_hash: str, idx: int, filename: str) -> str:
        user = urllib.parse.quote(self.cfg["username"], safe="")
        pw = urllib.parse.quote(self.cfg["password"], safe="")
        return (f"http://{user}:{pw}@{self.host}:{self.port}/torrents/{info_hash}/stream/{idx}/"
                f"{urllib.parse.quote(filename)}")


def find_by_hash(api: Api, info_hash: Optional[str]) -> Optional[Dict[str, Any]]:
    if not info_hash:
        return None
    for t in api.torrents():
        if t["info_hash"] == info_hash:
            return t
    return None


def find_task(api: Api, ref: Optional[str]) -> Dict[str, Any]:
    items = api.torrents()
    if not items:
        raise MagnetError("当前没有任务")
    r = (ref or "").strip()
    if r in ("", "last", "latest", "最新"):
        return max(items, key=lambda t: t["id"])
    if r.isdigit():
        for t in items:
            if t["id"] == int(r):
                return t
    low = r.lower()
    if re.fullmatch(r"[0-9a-f]{6,40}", low):
        hits = [t for t in items if t["info_hash"].startswith(low)]
        if len(hits) == 1:
            return hits[0]
    hits = [t for t in items if low in t["name"].lower()]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise MagnetError("匹配到多个任务，请用 ID：\n" + "\n".join(f"  {t['id']}  {t['name']}" for t in hits))
    raise MagnetError(f"找不到任务：{ref}（magnet ls 查看 ID）")


def task_files(details: Dict[str, Any]) -> List[TFile]:
    out = []
    for i, f in enumerate(details.get("files") or []):
        path = "/".join(f.get("components") or [f.get("name") or f"file{i}"])
        out.append(TFile(i, path, f.get("length") or 0, bool((f.get("attributes") or {}).get("padding"))))
    return out


def selected_size(details: Dict[str, Any]) -> int:
    """所选文件的总大小（rqbit 的 total_bytes 按整块计，只选小文件时会偏大）。"""
    return sum(f.get("length") or 0 for f in details.get("files") or []
               if f.get("included") and not (f.get("attributes") or {}).get("padding"))


def speed_bps(live: Optional[Dict[str, Any]], key: str) -> float:
    try:
        return float(live[key]["mbps"]) * 1024 * 1024     # rqbit 的 "mbps" 实际单位是 MiB/s
    except (TypeError, KeyError, ValueError):
        return 0.0


def eta_secs(live: Optional[Dict[str, Any]]) -> Optional[float]:
    tr = (live or {}).get("time_remaining")
    if isinstance(tr, dict):
        d = tr.get("duration")
        if isinstance(d, dict) and "secs" in d:
            return float(d["secs"])
        if isinstance(d, (int, float)):
            return float(d)
    return None


STATUS = {  # 代码 → (中文, 终端颜色)
    "error": ("出错", red), "checking": ("校验中", yellow), "done": ("已完成", green),
    "paused": ("已暂停", dim), "seeding": ("做种中", green), "downloading": ("下载中", cyan),
}


def task_code(s: Dict[str, Any]) -> str:
    st, fin = s.get("state"), s.get("finished")
    if st == "error":
        return "error"
    if st == "initializing":
        return "checking"
    if st == "paused":
        return "done" if fin else "paused"
    if st == "live":
        return "seeding" if fin else "downloading"
    return "checking"


def task_status(s: Dict[str, Any]) -> Tuple[str, Callable[[str], str]]:
    return STATUS[task_code(s)]


# ───────────────────────────── 服务管理（launchd） ─────────────────────────────

DOMAIN = f"gui/{os.getuid()}"


def agent_plist(label: str) -> Path:
    return LAUNCH_AGENTS / f"{label}.plist"


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def agent_status(label: str) -> Tuple[bool, Optional[int]]:
    r = launchctl("print", f"{DOMAIN}/{label}")
    if r.returncode != 0:
        return False, None
    m = re.search(r"^\s*pid = (\d+)", r.stdout, re.M)
    return True, int(m.group(1)) if m else None


def service_start() -> None:
    for label in LABELS:
        if not agent_plist(label).exists():
            raise ServiceDown("后台服务还没安装（运行安装脚本 install.sh）")
        loaded, pid = agent_status(label)
        if not loaded:
            r = launchctl("bootstrap", DOMAIN, str(agent_plist(label)))
            if r.returncode != 0 and "already" not in (r.stderr or "").lower():
                raise MagnetError(f"启动 {label} 失败：{r.stderr.strip()}")
        elif not pid:
            launchctl("kickstart", f"{DOMAIN}/{label}")


def service_stop() -> None:
    for label in LABELS:
        launchctl("bootout", f"{DOMAIN}/{label}")


def service_restart(labels: Tuple[str, ...] = LABELS) -> None:
    for label in labels:
        loaded, _ = agent_status(label)
        if loaded:
            launchctl("kickstart", "-k", f"{DOMAIN}/{label}")
        else:
            launchctl("bootstrap", DOMAIN, str(agent_plist(label)))


def wait_api(api: Api, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            api.ping(timeout=2)
            return True
        except (ServiceDown, RequestTimeout, ApiError):
            time.sleep(0.5)
    return False


def ensure_service(api: Api, quiet: bool = False) -> None:
    try:
        api.ping()
        return
    except ServiceDown:
        pass
    if not quiet:
        eprint(dim("后台服务没有运行，正在启动…"))
    service_start()
    if not wait_api(api, 20):
        raise ServiceDown("rqbit 服务没能启动，看看日志：magnet service logs")


def ctx() -> Tuple[Dict[str, Any], Api]:
    cfg = load_config()
    api = Api(cfg)
    ensure_service(api)
    return cfg, api


def ensure_ui_info(cfg: Dict[str, Any]) -> Tuple[int, str]:
    """图形界面 API 的端口和令牌：令牌只生成一次，App 从 ui.json 读取。"""
    info = read_json(UI_INFO_FILE, {})
    token = info.get("token") or secrets.token_urlsafe(24)
    port = int(cfg.get("ui_port") or 3031)
    if info.get("token") != token or info.get("port") != port:
        write_atomic(UI_INFO_FILE, json.dumps({"port": port, "token": token}), mode=0o600)
    return port, token


# ───────────────────────────── tracker / 缓存 ─────────────────────────────

def public_trackers(cfg: Dict[str, Any]) -> List[str]:
    if not cfg.get("public_trackers"):
        return []
    try:
        lines = [t for t in TRACKERS_FILE.read_text("utf-8").split() if "://" in t]
    except OSError:
        return []
    # HTTP(S) tracker 走 TCP，在屏蔽 UDP 的网络（VPN 出口节点、校园网）里也能用，所以排在前面
    lines.sort(key=lambda t: 0 if t.startswith(("https://", "http://")) else 1)
    return lines[: int(cfg.get("max_extra_trackers") or 0)]


DHT_NODES = [("router.bittorrent.com", 6881), ("dht.transmissionbt.com", 6881), ("dht.libtorrent.org", 25401)]


def udp_probe(timeout: float = 3) -> bool:
    """给 DHT 引导节点发 KRPC ping，看 UDP 能不能出去。"""
    query = b"d1:ad2:id20:" + os.urandom(20) + b"e1:q4:ping1:t2:mk1:y1:qe"
    for host, port in DHT_NODES:
        with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(query, (socket.gethostbyname(host), port))
            if s.recvfrom(2048)[0].startswith(b"d"):
                return True
    return False


def default_route_iface() -> str:
    r = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True)
    m = re.search(r"interface:\s*(\S+)", r.stdout)
    return m.group(1) if m else ""


def dht_hint(api: Api) -> str:
    try:
        size = api.call("GET", "/dht/stats", timeout=3).get("routing_table_size", 0)
    except MagnetError:
        return ""
    if size:
        return ""
    return ("当前 DHT 一个节点都没有：网络可能不通 UDP（开着 VPN / Tailscale 出口节点、校园网或公司网常见），"
            "只能靠 HTTP tracker 找节点，冷门资源会很难解析。")


# 向 tracker 询问做种人数（scrape，BEP 15 / BEP 48）：只问人数，不下载任何内容

def _scrape_udp(url: str, ih: bytes, timeout: float) -> Tuple[int, int]:
    u = urllib.parse.urlsplit(url)
    if not u.hostname or not u.port:
        raise ValueError("无效的 tracker 地址")
    addr = socket.getaddrinfo(u.hostname, u.port, socket.AF_INET, socket.SOCK_DGRAM)[0][4]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        tid = secrets.randbits(32)
        s.sendto(struct.pack(">QII", 0x41727101980, 0, tid), addr)
        data = s.recv(2048)
        if len(data) < 16 or struct.unpack(">II", data[:8]) != (0, tid):
            raise ValueError("握手失败")
        tid = secrets.randbits(32)
        s.sendto(data[8:16] + struct.pack(">II", 2, tid) + ih, addr)
        data = s.recv(2048)
    if len(data) < 20 or struct.unpack(">II", data[:8]) != (2, tid):
        raise ValueError("没有返回人数")
    seeders, _completed, leechers = struct.unpack(">III", data[8:20])
    return seeders, leechers


def _scrape_http(url: str, ih: bytes, timeout: float) -> Tuple[int, int]:
    u = urllib.parse.urlsplit(url)
    head, _, last = u.path.rpartition("/")
    if not last.startswith("announce"):
        raise ValueError("这个 tracker 不支持查询人数")
    query = (u.query + "&" if u.query else "") + "info_hash=" + urllib.parse.quote_from_bytes(ih)
    scrape = urllib.parse.urlunsplit((u.scheme, u.netloc, f"{head}/scrape{last[len('announce'):]}", query, ""))
    with urllib.request.urlopen(urllib.request.Request(scrape, headers={"User-Agent": USER_AGENT}),
                                timeout=timeout) as r:
        data, _ = _bdecode(r.read(1024 * 1024), 0)
    files = data.get(b"files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        raise ValueError("没有返回人数")
    f = files.get(ih)
    if not isinstance(f, dict):
        return 0, 0                 # tracker 上没有这个种子
    return int(f.get(b"complete") or 0), int(f.get(b"incomplete") or 0)


def scrape_swarm(info_hash: str, trackers: List[str], timeout: float = 4, limit: int = 80) -> Dict[str, Any]:
    """并发询问一批 tracker。各 tracker 看到的人大多重叠，所以取最大值而不是求和。"""
    ih = bytes.fromhex(info_hash)
    targets = [t for t in dedupe(trackers) if t.startswith(("udp://", "http://", "https://"))][:limit]
    got: List[Tuple[int, int]] = []
    lock = threading.Lock()

    def ask(url: str) -> None:
        try:
            answer = (_scrape_udp if url.startswith("udp://") else _scrape_http)(url, ih, timeout)
        except Exception:  # noqa: BLE001 - 任何失败都只是「这个 tracker 没回答」
            return
        with lock:
            got.append(answer)

    threads = [threading.Thread(target=ask, args=(t,), daemon=True) for t in targets]
    for th in threads:
        th.start()
    deadline = time.time() + timeout * 2 + 2
    for th in threads:
        th.join(max(0.0, deadline - time.time()))
    with lock:
        answers = list(got)
    return {"seeders": max((s for s, _ in answers), default=0), "leechers": max((le for _, le in answers), default=0),
            "responded": len(answers), "queried": len(targets), "at": time.time()}


def swarm_text(sw: Optional[Dict[str, Any]]) -> str:
    if not sw:
        return ""
    if not sw.get("queried"):
        return "没有可以询问做种人数的 tracker"
    if not sw.get("responded"):
        return f"问了 {sw['queried']} 个 tracker 都没有回应，无法判断做种人数"
    return f"tracker 上 {sw['seeders']} 个做种 · {sw['leechers']} 个在下载（{sw['responded']} 个 tracker 回应）"


def print_swarm(src: Source, cfg: Dict[str, Any]) -> None:
    """命令行里解析超时后，顺便告诉用户这个资源还有多少人在分享。"""
    if src.kind != "magnet" or not src.info_hash:
        return
    sw = run_with_spinner(lambda: scrape_swarm(src.info_hash, src.trackers + public_trackers(cfg)),
                          "正在询问 tracker 有多少人在做种")
    eprint((yellow if sw["seeders"] <= 2 else dim)("   " + swarm_text(sw)))


def update_trackers(cfg: Dict[str, Any]) -> int:
    errors = []
    for url in cfg.get("tracker_urls") or []:
        try:
            text = http_get(url, timeout=20).decode("utf-8", "replace")
        except MagnetError as e:
            errors.append(str(e))
            continue
        found = dedupe([t for t in re.split(r"[\s,]+", text) if re.match(r"(udp|https?)://\S+$", t)])
        if len(found) >= 5:
            write_atomic(TRACKERS_FILE, "\n".join(found) + "\n")
            return len(found)
        errors.append(f"{url}：内容无效")
    raise MagnetError("tracker 列表更新失败：" + "；".join(errors))


def augment_magnet(src: Source, cfg: Dict[str, Any]) -> str:
    extra = [t for t in public_trackers(cfg) if t not in src.trackers]
    return src.value + "".join("&tr=" + urllib.parse.quote(t, safe="") for t in extra)


def cached_meta(src: Source) -> Optional[TorrentMeta]:
    if src.kind != "magnet":
        return None
    try:
        meta = TorrentMeta((TORRENT_CACHE / f"{src.info_hash}.torrent").read_bytes(), "magnet", src.trackers)
    except (OSError, MagnetError):
        return None
    return meta if meta.info_hash == src.info_hash else None


def fetch_torrent(api: Api, cfg: Dict[str, Any], src: Source, timeout: float) -> TorrentMeta:
    """磁力链接 → 通过 rqbit 从 DHT / tracker 拿元数据；种子网址 / 文件 → 直接读取。结果都会缓存。"""
    if src.kind == "magnet":
        meta = cached_meta(src)
        if meta:
            return meta
        try:
            raw = api.resolve_magnet(augment_magnet(src, cfg), timeout)
        except RequestTimeout:
            raise MetaTimeout(f"{int(timeout)} 秒内没拿到元数据：资源可能较冷门，或者暂时没有节点在线")
        meta = TorrentMeta(raw, "magnet", src.trackers)
        if meta.info_hash != src.info_hash:
            raise MagnetError("rqbit 返回的元数据与磁力链接的 hash 不一致")
    else:
        raw = http_get(src.value, timeout=min(timeout, 60)) if src.kind == "url" else Path(src.value).read_bytes()
        meta = TorrentMeta(raw, "file")
    write_atomic(TORRENT_CACHE / f"{meta.info_hash}.torrent", raw)
    return meta


def add_to_rqbit(api: Api, cfg: Dict[str, Any], meta: TorrentMeta, selected: Set[int],
                 out_dir: Optional[str] = None) -> Dict[str, Any]:
    """用（缓存的）种子字节添加任务，不需要再解析一次磁力链接。已存在的任务则合并文件选择。

    调用前必须已经得到用户对 selected 的确认。
    """
    real_ids = {f.idx for f in meta.real_files}
    existing = find_by_hash(api, meta.info_hash)
    if existing:
        d = api.details(meta.info_hash)
        cur = {i for i, f in enumerate(d["files"]) if f.get("included")}
        new = cur | selected
        if new != cur:
            api.set_files(meta.info_hash, new)
        return {"id": existing["id"], "details": api.details(meta.info_hash), "existed": True,
                "added_files": len(new - cur)}
    body = meta.raw
    if meta.origin == "magnet" and not meta.private:
        # resolve_magnet 得到的种子没有 tracker：补上磁力链接自带的 + 公共 tracker（私有种子绝不添加）
        body = torrent_with_trackers(meta, dedupe(meta.magnet_trackers + public_trackers(cfg)))
    params = {"overwrite": "true"}
    if set(selected) != real_ids:
        params["only_files"] = ",".join(str(i) for i in sorted(selected))
    if out_dir:
        base = Path(os.path.expanduser(out_dir)).resolve()
        params["output_folder"] = str(base / meta.name.replace("/", "_")) if len(meta.files) > 1 else str(base)
    res = api.add(body, params)
    res["existed"] = False
    return res


def default_dest(cfg: Dict[str, Any], meta: TorrentMeta, out_dir: Optional[str] = None) -> str:
    base = Path(os.path.expanduser(out_dir or cfg["download_dir"]))
    return str(base / meta.name.replace("/", "_")) if len(meta.files) > 1 else str(base)


# ───────────────────────────── 解析任务（解析中 / 待确认 / 失败） ─────────────────────────────

@contextlib.contextmanager
def locked_queue():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / "queue.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = read_json(QUEUE_FILE, {"items": []})
        yield data
        write_atomic(QUEUE_FILE, json.dumps(data, ensure_ascii=False, indent=1))


def read_jobs() -> List[Dict[str, Any]]:
    items = read_json(QUEUE_FILE, {"items": []}).get("items", [])
    for it in items:          # 兼容 1.x 的队列格式
        it.setdefault("state", "resolving")
        it.setdefault("kind", "magnet")
    return items


def _job(kind: str, source: str, name: str, origin: str, state: str,
         info_hash: Optional[str] = None) -> Dict[str, Any]:
    return {"id": secrets.token_hex(4), "kind": kind, "source": source, "name": name, "origin": origin,
            "state": state, "info_hash": info_hash, "created": time.time(), "tries": 0, "next_try": 0,
            "last_error": "", "size": 0, "files": 0}


def queue_add(src: Source, origin: str = "cli") -> str:
    """新建一个解析任务（解析好之后等待确认，不会自动下载）。同一个链接只保留一个任务。"""
    with locked_queue() as q:
        for it in q["items"]:
            if it["source"] == src.value or (src.info_hash and it.get("info_hash") == src.info_hash):
                if it.get("state") == "failed":
                    it.update(state="resolving", created=time.time(), tries=0, next_try=0, last_error="")
                return it["id"]
        it = _job(src.kind, src.value, src.label, origin, "resolving", src.info_hash)
        q["items"].append(it)
        return it["id"]


def queue_add_ready(kind: str, source: str, meta: TorrentMeta, origin: str) -> str:
    """元数据已经在手（种子文件 / 缓存命中）：直接建一个「待确认」任务。"""
    with locked_queue() as q:
        for it in q["items"]:
            if it.get("info_hash") == meta.info_hash:
                it.update(state="ready", name=meta.name, size=meta.total, files=len(meta.real_files),
                          last_error="", kind=kind, source=source)
                return it["id"]
        it = _job(kind, source, meta.name, origin, "ready", meta.info_hash)
        it.update(size=meta.total, files=len(meta.real_files))
        q["items"].append(it)
        return it["id"]


def queue_find(jid: str) -> Optional[Dict[str, Any]]:
    return next((it for it in read_jobs() if it["id"] == jid), None)


def queue_remove(jid: str) -> bool:
    with locked_queue() as q:
        n = len(q["items"])
        q["items"] = [x for x in q["items"] if x["id"] != jid]
        return len(q["items"]) != n


def load_job_meta(it: Dict[str, Any]) -> TorrentMeta:
    try:
        raw = (TORRENT_CACHE / f"{it.get('info_hash')}.torrent").read_bytes()
    except OSError:
        raise MagnetError("缓存的文件列表丢失了，请重新解析")
    kind = it.get("kind", "magnet")
    trackers: List[str] = []
    if kind == "magnet":
        with contextlib.suppress(MagnetError):
            trackers = parse_source(it["source"]).trackers
    return TorrentMeta(raw, "magnet" if kind == "magnet" else "file", trackers)


def job_public(it: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    now = now or time.time()
    created = it.get("created", now)
    return {"id": it["id"], "name": it.get("name") or "", "state": it.get("state", "resolving"),
            "kind": it.get("kind", "magnet"), "origin": it.get("origin", "cli"), "created": created,
            "elapsed": max(0.0, now - created), "error": it.get("last_error", ""), "tries": it.get("tries", 0),
            "info_hash": it.get("info_hash"), "size": it.get("size", 0), "files": it.get("files", 0),
            "swarm": it.get("swarm")}


def job_trackers(it: Dict[str, Any], cfg: Dict[str, Any]) -> List[str]:
    """询问做种人数用的 tracker：链接 / 种子自带的 + 公共 tracker（私有种子只问它自己的 tracker）。"""
    trackers: List[str] = []
    private = False
    if it.get("kind", "magnet") == "magnet":
        with contextlib.suppress(MagnetError):
            trackers += parse_source(it["source"]).trackers
    if it.get("state") == "ready":
        with contextlib.suppress(MagnetError):
            meta = load_job_meta(it)
            private = meta.private
            trackers += meta.trackers
    if not private:
        trackers += public_trackers(cfg)
    return dedupe(trackers)


def meta_public(meta: TorrentMeta, cfg: Dict[str, Any]) -> Dict[str, Any]:
    real = meta.real_files
    return {"name": meta.name, "hash": meta.info_hash, "total": meta.total, "private": meta.private,
            "summary": type_summary(real), "default": sorted(recommended(meta.files)),
            "dir": tilde(default_dest(cfg, meta)),
            "files": [{"i": f.idx, "path": f.path, "size": f.length, "type": f.category,
                       "suspicious": f.suspicious} for f in real]}


# ───────────────────────────── 清理 / 播放 ─────────────────────────────

def mark_stream(info_hash: str) -> None:
    STREAM_MARKS.mkdir(parents=True, exist_ok=True)
    (STREAM_MARKS / info_hash).touch()


def stream_active(info_hash: str, hours: float = 3) -> bool:
    try:
        return time.time() - (STREAM_MARKS / info_hash).stat().st_mtime < hours * 3600
    except OSError:
        return False


def _within(p: Path, base: Path) -> bool:
    try:
        p.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def cleanup_placeholders(folder: str, files: List[List[Any]], progress: Optional[List[int]],
                         download_dir: str) -> int:
    """任务移出列表后，删掉 rqbit 给「未选择」文件留下的残缺文件。

    rqbit 会把和已选文件共享分块的数据、以及边下边播时读到的位置写进未选文件（写到哪文件就长到哪，
    前面补 0 且占用真实磁盘空间），任务移除后这些残缺文件没有用处。
    files: [[相对路径, 长度, 是否选中, 是否填充文件], …]；已完整下载的未选文件会保留；
    比种子里记录的长度还大的同名文件不是 rqbit 写的，也不动。
    """
    base = Path(folder)
    if not base.is_dir():
        return 0
    removed, parents = 0, set()
    for i, (rel, length, included, padding) in enumerate(files):
        if included and not padding:
            continue
        done = progress[i] if progress and i < len(progress) else None
        if not padding and done is not None and done >= length:
            continue
        p = base / rel
        try:
            st = p.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode) or st.st_size > length or not _within(p, base):
            continue
        with contextlib.suppress(OSError):
            p.unlink()
            removed += 1
            parents.add(p.parent)
    stop = {Path(download_dir).resolve(), base.resolve().parent}
    for d in sorted(parents, key=lambda x: len(x.parts), reverse=True):
        while _within(d, base) and d.resolve() not in stop:
            try:
                d.rmdir()
            except OSError:
                break
            d = d.parent
    return removed


def snapshot_files(details: Dict[str, Any]) -> List[List[Any]]:
    return [["/".join(f.get("components") or [f.get("name", "")]), f.get("length") or 0,
             bool(f.get("included")), bool((f.get("attributes") or {}).get("padding"))]
            for f in details.get("files") or []]


PLAYER_APPS = {
    "iina": ["/Applications/IINA.app", "~/Applications/IINA.app"],
    "vlc": ["/Applications/VLC.app", "~/Applications/VLC.app"],
    "quicktime": ["/System/Applications/QuickTime Player.app", "/Applications/QuickTime Player.app"],
}


def find_player(cfg: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    pref = str(cfg.get("player") or "auto").lower()
    order = ["iina", "mpv", "vlc"] if pref == "auto" else [pref]
    for name in order:
        if name == "mpv":
            path = shutil.which("mpv") or next((p for p in ("/opt/homebrew/bin/mpv", "/usr/local/bin/mpv")
                                                if os.path.exists(p)), None)
        else:
            path = next((os.path.expanduser(p) for p in PLAYER_APPS.get(name, [])
                         if os.path.exists(os.path.expanduser(p))), None)
        if path:
            return name, path
    return "default", None


def launch_player(cfg: Dict[str, Any], target: str) -> str:
    name, path = find_player(cfg)
    if name == "mpv":
        subprocess.Popen([path, "--force-window=immediate", target], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return "mpv"
    if name == "default" or not path:
        subprocess.run(["open", target], check=False)
        return "默认程序"
    cli = Path(path) / "Contents" / "MacOS" / "iina-cli"
    if name == "iina" and cli.exists():
        # iina-cli 直接接收网址，比 open -a 更可靠
        subprocess.run([str(cli), "--no-stdin", target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
        return "IINA"
    subprocess.run(["open", "-a", path, target], check=False)
    return {"iina": "IINA", "vlc": "VLC", "quicktime": "QuickTime"}.get(name, name)


def pick_media(files: List[TFile], included: List[bool], fileno: Optional[int]) -> TFile:
    real = [f for f in files if not f.padding]
    if fileno:
        for f in real:
            if f.num == fileno:
                return f
        raise MagnetError(f"没有编号为 {fileno} 的文件")
    media = [f for f in real if f.category in ("video", "audio")]
    if not media:
        raise MagnetError("这个任务里没有视频 / 音频文件")
    chosen = [f for f in media if included[f.idx]] or media
    return max(chosen, key=lambda f: f.length)


def play_plan(api: Api, info_hash: str, fileno: Optional[int]) -> Tuple[TFile, int]:
    """要播放哪个文件，以及播放它还需要新下载多少字节（0 = 已在下载列表里或已下完，不需要再确认）。"""
    d = api.details(info_hash)
    s = api.stats(info_hash)
    included = [bool(f.get("included")) for f in d["files"]]
    f = pick_media(task_files(d), included, fileno)
    progress = s.get("file_progress") or []
    done = progress[f.idx] if f.idx < len(progress) else 0
    return f, 0 if included[f.idx] or done >= f.length else f.length - done


def play_task(api: Api, cfg: Dict[str, Any], info_hash: str, fileno: Optional[int] = None,
              url_only: bool = False) -> Tuple[str, str, TFile]:
    """返回 (播放器, 方式, 文件)。已下完就播本地文件，否则通过 rqbit 的 HTTP 流边下边播。

    文件不在下载列表里时会把它加进去——调用前要先按 play_plan 的结果向用户确认。
    """
    d = api.details(info_hash)
    s = api.stats(info_hash)
    files = task_files(d)
    included = [bool(f.get("included")) for f in d["files"]]
    f = pick_media(files, included, fileno)
    if not included[f.idx]:
        api.set_files(info_hash, {i for i, inc in enumerate(included) if inc} | {f.idx})
    progress = s.get("file_progress") or []
    local = Path(d["output_folder"]) / f.path
    if f.idx < len(progress) and progress[f.idx] >= f.length and local.exists():
        target, how = str(local), "本地文件"
    else:
        target, how = api.stream_url(info_hash, f.idx, f.base), "边下边播"
    if url_only:
        return "", target, f
    mark_stream(info_hash)
    return launch_player(cfg, target), how, f


# ───────────────────────────── 输出 ─────────────────────────────

def print_files(files: List[TFile], included: Optional[List[bool]] = None,
                progress: Optional[List[int]] = None) -> None:
    shown = [f for f in files if not f.padding]
    w = len(str(max((f.num for f in shown), default=1)))
    cur_dir = None
    for f in shown:
        d = f.path.rpartition("/")[0]
        if d != cur_dir:
            if d:
                print(dim(f"  {' ' * w}   📁 {clean(d)}/"))
            cur_dir = d
        cols = [f"{f.num:>{w}}"]
        if included is not None:
            cols.append(green("✓") if included[f.idx] else dim("·"))
        if progress is not None:
            pct = (progress[f.idx] / f.length * 100) if f.length and f.idx < len(progress) else 100.0
            cols.append(f"{pct:5.1f}%")
        cols.append(dim(f"{hsize(f.length):>9}"))
        line = "  " + "  ".join(cols) + "  " + ("   " if d else "") + f"{f.icon} {clean(f.base)}"
        if f.suspicious:
            line = yellow(line + "   ← 可执行程序 / 快捷方式，谨慎打开")
        print(line)


def type_summary(files: List[TFile]) -> str:
    counts: Dict[str, int] = {}
    for f in files:
        counts[f.category] = counts.get(f.category, 0) + 1
    order = [k for k, *_ in CATEGORIES] + ["other"]
    return " · ".join(f"{CAT_NAME[k]} {counts[k]}" for k in order if k in counts)


def print_meta_header(meta: TorrentMeta) -> None:
    files = meta.real_files
    print(bold(f"🧲 {clean(meta.name)}"))
    bits = [f"{len(files)} 个文件", hsize(meta.total)]
    if meta.piece_length:
        bits.append(f"分块 {hsize(meta.piece_length)}")
    if meta.private:
        bits.append(yellow("私有种子"))
    print(dim(f"   {meta.info_hash} · ") + " · ".join(bits))
    if meta.comment:
        print(dim(f"   备注：{shorten(clean(meta.comment), 120)}"))
    print()


def run_with_spinner(fn: Callable[[], Any], msg: str) -> Any:
    if not sys.stderr.isatty():
        return fn()
    box: Dict[str, Any] = {}

    def target() -> None:
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001 - 转交给主线程
            box["e"] = e

    th = threading.Thread(target=target, daemon=True)
    th.start()
    frames, t0, i = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", time.time(), 0
    try:
        while th.is_alive():
            sys.stderr.write(f"\r{cyan(frames[i % len(frames)])} {msg} {dim(str(int(time.time() - t0)) + 's')} ")
            sys.stderr.flush()
            th.join(0.1)
            i += 1
    finally:
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()
    if "e" in box:
        raise box["e"]
    return box.get("v")


def resolve_cli(api: Api, cfg: Dict[str, Any], src: Source, timeout: Optional[int]) -> TorrentMeta:
    t = float(timeout or cfg.get("metadata_timeout") or 60)
    if src.kind == "magnet":
        meta = cached_meta(src)
        if meta:
            return meta
        ensure_service(api)
        msg = f"正在从 P2P 网络获取元数据（{shorten(src.label, 40)}）"
    else:
        msg = "正在读取种子"
    return run_with_spinner(lambda: fetch_torrent(api, cfg, src, t), msg)


def confirm_cli(summary: str, yes: bool) -> bool:
    """所有下载开始前都要确认；-y 表示已经确认过。"""
    if yes:
        return True
    if not sys.stdin.isatty():
        raise MagnetError("下载前需要确认：请在终端里交互运行，或加 -y 表示已经确认")
    print(summary)
    try:
        ans = input(bold("确认下载？") + " [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes", "是", "确认")


# ───────────────────────────── 命令：查看 / 下载 ─────────────────────────────

def cmd_info(a: argparse.Namespace) -> int:
    cfg = load_config()
    api = Api(cfg)
    src = parse_source(a.target)
    try:
        meta = resolve_cli(api, cfg, src, a.timeout)
    except MetaTimeout as e:
        print_swarm(src, cfg)
        hint = dht_hint(api)
        raise MagnetError(f"{e}。\n" + (hint + "\n" if hint else "") +
                          "可以加大等待时间再试：magnet info … --timeout 180，或者交给后台慢慢解析：magnet add … --queue")
    if a.json:
        print(json.dumps({
            "name": meta.name, "info_hash": meta.info_hash, "total_size": meta.total, "private": meta.private,
            "files": [{"index": f.num, "path": f.path, "size": f.length, "type": f.category,
                       "suspicious": f.suspicious} for f in meta.real_files],
        }, ensure_ascii=False, indent=2))
        return 0
    print_meta_header(meta)
    print_files(meta.files)
    print()
    print(dim("   " + type_summary(meta.real_files)))
    t = a.target if len(a.target) < 70 else {"magnet": "<磁力链接>", "url": "<种子网址>"}.get(src.kind, "<种子文件>")
    print(dim(f"\n下一步：magnet add '{t}'            选择文件并确认后下载"))
    print(dim(f"        magnet add '{t}' -s 1,3-5     只下指定编号（也可以 -s video / -s '*.mkv'）"))
    if any(f.category == "video" for f in meta.real_files):
        print(dim(f"        magnet play '{t}'              边下边播最大的视频"))
    return 0


def interactive_select(meta: TorrentMeta) -> Optional[Set[int]]:
    susp = [f for f in meta.real_files if f.suspicious]
    if susp:
        print(yellow(f"⚠️  有 {len(susp)} 个可执行程序 / 快捷方式文件，推荐规则不会下载它们。"))
    hint = dim(" [回车=推荐  all=全部  q=取消  例：1,3-5  video  *.mkv  !*.txt]")
    while True:
        try:
            ans = input(bold("选择要下载的文件") + hint + "：").strip()
        except EOFError:
            return None
        if ans.lower() in ("q", "quit", "exit", "取消"):
            return None
        try:
            sel = select_files(ans or "rec", meta.files)
        except MagnetError as e:
            print(red(str(e)))
            continue
        if not sel:
            print(red("没有匹配到任何文件，再试一次。"))
            continue
        return sel


def cmd_add(a: argparse.Namespace) -> int:
    cfg, api = ctx()
    src = parse_source(a.target)
    spec = a.select or ("all" if a.all else "video,sub" if a.video else None)
    try:
        meta = resolve_cli(api, cfg, src, a.timeout)
    except MetaTimeout as e:
        if src.kind != "magnet":
            raise
        want_queue = a.queue
        print_swarm(src, cfg)
        hint = dht_hint(api)
        if hint:
            eprint(yellow(hint))
        if not want_queue and sys.stdin.isatty():
            ans = input(f"{yellow(str(e))}\n转入后台继续解析吗？拿到文件列表后会通知你，确认后才会下载 [Y/n] ").strip().lower()
            want_queue = ans in ("", "y", "yes", "是")
        if not want_queue:
            eprint(red("✗ ") + str(e))
            return 1
        queue_add(src, "cli")
        print(green("⏳ 已交给后台解析，") + "拿到文件列表后会通知你，在 MagnetKit 的「待确认」里确认后才会下载")
        return 0

    interactive = sys.stdin.isatty() and not a.yes
    if spec:
        sel = select_files(spec, meta.files)
    elif interactive and len(meta.real_files) > 1:
        print_meta_header(meta)
        print_files(meta.files)
        print()
        picked = interactive_select(meta)
        if picked is None:
            print("已取消。")
            return 1
        sel = picked
    else:
        sel = recommended(meta.files) or {f.idx for f in meta.real_files}
    if a.min_size:
        limit = parse_size(a.min_size)
        sel = {i for i in sel if meta.files[i].length >= limit}
    if not sel:
        raise MagnetError("按这个规则没有选中任何文件")

    chosen = [f for f in meta.real_files if f.idx in sel]
    size = sum(f.length for f in chosen)
    summary = (f"将下载「{clean(meta.name)}」中的 {len(chosen)}/{len(meta.real_files)} 个文件，共 {hsize(size)}"
               f" → {tilde(default_dest(cfg, meta, a.dir))}")
    if not confirm_cli(summary, a.yes):
        print("已取消。")
        return 1
    res = add_to_rqbit(api, cfg, meta, sel, a.dir)
    tid = res.get("id")
    folder = (res.get("details") or {}).get("output_folder") or cfg["download_dir"]
    if res.get("existed"):
        print(green(f"✅ 任务已存在（ID {tid}）") + (f"，追加了 {res['added_files']} 个文件" if res["added_files"]
                                                 else "，所选文件都已在下载列表里"))
    else:
        print(green("✅ 已开始下载：") + bold(clean(meta.name)))
        print(f"   {len(chosen)}/{len(meta.real_files)} 个文件 · {hsize(size)} → {tilde(folder)}")
    print(dim(f"   查看进度：magnet ls    边下边播：magnet play {tid}    打开目录：magnet open {tid}"))
    if a.play:
        player, how, f = play_task(api, cfg, meta.info_hash)
        print(f"▶ 用 {player} 播放（{how}）：{clean(f.path)}")
    return 0


def cmd_play(a: argparse.Namespace) -> int:
    cfg, api = ctx()
    target, fileno = a.target, a.file
    if target and looks_like_source(target):
        src = parse_source(target)
        t = find_by_hash(api, src.info_hash)
        if t:
            info_hash = t["info_hash"]
        else:
            meta = resolve_cli(api, cfg, src, a.timeout)
            f = pick_media(meta.files, [True] * len(meta.files), fileno)
            subs = {x.idx for x in meta.real_files if x.category == "sub"}
            sel = {f.idx} | subs
            size = sum(x.length for x in meta.real_files if x.idx in sel)
            extra = f"和 {len(subs)} 个字幕" if subs else ""
            if not confirm_cli(f"将下载「{clean(f.path)}」{extra}（共 {hsize(size)}）并开始播放", a.yes):
                print("已取消。")
                return 1
            add_to_rqbit(api, cfg, meta, sel)
            info_hash, fileno = meta.info_hash, f.num
    else:
        info_hash = find_task(api, target)["info_hash"]
    f, need = play_plan(api, info_hash, fileno)
    if need and not confirm_cli(f"播放需要下载「{clean(f.path)}」（{hsize(need)}）", a.yes):
        print("已取消。")
        return 1
    player, how, f = play_task(api, cfg, info_hash, f.num, url_only=a.url)
    if a.url:
        print(how)
        return 0
    print(f"▶ 用 {player} 播放（{how}）：{clean(f.path)}")
    if how == "边下边播":
        print(dim("  开头需要缓冲几秒；拖动进度条时 rqbit 会优先下载对应位置。"))
    return 0


def cmd_open(a: argparse.Namespace) -> int:
    cfg, api = ctx()
    if not a.task:
        Path(cfg["download_dir"]).mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", cfg["download_dir"]])
        return 0
    t = find_task(api, a.task)
    d = api.details(t["info_hash"])
    folder = Path(d["output_folder"])
    if a.file:
        files = [f for f in task_files(d) if f.num == a.file and not f.padding]
        if not files:
            raise MagnetError(f"没有编号为 {a.file} 的文件")
        p = folder / files[0].path
        if not p.exists():
            raise MagnetError(f"文件还不存在：{tilde(p)}")
        subprocess.run(["open", "-R", str(p)] if a.reveal else ["open", str(p)])
    else:
        subprocess.run(["open", str(folder if folder.exists() else cfg["download_dir"])])
    return 0


# ───────────────────────────── 命令：任务管理 ─────────────────────────────

JOB_STATE_TEXT = {"resolving": "解析中", "ready": "待确认", "failed": "解析失败"}


def cmd_ls(a: argparse.Namespace) -> int:
    cfg, api = ctx()
    items = sorted(api.torrents(stats=True), key=lambda t: t["id"])
    jobs = read_jobs()
    if not items and not jobs:
        print("当前没有任务。打开 MagnetKit，或者 magnet add '<磁力链接>'。")
    if items:
        print(dim(f"{'ID':>3}  {wpad('状态', 6)}  {wpad('进度', 19)}  {wrpad('大小', 9)}  {wrpad('下载', 10)}  "
                  f"{wrpad('上传', 10)}  {wrpad('剩余', 9)}  名称"))
    for t in items:
        s = t.get("stats") or {}
        live = s.get("live")
        label, color = task_status(s)
        total = s.get("total_bytes") or 0
        frac = (s.get("progress_bytes") or 0) / total if total else (1.0 if s.get("finished") else 0.0)
        down, up = speed_bps(live, "download_speed"), speed_bps(live, "upload_speed")
        eta = eta_secs(live) if not s.get("finished") else None
        try:
            size = selected_size(api.details(t["info_hash"]))
        except MagnetError:
            size = total
        print(f"{t['id']:>3}  {color(wpad(label, 6))}  {bar(frac)} {frac * 100:5.1f}%  {hsize(size):>9}  "
              f"{(hsize(down) + '/s') if down else '-':>10}  {(hsize(up) + '/s') if up else '-':>10}  "
              f"{wrpad(hduration(eta), 9)}  {clean(t['name'])}")
        if s.get("error"):
            print(red(f"     错误：{s['error']}"))
    for it in jobs:
        state = JOB_STATE_TEXT.get(it["state"], it["state"])
        err = f"：{it['last_error']}" if it.get("last_error") and it["state"] != "ready" else ""
        print(yellow(f"  ⏳ {state}：{clean(it.get('name') or it['source'][:60])}{err}"))
    active = {t["info_hash"] for t in items}
    hist = [h for h in read_json(WATCH_STATE_FILE, {}).get("history", []) if h["hash"] not in active]
    if hist and not a.quiet:
        print(dim("\n最近完成（已移出任务列表，文件还在）："))
        for h in hist[-5:][::-1]:
            print(dim(f"  ✓ {time.strftime('%m-%d %H:%M', time.localtime(h['at']))}  {hsize(h.get('size', 0)):>9}"
                      f"  {clean(h['name'])}  → {tilde(h.get('folder', ''))}"))
    return 0


def cmd_show(a: argparse.Namespace) -> int:
    cfg, api = ctx()
    t = find_task(api, a.task)
    d = api.details(t["info_hash"])
    s = api.stats(t["info_hash"])
    label, color = task_status(s)
    files = task_files(d)
    included = [bool(f.get("included")) for f in d["files"]]
    total = s.get("total_bytes") or 0
    frac = (s.get("progress_bytes") or 0) / total if total else 1.0
    print(bold(f"🧲 {clean(d['name'])}") + dim(f"  (ID {t['id']})"))
    print(dim(f"   {d['info_hash']} · ") + color(label) + f" · {frac * 100:.1f}% · 已选 "
          f"{sum(1 for f in files if included[f.idx] and not f.padding)}/{sum(1 for f in files if not f.padding)}"
          f" 个文件 {hsize(selected_size(d))}")
    print(dim(f"   目录：{tilde(d['output_folder'])}"))
    if s.get("error"):
        print(red(f"   错误：{s['error']}"))
    print()
    print_files(files, included=included, progress=s.get("file_progress") or [])
    print(dim(f"\n修改选择：magnet select {t['id']} <编号/类别/通配>    播放：magnet play {t['id']} [编号]"))
    return 0


def cmd_select(a: argparse.Namespace) -> int:
    cfg, api = ctx()
    t = find_task(api, a.task)
    d = api.details(t["info_hash"])
    files = task_files(d)
    sel = select_files(" ".join(a.spec), files)
    if not sel:
        raise MagnetError("按这个规则没有选中任何文件")
    cur = {i for i, f in enumerate(d["files"]) if f.get("included")}
    added = sel - cur
    if added:
        size = sum(f.length for f in files if f.idx in added)
        if not confirm_cli(f"将开始下载 {len(added)} 个新文件（{hsize(size)}）", a.yes):
            print("已取消。")
            return 1
    api.set_files(t["info_hash"], sel)
    size = sum(f.length for f in files if f.idx in sel)
    print(green("✅ 已更新：") + f"{clean(d['name'])} 现在下载 {len(sel)} 个文件，共 {hsize(size)}")
    return 0


def _each_task(api: Api, refs: List[str]) -> List[Dict[str, Any]]:
    if any(r.lower() in ("all", "全部") for r in refs):
        return api.torrents()
    return [find_task(api, r) for r in refs]


def cmd_pause(a: argparse.Namespace) -> int:
    cfg, api = ctx()
    for t in _each_task(api, a.tasks):
        api.action(t["info_hash"], "pause")
        print(f"⏸  已暂停：{clean(t['name'])}")
    return 0


def cmd_resume(a: argparse.Namespace) -> int:
    cfg, api = ctx()
    for t in _each_task(api, a.tasks):
        api.action(t["info_hash"], "start")
        print(f"▶  已继续：{clean(t['name'])}")
    return 0


def remove_task(api: Api, cfg: Dict[str, Any], info_hash: str) -> Tuple[str, int]:
    """移出任务列表（保留文件），并清理未选文件的残缺文件。返回 (目录, 清理数量)。"""
    d = api.details(info_hash)
    s = api.stats(info_hash)
    api.action(info_hash, "forget")
    n = cleanup_placeholders(d["output_folder"], snapshot_files(d), s.get("file_progress"), cfg["download_dir"])
    return d["output_folder"], n


def cmd_rm(a: argparse.Namespace) -> int:
    cfg, api = ctx()
    t = find_task(api, a.task)
    if a.delete:
        if not a.yes:
            ans = input(red(f"确定删除任务「{t['name']}」以及已下载的文件？[y/N] ")).strip().lower()
            if ans not in ("y", "yes", "是"):
                print("已取消。")
                return 1
        api.action(t["info_hash"], "delete")
        print(f"🗑  已删除任务和文件：{clean(t['name'])}")
        return 0
    folder, n = remove_task(api, cfg, t["info_hash"])
    print(f"✅ 已移出任务列表（文件保留在 {tilde(folder)}）" + (f"，清理了 {n} 个未选文件的残缺文件" if n else ""))
    return 0


def cmd_queue(a: argparse.Namespace) -> int:
    if a.action == "clear":
        with locked_queue() as q:
            n = len(q["items"])
            q["items"] = []
        print(f"已清空解析任务（{n} 项）")
        return 0
    items = read_jobs()
    if not items:
        print("没有解析中或待确认的项目。")
    for it in items:
        state = JOB_STATE_TEXT.get(it["state"], it["state"])
        print(f"⏳ [{state}] {clean(it.get('name') or '')}  {dim(it['source'][:70])}")
        extra = f" · 上次：{it['last_error']}" if it.get("last_error") else ""
        print(dim(f"   加入于 {hago(it['created'])} · 已尝试 {it.get('tries', 0)} 次{extra}"))
        if it.get("swarm"):
            print(dim(f"   {swarm_text(it['swarm'])}（{hago(it['swarm']['at'])}）"))
    if any(it["state"] == "ready" for it in items):
        print(dim("\n待确认的项目请在 MagnetKit 的「待确认」里查看文件并确认下载。"))
    return 0


# ───────────────────────────── 命令：服务 / 配置 / 自检 ─────────────────────────────

def cmd_ui(a: argparse.Namespace) -> int:
    cfg = load_config()
    if a.rqbit:
        api = Api(cfg)
        ensure_service(api, quiet=True)
        url = f"http://{cfg['api_host']}:{cfg['api_port']}/web/"
        subprocess.run(["pbcopy"], input=cfg["password"].encode(), check=False)
        subprocess.run(["open", url], check=False)
        print(f"rqbit 网页界面：{url}")
        print(f"浏览器会要求登录：用户名 {bold(cfg['username'])}，密码已复制到剪贴板。")
        return 0
    if APP_BUNDLE.exists() and not a.browser:
        subprocess.run(["open", str(APP_BUNDLE)], check=False)
        return 0
    port, token = ensure_ui_info(cfg)
    subprocess.run(["open", f"http://127.0.0.1:{port}/#token={token}"], check=False)
    return 0


def cmd_service(a: argparse.Namespace) -> int:
    cfg = load_config()
    api = Api(cfg)
    if a.action == "start":
        service_start()
        print(green("✅ 已启动") if wait_api(api, 20) else red("✗ 启动后 API 没有响应，看看 magnet service logs"))
    elif a.action == "stop":
        service_stop()
        print("已停止后台服务（下次登录或打开 MagnetKit 时会自动启动）")
    elif a.action == "restart":
        service_restart()
        print(green("✅ 已重启") if wait_api(api, 20) else red("✗ 重启后 API 没有响应，看看 magnet service logs"))
    elif a.action == "logs":
        for name in ("rqbit.log", "watcher.log", "app.log"):
            p = LOG_DIR / name
            if p.exists():
                print(bold(f"── {tilde(p)}"))
                lines = p.read_text("utf-8", "replace").splitlines()
                print("\n".join(lines[-a.lines:]))
    else:
        for label in LABELS:
            loaded, pid = agent_status(label)
            state = green(f"运行中 pid {pid}") if pid else (yellow("已加载但未运行") if loaded else red("未加载"))
            print(f"{label:<24} {state}")
        try:
            info = api.ping()
            print(f"rqbit API  {green('正常')}  v{info.get('version')}  http://{cfg['api_host']}:{cfg['api_port']}")
        except MagnetError as e:
            print(f"rqbit API  {red(str(e))}")
    return 0


def cmd_trackers(a: argparse.Namespace) -> int:
    cfg = load_config()
    if a.update:
        n = update_trackers(cfg)
        print(green(f"✅ 已更新 {n} 个 tracker"))
    try:
        lines = [t for t in TRACKERS_FILE.read_text("utf-8").split() if "://" in t]
        age = hago(TRACKERS_FILE.stat().st_mtime)
    except OSError:
        print("还没有 tracker 列表（magnet trackers --update 下载）")
        return 0
    used = min(len(lines), int(cfg.get("max_extra_trackers") or 0)) if cfg.get("public_trackers") else 0
    print(f"公共 tracker：{len(lines)} 个（{age}更新），给磁力链接追加前 {used} 个；私有种子不会追加。")
    for t in lines[:8]:
        print(dim("  " + t))
    if len(lines) > 8:
        print(dim(f"  … 共 {len(lines)} 个，完整列表：{tilde(TRACKERS_FILE)}"))
    return 0


def cmd_config(a: argparse.Namespace) -> int:
    raw = read_raw_config()
    if a.action == "set":
        if not a.key or a.value is None:
            raise MagnetError("用法：magnet config set <键> <值>")
        if a.key not in DEFAULT_CONFIG:
            raise MagnetError(f"没有这个配置项：{a.key}")
        try:
            value = json.loads(a.value)
        except ValueError:
            value = a.value
        if a.key in UI_FIELDS:
            value = coerce_config(a.key, value)
        raw[a.key] = value
        save_raw_config(raw)
        print(green(f"✅ {a.key} = {json.dumps(value, ensure_ascii=False)}"))
        if a.key in RESTART_KEYS:
            print(dim("   这一项需要重启服务才生效：magnet service restart"))
        return 0
    cfg = load_config()
    print(bold(f"配置文件：{tilde(CONFIG_PATH)}") + dim("（修改：magnet config set <键> <值>，或在 MagnetKit 的「设置」里改）"))
    for k, help_text in CONFIG_HELP.items():
        v = cfg.get(k)
        shown = tilde(v) if k == "download_dir" else json.dumps(v, ensure_ascii=False)
        print(f"  {k:<20} {shown:<14} {dim(help_text)}")
    return 0


def jxa_default_app(url: str) -> Optional[str]:
    script = ("ObjC.import('AppKit');"
              f"var u=$.NSWorkspace.sharedWorkspace.URLForApplicationToOpenURL($.NSURL.URLWithString({json.dumps(url)}));"
              "u.isNil()?'':u.path.js")
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", script], capture_output=True, text=True)
    return r.stdout.strip() or None


def upnp_status(cfg: Dict[str, Any]) -> Tuple[str, str]:
    """根据 rqbit 的 UPnP 日志判断端口映射有没有真正生效。"""
    if not cfg.get("upnp"):
        return "ok", "UPnP 端口映射：关闭（别人无法主动连进来，冷门资源会更难连上）"
    try:
        text = UPNP_LOG.read_text("utf-8", "replace")
    except OSError:
        return "warn", "UPnP 端口映射：已开启，但还没有端口映射日志（重启下载服务后再看）"
    done = re.findall(r"successfully port forwarded.*", text)
    if done:
        port = re.search(r"port=(\d+)", done[-1])
        ip = re.search(r"local_ip=(\S+)", done[-1])
        return "ok", (f"UPnP 端口映射：路由器已把端口 {port.group(1) if port else cfg['listen_port']} 转给本机"
                      f"{'（' + ip.group(1) + '）' if ip else ''}，别人可以主动连进来")
    failed = re.findall(r"failed to (?:run SSDP/UPNP discovery|forward port)[^\n]*", text)
    if failed:
        return "warn", f"UPnP 端口映射：没有成功（{shorten(failed[-1], 160)}）"
    found = re.findall(r"discovered (\d+) endpoints", text)
    if found and found[-1] == "0":
        return "warn", ("UPnP 端口映射：没有生效，局域网里找不到支持 UPnP 的路由器"
                        "（校园网 / 公司网、路由器关闭了 UPnP，或开着 VPN 时常见）")
    return "warn", "UPnP 端口映射：已开启，正在和路由器协商，稍后再看"


def run_checks(cfg: Dict[str, Any], api: Api, net: bool = False) -> List[Tuple[str, str]]:
    """自检：返回 [(ok|warn|bad, 说明)]，命令行和图形界面共用。"""
    out: List[Tuple[str, str]] = []
    rq = find_rqbit(cfg)
    if rq:
        ver = subprocess.run([rq, "--version"], capture_output=True, text=True).stdout.strip()
        out.append(("ok", f"下载引擎：{ver}（{tilde(rq)}）"))
    else:
        out.append(("bad", "没找到 rqbit：brew install rqbit"))
    if CONFIG_PATH.exists():
        mode = stat.S_IMODE(CONFIG_PATH.stat().st_mode)
        out.append(("ok" if mode & 0o077 == 0 else "warn", f"配置文件：{tilde(CONFIG_PATH)}（权限 {oct(mode)[2:]}）"))
    else:
        out.append(("bad", "没有配置文件，请运行安装脚本"))
    for label, name in ((LABEL_RQBIT, "下载服务"), (LABEL_WATCHER, "后台监控")):
        loaded, pid = agent_status(label)
        out.append(("ok" if pid else "bad", f"{name}（{label}）：" + (f"运行中，pid {pid}" if pid else "没有运行")))
    try:
        info = api.ping()
        out.append(("ok", f"rqbit API：v{info.get('version')}，已认证"))
        dht = api.call("GET", "/dht/stats")
        size = dht.get("routing_table_size", 0)
        out.append(("ok" if size > 20 else "warn",
                    f"DHT 路由表：{size} 个节点" + ("" if size > 20 else "（刚启动时较少，几分钟后会增加）")))
        if udp_probe():
            out.append(("ok", "UDP 出网正常（DHT / UDP tracker 可用）"))
        else:
            iface = default_route_iface()
            why = (f"默认路由走的是隧道 {iface}（VPN / Tailscale 出口节点等），它可能不转发 UDP"
                   if iface.startswith(("utun", "ppp", "ipsec")) else "所在网络可能屏蔽了 UDP（校园网 / 公司网常见）")
            out.append(("warn", f"UDP 不通，DHT 用不了：{why}。磁力链接只能靠 HTTP tracker 解析，冷门资源会很慢甚至失败；"
                                "关掉出口节点 / 换个网络后会自动恢复。"))
    except MagnetError as e:
        out.append(("bad", f"rqbit API：{e}"))
    pid = agent_status(LABEL_RQBIT)[1]
    args = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True).stdout if pid else ""
    if pid and (bool(cfg.get("upnp")) == ("--disable-upnp-port-forward" in args)
                or bool(cfg.get("utp")) != ("--experimental-enable-utp-listen" in args)):
        out.append(("warn", "UPnP / uTP 的设置改过了，但下载服务还没重启，重启后才生效"))
    out.append(upnp_status(cfg))
    out.append(("ok", "uTP 协议：" + ("已开启（rqbit 的实验功能）" if cfg.get("utp") else "关闭")))
    port = int(cfg.get("ui_port") or 3031)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=3) as r:
            ok = json.loads(r.read()).get("ok")
        out.append(("ok" if ok else "bad", f"图形界面服务：http://127.0.0.1:{port}"))
    except (OSError, ValueError, urllib.error.URLError):
        out.append(("bad", f"图形界面服务没有响应（端口 {port}）"))
    if TRACKERS_FILE.exists():
        n = len([t for t in TRACKERS_FILE.read_text().split() if "://" in t])
        age_days = (time.time() - TRACKERS_FILE.stat().st_mtime) / 86400
        out.append(("ok" if age_days < 3 else "warn", f"公共 tracker：{n} 个（{hago(TRACKERS_FILE.stat().st_mtime)}更新）"))
    else:
        out.append(("warn", "还没有公共 tracker 列表"))
    dl = Path(cfg["download_dir"])
    try:
        dl.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=dl, prefix=".mk-"):
            pass
        out.append(("ok", f"下载目录可写：{tilde(dl)}"))
    except OSError as e:
        out.append(("bad", f"下载目录不可写：{tilde(dl)}（{e}）"))
    name, path = find_player(cfg)
    out.append(("ok" if path else "warn",
                f"播放器：{name} {tilde(path)}" if path else "没找到 IINA / mpv / VLC，边下边播会用默认程序"))
    out.append(("ok" if APP_BUNDLE.exists() else "bad", f"MagnetKit.app：{tilde(APP_BUNDLE)}"
                if APP_BUNDLE.exists() else "MagnetKit.app 没有安装"))
    notif = read_json(STATE_DIR / "app-status.json", {}).get("notifications")
    if not cfg.get("notify", True):
        out.append(("ok", "系统通知：已在设置里关闭"))
    elif notif:
        text = {"authorized": "已允许", "provisional": "静默送达（可在系统设置里改成横幅）",
                "notDetermined": "还没授权（第一次提醒时系统会询问）",
                "denied": "被系统设置关闭了（系统设置 → 通知 → MagnetKit）"}.get(notif, notif)
        out.append(("ok" if notif in ("authorized", "provisional", "notDetermined") else "warn", f"系统通知：{text}"))
    app = jxa_default_app("magnet:?xt=urn:btih:" + "0" * 40)
    out.append(("ok" if app and "MagnetKit" in app else "warn", f"磁力链接默认打开方式：{tilde(app) if app else '无'}"))
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "probe.torrent"
        probe.write_bytes(b"d4:infod4:name1:xee")
        app = jxa_default_app(probe.as_uri())
    out.append(("ok" if app and "MagnetKit" in app else "warn", f".torrent 默认打开方式：{tilde(app) if app else '无'}"))
    if net:
        sintel = ("magnet:?xt=urn:btih:08ada5a7a6183aae1e09d831df6748d566095a10&dn=Sintel"
                  "&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337")
        t0 = time.time()
        try:
            raw = api.resolve_magnet(augment_magnet(parse_source(sintel), cfg), 90)
            meta = TorrentMeta(raw, "magnet")
            out.append(("ok", f"联网测试：{time.time() - t0:.1f} 秒解析出《{meta.name}》（Blender 开源电影）的 "
                              f"{len(meta.real_files)} 个文件"))
        except MagnetError as e:
            out.append(("bad", f"联网测试失败：{e}"))
    return out


def cmd_doctor(a: argparse.Namespace) -> int:
    cfg = load_config()
    checks = run_checks(cfg, Api(cfg), a.net)
    marks = {"ok": green("✓ "), "warn": yellow("! "), "bad": red("✗ ")}
    for level, text in checks:
        print(marks[level] + text)
    problems = sum(1 for level, _ in checks if level == "bad")
    print()
    print(green("一切正常 ✨") if not problems else red(f"发现 {problems} 个问题"))
    return 1 if problems else 0


# ───────────────────────────── 通知 ─────────────────────────────

def as_str(s: str) -> str:
    s = str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\r", "")
    return '("' + s.replace("\n", '" & linefeed & "') + '")'


def osa(script: str, timeout: Optional[float] = None) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(["osascript"], input=script, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def notify(title: str, message: str = "", subtitle: str = "", cfg: Optional[Dict[str, Any]] = None,
           view: str = "") -> None:
    """优先让 MagnetKit.app 发原生通知（点击会打开对应页面）；App 不在时退回 AppleScript 通知。"""
    try:
        if cfg is None:
            cfg = load_config()
    except MagnetError:
        cfg = DEFAULT_CONFIG
    if not cfg.get("notify", True):
        return
    info = read_json(UI_INFO_FILE, {})
    if APP_BUNDLE.exists() and info.get("token") and not _ROOT:
        query = urllib.parse.urlencode({"token": info["token"], "title": title, "subtitle": subtitle,
                                        "body": message, "view": view}, quote_via=urllib.parse.quote)
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            if subprocess.run(["open", "-g", "magnetkit://notify?" + query], capture_output=True,
                              timeout=15).returncode == 0:
                return
    script = f"display notification {as_str(message)} with title {as_str(title)}"
    if subtitle:
        script += f" subtitle {as_str(subtitle)}"
    osa(script, timeout=10)


# ───────────────────────────── 图形界面的本地 API ─────────────────────────────

class NotFound(MagnetError):
    pass


class Confirm(Exception):
    """这个操作会开始新的下载，需要前端先让用户确认（HTTP 409）。"""

    def __init__(self, info: Dict[str, Any]):
        super().__init__("需要确认")
        self.info = info


def _json_body_int_list(value: Any, what: str) -> Set[int]:
    if not isinstance(value, list) or not value:
        raise MagnetError(f"没有选择任何{what}")
    try:
        return {int(i) for i in value}
    except (TypeError, ValueError):
        raise MagnetError("文件编号无效")


class UIApi:
    """图形界面调用的接口。所有会开始下载的接口都要求请求里带 confirm: true。"""

    def __init__(self, watcher: "Watcher"):
        self.w = watcher
        self._details: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._dlock = threading.Lock()
        h40 = r"([0-9a-f]{40})"
        self.routes: List[Tuple[str, str, Callable[..., Any]]] = [
            ("GET", r"/api/state", self.state),
            ("POST", r"/api/resolve", self.resolve),
            ("GET", r"/api/job/(\w+)", self.job),
            ("DELETE", r"/api/job/(\w+)", self.job_delete),
            ("POST", r"/api/job/(\w+)/retry", self.job_retry),
            ("POST", r"/api/add", self.add),
            ("GET", rf"/api/task/{h40}", self.task),
            ("POST", rf"/api/task/{h40}/files", self.task_files),
            ("POST", rf"/api/task/{h40}/play", self.task_play),
            ("POST", rf"/api/task/{h40}/(pause|resume|remove|delete)", self.task_action),
            ("POST", r"/api/open", self.open),
            ("GET", r"/api/config", self.config_get),
            ("POST", r"/api/config", self.config_set),
            ("GET", r"/api/doctor", self.doctor),
            ("POST", r"/api/service/restart", self.service_restart),
            ("POST", r"/api/trackers/update", self.trackers_update),
            ("GET", r"/api/logs", self.logs),
            ("POST", r"/api/history/remove", self.history_remove),
            ("POST", r"/api/rqbit-ui", self.rqbit_ui),
        ]

    @property
    def cfg(self) -> Dict[str, Any]:
        return self.w.cfg

    @property
    def api(self) -> Api:
        return self.w.api

    def dispatch(self, method: str, path: str, body: Dict[str, Any], query: Dict[str, str]) -> Any:
        for m, pattern, fn in self.routes:
            if m == method:
                hit = re.fullmatch(pattern, path)
                if hit:
                    return fn(*hit.groups(), body=body, query=query)
        raise NotFound("没有这个接口")

    def details(self, h: str, fresh: bool = False) -> Dict[str, Any]:
        now = time.time()
        with self._dlock:
            hit = self._details.get(h)
            if hit and not fresh and now - hit[0] < 10:
                return hit[1]
        d = self.api.details(h)
        with self._dlock:
            self._details[h] = (now, d)
        return d

    def forget(self, h: str) -> None:
        with self._dlock:
            self._details.pop(h, None)

    # ── 总览 ──
    def summary(self, t: Dict[str, Any]) -> Dict[str, Any]:
        s = t.get("stats") or {}
        live = s.get("live")
        d = self.details(t["info_hash"])
        files = [f for f in task_files(d) if not f.padding]
        inc = [bool(f.get("included")) for f in d["files"]]
        total = s.get("total_bytes") or 0
        frac = (s.get("progress_bytes") or 0) / total if total else (1.0 if s.get("finished") else 0.0)
        size = selected_size(d)
        code = task_code(s)
        return {"hash": t["info_hash"], "id": t["id"], "name": t["name"], "status": code,
                "status_text": STATUS[code][0], "progress": frac, "size": size, "done": int(size * frac),
                "down": speed_bps(live, "download_speed"), "up": speed_bps(live, "upload_speed"),
                "eta": eta_secs(live) if not s.get("finished") else None, "error": s.get("error"),
                "folder": d.get("output_folder"), "folder_display": tilde(d.get("output_folder") or ""),
                "files": len(files), "selected": sum(1 for f in files if inc[f.idx]),
                "media": any(f.category in ("video", "audio") for f in files)}

    def state(self, body: Dict[str, Any], query: Dict[str, str]) -> Dict[str, Any]:
        out: Dict[str, Any] = {"version": VERSION, "rqbit": True, "error": "", "tasks": [], "jobs": [],
                               "history": [], "speed": {"down": 0.0, "up": 0.0},
                               "download_dir": tilde(self.cfg["download_dir"])}
        try:
            items = self.api.torrents(stats=True)
        except MagnetError as e:
            items, out["rqbit"], out["error"] = [], False, str(e)
        for t in sorted(items, key=lambda x: x["id"]):
            with contextlib.suppress(MagnetError):
                out["tasks"].append(self.summary(t))
        out["speed"] = {"down": sum(x["down"] for x in out["tasks"]), "up": sum(x["up"] for x in out["tasks"])}
        now = time.time()
        out["jobs"] = [job_public(it, now) for it in read_jobs()]
        active = {x["hash"] for x in out["tasks"]}
        with self.w.lock:
            hist = list(self.w.state.get("history", []))
        for h in reversed(hist):
            if h["hash"] not in active:
                folder = h.get("folder") or ""
                out["history"].append(dict(h, exists=bool(folder) and os.path.exists(folder),
                                           folder_display=tilde(folder)))
        return out

    # ── 解析 ──
    def resolve(self, body: Dict[str, Any], query: Dict[str, str]) -> Dict[str, Any]:
        if body.get("torrent_b64"):
            try:
                raw = base64.b64decode(str(body["torrent_b64"]), validate=True)
            except (ValueError, TypeError):
                raise MagnetError("种子文件的数据损坏了")
            meta = TorrentMeta(raw, "file")
            write_atomic(TORRENT_CACHE / f"{meta.info_hash}.torrent", raw)
            if find_by_hash(self.api, meta.info_hash):
                return {"existing": meta.info_hash, "name": meta.name}
            return {"job": queue_add_ready("upload", str(body.get("name") or meta.name), meta, "ui")}
        src = parse_source(str(body.get("source") or ""))
        if src.info_hash and find_by_hash(self.api, src.info_hash):
            return {"existing": src.info_hash, "name": src.label}
        if src.kind == "file":
            try:
                raw = Path(src.value).read_bytes()
            except OSError as e:
                raise MagnetError(f"读不了这个文件（{e.strerror}）。可以直接把种子文件拖进窗口。")
            meta = TorrentMeta(raw, "file")
            write_atomic(TORRENT_CACHE / f"{meta.info_hash}.torrent", raw)
            if find_by_hash(self.api, meta.info_hash):
                return {"existing": meta.info_hash, "name": meta.name}
            return {"job": queue_add_ready("file", src.value, meta, "ui")}
        meta = cached_meta(src)
        if meta:
            return {"job": queue_add_ready("magnet", src.value, meta, "ui")}
        jid = queue_add(src, "ui")
        it = queue_find(jid)
        if it and it["state"] == "resolving":
            self.w.start_job(it)
            self.w.scrape_tick(time.time(), [it])      # 马上问一次做种人数，不等下一轮
        log(f"界面新建解析任务：{src.label}")
        return {"job": jid}

    def job(self, jid: str, body: Dict[str, Any], query: Dict[str, str]) -> Dict[str, Any]:
        it = queue_find(jid)
        if not it:
            raise NotFound("这个解析任务已经不存在了")
        self.w.job_seen[jid] = time.time()
        out = job_public(it)
        if out["state"] == "resolving" and out["elapsed"] > 15:
            out["hint"] = dht_hint(self.api)
        if out["state"] == "ready":
            meta = load_job_meta(it)
            out["meta"] = meta_public(meta, self.cfg)
            out["existing"] = bool(find_by_hash(self.api, meta.info_hash))
        return out

    def job_delete(self, jid: str, body: Dict[str, Any], query: Dict[str, str]) -> Dict[str, Any]:
        return {"ok": queue_remove(jid)}

    def job_retry(self, jid: str, body: Dict[str, Any], query: Dict[str, str]) -> Dict[str, Any]:
        with locked_queue() as q:
            it = next((x for x in q["items"] if x["id"] == jid), None)
            if not it:
                raise NotFound("这个解析任务已经不存在了")
            it.update(state="resolving", created=time.time(), tries=0, next_try=0, last_error="")
            job = dict(it)
        self.w.start_job(job)
        return {"ok": True}

    # ── 确认下载 ──
    def add(self, body: Dict[str, Any], query: Dict[str, str]) -> Dict[str, Any]:
        if body.get("confirm") is not True:
            raise MagnetError("开始下载前需要确认")
        it = queue_find(str(body.get("job") or ""))
        if not it or it.get("state") != "ready":
            raise MagnetError("找不到待确认的文件列表，请重新解析")
        meta = load_job_meta(it)
        sel = _json_body_int_list(body.get("files"), "文件")
        if not sel <= {f.idx for f in meta.real_files}:
            raise MagnetError("文件编号无效")
        out_dir = str(body.get("dir") or "").strip() or None
        if out_dir and not os.path.isabs(os.path.expanduser(out_dir)):
            raise MagnetError("保存位置要写完整路径")
        res = add_to_rqbit(self.api, self.cfg, meta, sel, out_dir)
        queue_remove(it["id"])
        self.forget(meta.info_hash)
        size = sum(f.length for f in meta.real_files if f.idx in sel)
        log(f"界面确认下载：{meta.name}（{len(sel)} 个文件，{hsize(size)}）")
        out: Dict[str, Any] = {"hash": meta.info_hash, "id": res.get("id"), "existed": res.get("existed"),
                               "added_files": res.get("added_files", len(sel)), "size": size}
        if body.get("play"):
            media = [f for f in meta.real_files if f.idx in sel and f.category in ("video", "audio")]
            if media:
                f = max(media, key=lambda x: x.length)
                player, how, _ = play_task(self.api, self.cfg, meta.info_hash, f.num)
                out["played"] = {"player": player, "how": how, "file": f.path}
        return out

    # ── 任务 ──
    def task(self, h: str, body: Dict[str, Any], query: Dict[str, str]) -> Dict[str, Any]:
        d = self.details(h, fresh=True)
        s = self.api.stats(h)
        prog = s.get("file_progress") or []
        code = task_code(s)
        total = s.get("total_bytes") or 0
        files = [f for f in task_files(d) if not f.padding]
        return {"hash": h, "name": d["name"], "folder": d["output_folder"], "folder_display": tilde(d["output_folder"]),
                "status": code, "status_text": STATUS[code][0],
                "progress": (s.get("progress_bytes") or 0) / total if total else 1.0,
                "files": [{"i": f.idx, "path": f.path, "size": f.length, "type": f.category,
                           "suspicious": f.suspicious, "included": bool(d["files"][f.idx].get("included")),
                           "done": min(prog[f.idx], f.length) if f.idx < len(prog) else 0} for f in files]}

    def task_files(self, h: str, body: Dict[str, Any], query: Dict[str, str]) -> Dict[str, Any]:
        d = self.details(h, fresh=True)
        files = task_files(d)
        sel = _json_body_int_list(body.get("files"), "文件")
        if not sel <= {f.idx for f in files if not f.padding}:
            raise MagnetError("文件编号无效")
        added = sel - {i for i, f in enumerate(d["files"]) if f.get("included")}
        if added and body.get("confirm") is not True:
            raise Confirm({"files": len(added), "size": sum(f.length for f in files if f.idx in added)})
        self.api.set_files(h, sel)
        self.forget(h)
        return {"ok": True, "added": len(added)}

    def task_play(self, h: str, body: Dict[str, Any], query: Dict[str, str]) -> Dict[str, Any]:
        fileno = body.get("file")
        f, need = play_plan(self.api, h, int(fileno) + 1 if fileno is not None else None)
        if need and body.get("confirm") is not True:
            raise Confirm({"file": f.path, "size": need})
        player, how, f = play_task(self.api, self.cfg, h, f.num)
        self.forget(h)
        return {"player": player, "how": how, "file": f.path}

    def task_action(self, h: str, action: str, body: Dict[str, Any], query: Dict[str, str]) -> Dict[str, Any]:
        out: Dict[str, Any] = {"ok": True}
        if action == "pause":
            self.api.action(h, "pause")
        elif action == "resume":
            self.api.action(h, "start")
        elif action == "remove":
            _, out["cleaned"] = remove_task(self.api, self.cfg, h)
        elif action == "delete":
            self.api.action(h, "delete")
        self.forget(h)
        return out

    def open(self, body: Dict[str, Any], query: Dict[str, str]) -> Dict[str, Any]:
        reveal = bool(body.get("reveal"))
        if body.get("hash"):
            d = self.details(str(body["hash"]))
            folder = Path(d["output_folder"])
            target = folder
            if body.get("file") is not None:
                idx = int(body["file"])
                files = task_files(d)
                if not 0 <= idx < len(files):
                    raise MagnetError("文件编号无效")
                target = folder / files[idx].path
            if not target.exists():
                target = folder if folder.exists() else Path(self.cfg["download_dir"])
        else:
            p = Path(os.path.expanduser(str(body.get("path") or self.cfg["download_dir"]))).resolve()
            with self.w.lock:
                known = [h.get("folder") for h in self.w.state.get("history", []) if h.get("folder")]
            allowed = [Path(self.cfg["download_dir"]).resolve()] + [Path(k).resolve() for k in known]
            if not any(p == a or a in p.parents for a in allowed):
                raise MagnetError("只能打开下载目录里的文件")
            if not p.exists():
                raise MagnetError("文件夹已经不存在了（可能被移动或删除）")
            target = p
        subprocess.run(["open", "-R", str(target)] if reveal else ["open", str(target)], check=False)
        return {"ok": True}

    # ── 设置 / 诊断 ──
    def config_get(self, body: Dict[str, Any], query: Dict[str, str]) -> Dict[str, Any]:
        cfg = load_config()
        values = {k: cfg.get(k) for k in UI_FIELDS}
        values["download_dir"] = tilde(cfg["download_dir"])
        return {"values": values, "restart_keys": sorted(RESTART_KEYS & set(UI_FIELDS))}

    def config_set(self, body: Dict[str, Any], query: Dict[str, str]) -> Dict[str, Any]:
        raw = read_raw_config()
        changed = []
        for k, v in body.items():
            v = coerce_config(k, v)
            if raw.get(k, DEFAULT_CONFIG.get(k)) != v:
                raw[k] = v
                changed.append(k)
        if changed:
            save_raw_config(raw)
            self.w.reload_config()
            log(f"界面修改设置：{', '.join(changed)}")
        return {"changed": changed, "restart_needed": any(k in RESTART_KEYS for k in changed)}

    def doctor(self, body: Dict[str, Any], query: Dict[str, str]) -> Dict[str, Any]:
        checks = run_checks(self.cfg, self.api, net=query.get("net") == "1")
        return {"checks": [{"level": lv, "text": tx} for lv, tx in checks]}

    def service_restart(self, body: Dict[str, Any], query: Dict[str, str]) -> Dict[str, Any]:
        service_restart((LABEL_RQBIT,))
        ok = wait_api(Api(load_config()), 25)
        self.w.reload_config()
        return {"ok": ok}

    def trackers_update(self, body: Dict[str, Any], query: Dict[str, str]) -> Dict[str, Any]:
        return {"count": update_trackers(self.cfg)}

    def logs(self, body: Dict[str, Any], query: Dict[str, str]) -> Dict[str, Any]:
        name = query.get("name", "rqbit")
        if name not in ("rqbit", "watcher", "app"):
            raise MagnetError("没有这个日志")
        p = LOG_DIR / f"{name}.log"
        lines = p.read_text("utf-8", "replace").splitlines()[-300:] if p.exists() else []
        return {"name": name, "path": tilde(p), "text": "\n".join(lines)}

    def history_remove(self, body: Dict[str, Any], query: Dict[str, str]) -> Dict[str, Any]:
        h = str(body.get("hash") or "")
        with self.w.lock:
            self.w.state["history"] = [x for x in self.w.state.get("history", []) if x["hash"] != h]
            self.w.dirty = True
        return {"ok": True}

    def rqbit_ui(self, body: Dict[str, Any], query: Dict[str, str]) -> Dict[str, Any]:
        url = f"http://{self.cfg['api_host']}:{self.cfg['api_port']}/web/"
        subprocess.run(["pbcopy"], input=self.cfg["password"].encode(), check=False)
        subprocess.run(["open", url], check=False)
        return {"url": url, "username": self.cfg["username"]}


class UIHandler(http.server.BaseHTTPRequestHandler):
    server_version = "MagnetKit"
    STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
              "/index.html": ("index.html", "text/html; charset=utf-8"),
              "/app.js": ("app.js", "application/javascript; charset=utf-8"),
              "/app.css": ("app.css", "text/css; charset=utf-8")}
    CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
           "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")

    def log_message(self, fmt: str, *args: Any) -> None:  # 不写访问日志
        pass

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def _send(self, status: int, payload: bytes, ctype: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, obj: Any) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _handle(self, method: str) -> None:
        srv: UIServer = self.server  # type: ignore[assignment]
        # 只接受本机地址的 Host（防 DNS rebinding）
        if (self.headers.get("Host") or "").lower() not in srv.allowed_hosts:
            return self._json(403, {"error": "forbidden"})
        url = urllib.parse.urlsplit(self.path)
        if not url.path.startswith("/api/"):
            if method != "GET":
                return self._json(405, {"error": "method not allowed"})
            entry = self.STATIC.get(url.path)
            if not entry:
                return self._json(404, {"error": "not found"})
            try:
                data = (UI_DIR / entry[0]).read_bytes()
            except OSError:
                return self._json(404, {"error": "界面文件缺失"})
            extra = {"Content-Security-Policy": self.CSP, "X-Frame-Options": "DENY"} if entry[0] == "index.html" else None
            return self._send(200, data, entry[1], extra)
        if url.path == "/api/ping":
            return self._json(200, {"ok": True, "version": VERSION})
        # 令牌只有 MagnetKit.app 知道（ui.json，权限 600）；网页无法读到它，也就无法冒充界面发请求
        if not secrets.compare_digest((self.headers.get("X-MK-Token") or "").encode(), srv.token.encode()):
            return self._json(401, {"error": "unauthorized"})
        origin = (self.headers.get("Origin") or "").lower()
        if method != "GET" and origin and origin not in srv.allowed_origins:
            return self._json(403, {"error": "forbidden origin"})
        body: Dict[str, Any] = {}
        if method in ("POST", "DELETE"):
            length = int(self.headers.get("Content-Length") or 0)
            if length > 32 * 1024 * 1024:
                return self._json(413, {"error": "请求太大"})
            raw = self.rfile.read(length) if length else b""
            if raw:
                try:
                    body = json.loads(raw)
                except ValueError:
                    return self._json(400, {"error": "请求格式错误"})
                if not isinstance(body, dict):
                    return self._json(400, {"error": "请求格式错误"})
        srv.note_session(self.headers.get("X-MK-Session") or "", self.headers.get("X-MK-Client") or "")
        try:
            result, status = srv.ui.dispatch(method, url.path, body, dict(urllib.parse.parse_qsl(url.query))), 200
        except NotFound as e:
            result, status = {"error": str(e)}, 404
        except Confirm as c:
            result, status = {"confirm": c.info}, 409
        except MagnetError as e:
            result, status = {"error": str(e)}, 400
        except Exception as e:  # noqa: BLE001 - 接口不能因为单个请求出错而断开
            log("界面接口异常：\n" + traceback.format_exc())
            result, status = {"error": f"内部错误：{e}"}, 500
        self._json(status, result)


class UIServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, watcher: "Watcher", port: int, token: str):
        super().__init__(("127.0.0.1", port), UIHandler)
        self.ui = UIApi(watcher)
        self.token = token
        self.port = self.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.allowed_origins = {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}
        self.sessions: Dict[str, float] = {}

    def note_session(self, sid: str, client: str) -> None:
        if sid and sid not in self.sessions:
            self.sessions[sid] = time.time()
            log(f"界面已连接：{ {'app': 'MagnetKit.app', 'browser': '浏览器'}.get(client, '未知客户端') }")


# ───────────────────────────── 后台监控（launchd 常驻） ─────────────────────────────

class Watcher:
    TICK = 3.0

    def __init__(self) -> None:
        self.cfg = load_config()
        self.api = Api(self.cfg)
        self.cfg_at = time.time()
        self.state = read_json(WATCH_STATE_FILE, {})
        self.state.setdefault("torrents", {})
        self.state.setdefault("history", [])
        self.dirty = False
        self.saved_at = 0.0
        self.workers: Dict[str, threading.Thread] = {}
        self.scrapers: Dict[str, threading.Thread] = {}
        self.job_seen: Dict[str, float] = {}
        self.lock = threading.Lock()
        self.maint_at = 0.0
        self.first_pass = True
        self.ui_server: Optional[UIServer] = None
        self.ui_tried = 0.0

    def reload_config(self) -> None:
        with contextlib.suppress(MagnetError):
            self.cfg = load_config()
            self.api = Api(self.cfg)
        self.cfg_at = time.time()

    # 状态文件只有 watcher 写；进度等高频变化最多每分钟落盘一次
    def save(self, force: bool = False) -> None:
        if self.dirty or force or time.time() - self.saved_at > 60:
            write_atomic(WATCH_STATE_FILE, json.dumps(self.state, ensure_ascii=False))
            self.dirty, self.saved_at = False, time.time()

    def start_ui(self) -> None:
        self.ui_tried = time.time()
        port, token = ensure_ui_info(self.cfg)
        try:
            self.ui_server = UIServer(self, port, token)
        except OSError as e:
            log(f"图形界面服务启动失败（端口 {port}）：{e}，一分钟后重试")
            return
        threading.Thread(target=self.ui_server.serve_forever, daemon=True).start()
        log(f"图形界面服务：http://127.0.0.1:{self.ui_server.port}/")

    def run(self) -> None:
        log(f"watcher 启动（MagnetKit {VERSION}）")
        self.start_ui()
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - 常驻进程不能因为单次异常退出
                log("tick 异常：\n" + traceback.format_exc())
            time.sleep(self.TICK)

    def tick(self) -> None:
        now = time.time()
        if now - self.cfg_at > 60:
            self.reload_config()
        if self.ui_server is None and now - self.ui_tried > 60:
            self.start_ui()
        try:
            items = self.api.torrents(stats=True)
        except (ServiceDown, RequestTimeout):
            items = None
        except ApiError as e:
            log(f"读取任务列表失败：{e}")
            items = None
        if items is not None:
            self.handle(items, now)
        self.queue_tick(now)
        if now - self.maint_at > 3600:
            self.maint_at = now
            threading.Thread(target=self.maintenance, daemon=True).start()
        with self.lock:
            self.save()

    def handle(self, items: List[Dict[str, Any]], now: float) -> None:
        present = set()
        for t in items:
            h = t["info_hash"]
            present.add(h)
            s = t.get("stats") or {}
            finished = bool(s.get("finished"))
            with self.lock:
                rec = self.state["torrents"].get(h)
                if rec is None:
                    # watcher 刚启动时看到的已完成任务不再通知；运行中新出现的（添加后几秒就下完）照常通知
                    known = finished and self.first_pass
                    rec = self.state["torrents"][h] = {
                        "added": now, "finished_at": now if known else None, "notified": known,
                        "policy_done": False, "details_at": 0, "files": None, "missing": 0}
                    self.dirty = True
                rec.update(name=t["name"], id=t["id"], folder=t["output_folder"], missing=0,
                           progress=s.get("file_progress"), total=s.get("total_bytes") or 0,
                           uploaded=s.get("uploaded_bytes") or 0)
            if rec["files"] is None or now - rec["details_at"] > 30:
                with contextlib.suppress(MagnetError):
                    files = snapshot_files(self.api.details(h))
                    with self.lock:
                        if files != rec["files"]:
                            rec["files"] = files
                            self.dirty = True
                        rec["details_at"] = now
            state = s.get("state")
            if state == "error":
                if not rec.get("err"):
                    rec["err"] = True
                    self.dirty = True
                    log(f"任务出错：{t['name']}：{s.get('error')}")
                    notify("❌ 下载出错", f"{t['name']}：{s.get('error')}", cfg=self.cfg, view="tasks")
            elif rec.get("err"):
                rec["err"] = False
            if finished and not rec["notified"]:
                rec.update(notified=True, finished_at=now)
                self.dirty = True
                log(f"下载完成：{t['name']}")
                notify("✅ 下载完成", t["name"], subtitle=hsize(self.rec_size(rec)), cfg=self.cfg, view="tasks")
                with self.lock:
                    self.add_history(h, rec, now)
            elif not finished and rec["notified"] and state == "live":
                rec.update(notified=False, policy_done=False)     # 用户追加了文件，重新开始计
                self.dirty = True
            if finished and state == "live" and not rec["policy_done"]:
                self.seed_policy(h, rec, now)
        with self.lock:
            for h in list(self.state["torrents"]):
                if h in present:
                    continue
                rec = self.state["torrents"][h]
                rec["missing"] = rec.get("missing", 0) + 1
                if rec["missing"] >= 2:        # 连续两次都不在列表里，才认为被移除了
                    self.cleanup(h, rec)
                    del self.state["torrents"][h]
                    self.dirty = True
        self.first_pass = False

    @staticmethod
    def rec_size(rec: Dict[str, Any]) -> int:
        files = rec.get("files")
        if files:
            return sum(length for _, length, inc, pad in files if inc and not pad)
        return rec.get("total") or 0

    def add_history(self, h: str, rec: Dict[str, Any], now: float) -> None:
        hist = [x for x in self.state["history"] if x["hash"] != h]
        hist.append({"hash": h, "name": rec["name"], "folder": rec.get("folder"), "size": self.rec_size(rec),
                     "at": now})
        self.state["history"] = hist[-50:]

    def seed_policy(self, h: str, rec: Dict[str, Any], now: float) -> None:
        action = str(self.cfg.get("after_seeding") or "keep")
        if action not in ("pause", "remove"):
            return
        ratio_goal = float(self.cfg.get("seed_ratio") or 0)
        minutes = float(self.cfg.get("seed_minutes", -1))
        total = rec.get("total") or 0
        done_ratio = ratio_goal > 0 and total > 0 and (rec.get("uploaded") or 0) >= ratio_goal * total
        done_time = minutes >= 0 and rec.get("finished_at") is not None and now - rec["finished_at"] >= minutes * 60
        if not (done_ratio or done_time) or stream_active(h):
            return
        try:
            if action == "pause":
                self.api.action(h, "pause")
            else:
                self.api.action(h, "forget")
                self.cleanup(h, rec)
        except MagnetError as e:
            log(f"做种策略执行失败：{rec['name']}：{e}")
            return
        rec["policy_done"] = True
        self.dirty = True
        why = f"分享率达到 {ratio_goal}" if done_ratio else f"做种满 {minutes:g} 分钟"
        log(f"做种结束（{why}）：{rec['name']} → {'暂停' if action == 'pause' else '移出列表'}")

    def cleanup(self, h: str, rec: Dict[str, Any]) -> None:
        if rec.get("cleaned") or not rec.get("files") or not rec.get("folder"):
            return
        n = cleanup_placeholders(rec["folder"], rec["files"], rec.get("progress"), self.cfg["download_dir"])
        rec["cleaned"] = True
        if n:
            log(f"清理了 {n} 个未选文件的残缺文件：{rec['name']}")

    # ── 解析任务：只负责拿到文件列表，拿到后等用户确认，绝不自动下载 ──
    def queue_tick(self, now: float) -> None:
        items = read_jobs()
        expired = False
        for it in items:
            if it["state"] != "resolving":
                expired |= now - it.get("created", now) > JOB_TTL
                continue
            if it.get("next_try", 0) <= now:
                self.start_job(it)
        if expired:
            with locked_queue() as q:
                q["items"] = [x for x in q["items"] if x.get("state", "resolving") == "resolving"
                              or now - x.get("created", now) <= JOB_TTL]
        self.scrape_tick(now, items)

    SCRAPE_EVERY = 180

    def scrape_tick(self, now: float, items: List[Dict[str, Any]]) -> None:
        """给解析中的项目每 3 分钟问一次做种人数；待确认的项目问一次。"""
        ids = {it["id"] for it in items}
        with self.lock:
            for jid in [k for k, th in self.scrapers.items() if k not in ids and not th.is_alive()]:
                del self.scrapers[jid]
        for it in items:
            sw = it.get("swarm") or {}
            due = ((it["state"] == "resolving" and now - sw.get("at", 0) > self.SCRAPE_EVERY)
                   or (it["state"] == "ready" and not sw))
            if not due or not it.get("info_hash"):
                continue
            with self.lock:
                th = self.scrapers.get(it["id"])
                if th and th.is_alive():
                    continue
                th = threading.Thread(target=self.scrape_job, args=(dict(it),), daemon=True)
                self.scrapers[it["id"]] = th
            th.start()

    def scrape_job(self, it: Dict[str, Any]) -> None:
        sw = scrape_swarm(it["info_hash"], job_trackers(it, self.cfg))
        with locked_queue() as q:
            for x in q["items"]:
                if x["id"] == it["id"]:
                    x["swarm"] = sw

    def start_job(self, it: Dict[str, Any]) -> None:
        with self.lock:
            th = self.workers.get(it["id"])
            if th and th.is_alive():
                return
            if sum(1 for x in self.workers.values() if x.is_alive()) >= 6:
                return
            th = threading.Thread(target=self.job_worker, args=(dict(it),), daemon=True)
            self.workers[it["id"]] = th
        th.start()

    def job_worker(self, it: Dict[str, Any]) -> None:
        try:
            src = parse_source(it["source"])
            deadline = it["created"] + float(self.cfg.get("queue_retry_hours") or 24) * 3600
            meta = fetch_torrent(self.api, self.cfg, src, max(30, min(600, deadline - time.time())))
            found = False
            with locked_queue() as q:
                for x in q["items"]:
                    if x["id"] == it["id"]:
                        x.update(state="ready", info_hash=meta.info_hash, name=meta.name, size=meta.total,
                                 files=len(meta.real_files), last_error="", ready_at=time.time())
                        found = True
            if not found:
                return                     # 用户已经放弃了这个任务
            log(f"解析成功，等待确认：{meta.name}")
            if time.time() - self.job_seen.get(it["id"], 0) > 5:   # 界面正在看着它时就不用再发通知
                notify("📋 文件列表已就绪", meta.name, subtitle="打开 MagnetKit 确认要下载的文件", cfg=self.cfg,
                       view="pending")
        except ServiceDown:
            self.job_attempt_failed(it, "下载服务没有运行", count=False)
        except ConnectionLost:
            # 下载服务重启会打断进行中的解析：不算一次失败，稍后自动重新开始
            self.job_attempt_failed(it, "下载服务刚重启，自动重新解析", count=False)
        except MagnetError as e:
            self.job_attempt_failed(it, str(e))
        except Exception as e:  # noqa: BLE001
            log("解析任务异常：\n" + traceback.format_exc())
            self.job_attempt_failed(it, str(e))

    def job_attempt_failed(self, it: Dict[str, Any], why: str, count: bool = True) -> None:
        gave_up = None
        with locked_queue() as q:
            for x in q["items"]:
                if x["id"] != it["id"] or x.get("state", "resolving") != "resolving":
                    continue
                if count:
                    x["tries"] = x.get("tries", 0) + 1
                x["last_error"], x["next_try"] = shorten(why, 120), time.time() + 30
                # 只有真正尝试过（count=True）才会因为超过期限而放弃；服务没运行 / 重启不算
                if count and time.time() - x["created"] > float(self.cfg.get("queue_retry_hours") or 24) * 3600:
                    x["state"] = "failed"
                    gave_up = x
        if gave_up:
            log(f"放弃解析：{gave_up.get('name')}（{why}）")
            notify("❌ 没能拿到文件列表", f"{gave_up.get('name')}：{why}", cfg=self.cfg, view="pending")

    # ── 每小时的维护：tracker 列表、缓存清理 ──
    def maintenance(self) -> None:
        try:
            if not TRACKERS_FILE.exists() or time.time() - TRACKERS_FILE.stat().st_mtime > 86400:
                log(f"tracker 列表已更新：{update_trackers(self.cfg)} 个")
        except MagnetError as e:
            log(str(e))
        keep = {it.get("info_hash") for it in read_jobs()}
        for d, age in ((TORRENT_CACHE, time.time() - 30 * 86400), (STREAM_MARKS, time.time() - 86400)):
            for p in d.glob("*") if d.exists() else []:
                with contextlib.suppress(OSError):
                    if p.stem not in keep and p.stat().st_mtime < age:
                        p.unlink()


def cmd_watch(a: argparse.Namespace) -> int:
    setup_log("watcher.log")
    Watcher().run()
    return 0


# ───────────────────────────── rqbit 守护进程 ─────────────────────────────

def find_rqbit(cfg: Dict[str, Any]) -> Optional[str]:
    for cand in (cfg.get("rqbit_path"), shutil.which("rqbit"), "/opt/homebrew/bin/rqbit", "/usr/local/bin/rqbit"):
        if cand and os.access(cand, os.X_OK):
            return cand
    return None


def cmd_daemon(a: argparse.Namespace) -> int:
    """launchd 调用：按配置拼出 rqbit 参数后 exec（进程直接变成 rqbit，launchd 管理它的生命周期）。"""
    cfg = load_config()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOG_DIR / "rqbit.log"
    with contextlib.suppress(OSError):
        if logfile.stat().st_size > 10 * 1024 * 1024:
            logfile.replace(LOG_DIR / "rqbit.log.1")
    fd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    rq = find_rqbit(cfg)
    if not rq:
        print("找不到 rqbit，可执行 brew install rqbit", flush=True)
        time.sleep(30)
        return 1
    if not cfg.get("password"):
        print("配置里没有密码，拒绝以无认证方式启动（请运行安装脚本）", flush=True)
        time.sleep(30)
        return 1
    Path(cfg["download_dir"]).mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "rqbit").mkdir(parents=True, exist_ok=True)
    argv = [rq, "-v", str(cfg.get("rqbit_log_level") or "warn"),
            "--http-api-listen-addr", f"{cfg['api_host']}:{cfg['api_port']}",
            "--listen-port", str(cfg["listen_port"])]
    if cfg.get("upnp"):
        # UPnP 成功只在 debug 级别记录：单独记一份（每次启动清空），自检据此判断端口映射有没有生效
        UPNP_LOG.write_text("", "utf-8")
        argv += ["--log-file", str(UPNP_LOG), "--log-file-rust-log", "warn,librqbit_upnp=debug"]
    else:
        argv.append("--disable-upnp-port-forward")
    if cfg.get("utp"):
        argv.append("--experimental-enable-utp-listen")
    for key, flag in (("download_limit", "--ratelimit-download"), ("upload_limit", "--ratelimit-upload")):
        if cfg.get(key):
            argv += [flag, str(parse_size(cfg[key]))]
    argv += [str(x) for x in cfg.get("extra_rqbit_args") or []]
    argv += ["server", "start", "--persistence-location", str(STATE_DIR / "rqbit"), cfg["download_dir"]]
    env = dict(os.environ, RQBIT_HTTP_BASIC_AUTH_USERPASS=f"{cfg['username']}:{cfg['password']}", NO_COLOR="1")
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 启动：{' '.join(argv)}", flush=True)
    os.execve(rq, argv, env)
    return 0


# ───────────────────────────── 安装 / 卸载 ─────────────────────────────

def _step(msg: str) -> None:
    print(cyan("▸ ") + msg, flush=True)


def write_agents(python: str) -> None:
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    script = str(APP_DIR / "magnet.py")
    for label, sub, logname in ((LABEL_RQBIT, "_daemon", "rqbit"), (LABEL_WATCHER, "_watch", "watcher")):
        plist = {
            "Label": label,
            "ProgramArguments": [python, script, sub],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "StandardOutPath": str(LOG_DIR / f"{logname}.launchd.log"),
            "StandardErrorPath": str(LOG_DIR / f"{logname}.launchd.log"),
            "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                                     "LANG": "zh_CN.UTF-8", "PYTHONIOENCODING": "utf-8"},
        }
        with open(agent_plist(label), "wb") as f:
            plistlib.dump(plist, f)


def build_icon(helper: Path, out_icns: Path) -> bool:
    with tempfile.TemporaryDirectory() as td:
        png = Path(td) / "icon.png"
        r = subprocess.run(["swift", str(helper), "icon", str(png)], capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not png.exists():
            return False
        iconset = Path(td) / "AppIcon.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = size * scale
                name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
                subprocess.run(["sips", "-z", str(px), str(px), str(png), "--out", str(iconset / name)],
                               capture_output=True)
        r = subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out_icns)], capture_output=True)
        return r.returncode == 0


def app_info_plist() -> Dict[str, Any]:
    return {
        "CFBundleExecutable": "MagnetKit",
        "CFBundleIdentifier": APP_ID,
        "CFBundleName": "MagnetKit",
        "CFBundleDisplayName": "MagnetKit",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "CFBundleIconFile": "AppIcon",
        "LSMinimumSystemVersion": "13.0",
        "LSApplicationCategoryType": "public.app-category.utilities",
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
        "NSHumanReadableCopyright": "基于 rqbit（Apache-2.0）",
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "CFBundleURLTypes": [
            {"CFBundleURLName": "BitTorrent Magnet Link", "CFBundleURLSchemes": ["magnet"]},
            {"CFBundleURLName": "MagnetKit Internal", "CFBundleURLSchemes": ["magnetkit"]},
        ],
        "CFBundleDocumentTypes": [{
            "CFBundleTypeName": "BitTorrent Document", "CFBundleTypeRole": "Viewer",
            "LSHandlerRank": "Owner", "LSItemContentTypes": ["org.bittorrent.torrent"]}],
        "UTImportedTypeDeclarations": [{
            "UTTypeIdentifier": "org.bittorrent.torrent", "UTTypeDescription": "BitTorrent Document",
            "UTTypeConformsTo": ["public.data"],
            "UTTypeTagSpecification": {"public.filename-extension": ["torrent"],
                                       "public.mime-type": ["application/x-bittorrent"]}}],
    }


def build_app() -> None:
    src = APP_DIR / "app" / "main.swift"
    if not src.exists():
        raise MagnetError(f"缺少 App 源码：{src}")
    if not shutil.which("swiftc"):
        raise MagnetError("需要 Xcode 命令行工具来编译 App：xcode-select --install")
    with tempfile.TemporaryDirectory() as td:
        exe = Path(td) / "MagnetKit"
        r = subprocess.run(["swiftc", "-O", "-swift-version", "5", "-o", str(exe), str(src)],
                           capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            raise MagnetError("编译 MagnetKit.app 失败：\n" + (r.stderr or r.stdout)[-3000:])
        subprocess.run(["pkill", "-x", "MagnetKit"], capture_output=True)   # 正在运行的旧版本先退出
        if APP_BUNDLE.exists():
            subprocess.run([LSREGISTER, "-u", str(APP_BUNDLE)], capture_output=True)
            shutil.rmtree(APP_BUNDLE)
        (APP_BUNDLE / "Contents" / "MacOS").mkdir(parents=True)
        (APP_BUNDLE / "Contents" / "Resources").mkdir(parents=True)
        shutil.copy2(exe, APP_BUNDLE / "Contents" / "MacOS" / "MagnetKit")
    with open(APP_BUNDLE / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump(app_info_plist(), f)
    helper = APP_DIR / "macos_helper.swift"
    if helper.exists() and not build_icon(helper, APP_BUNDLE / "Contents" / "Resources" / "AppIcon.icns"):
        print(dim("   （图标生成失败，使用默认图标）"))
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(APP_BUNDLE)], capture_output=True)
    subprocess.run([LSREGISTER, "-f", str(APP_BUNDLE)], capture_output=True)


def set_default_handler() -> str:
    helper = APP_DIR / "macos_helper.swift"
    r = subprocess.run(["swift", str(helper), "set-default", str(APP_BUNDLE)], capture_output=True, text=True,
                       timeout=300)
    return (r.stdout + r.stderr).strip()


def cmd_setup(a: argparse.Namespace) -> int:
    python = a.python or sys.executable
    _step("写入配置")
    raw = read_raw_config()
    for k, v in DEFAULT_CONFIG.items():
        raw.setdefault(k, v)
    if not raw.get("password"):
        raw["password"] = secrets.token_urlsafe(18)
    save_raw_config(raw)
    cfg = load_config()
    ensure_ui_info(cfg)
    print(dim(f"   {tilde(CONFIG_PATH)}（权限 600，含 API 密码）"))

    _step("创建目录")
    for d in (STATE_DIR, TORRENT_CACHE, STREAM_MARKS, LOG_DIR, Path(cfg["download_dir"])):
        d.mkdir(parents=True, exist_ok=True)
    link = HOME / "Downloads" / "Magnet"
    if Path(cfg["download_dir"]) != link and not link.exists() and not link.is_symlink():
        with contextlib.suppress(OSError):
            link.symlink_to(cfg["download_dir"])
            print(dim(f"   ~/Downloads/Magnet → {tilde(cfg['download_dir'])}（快捷方式）"))
    print(dim(f"   下载目录：{tilde(cfg['download_dir'])}"))

    _step("下载公共 tracker 列表")
    try:
        print(dim(f"   {update_trackers(cfg)} 个"))
    except MagnetError as e:
        print(yellow(f"   {e}（不影响使用，之后会自动重试）"))

    _step("注册 launchd 后台服务（登录后自动运行）")
    service_stop()
    write_agents(python)

    _step("编译 MagnetKit.app（图形界面）")
    build_app()
    print(dim(f"   {tilde(APP_BUNDLE)}"))
    _step("设为磁力链接和 .torrent 文件的默认打开方式")
    for line in set_default_handler().splitlines():
        print(dim("   " + line))

    _step("启动服务")
    service_start()
    api = Api(cfg)
    if not wait_api(api, 30):
        print(red("   rqbit 没有按时启动，看看 magnet service logs"))
        return 1
    print(dim(f"   rqbit v{api.ping().get('version')} 已在 http://{cfg['api_host']}:{cfg['api_port']} 运行"))
    port = int(cfg.get("ui_port") or 3031)
    for _ in range(40):
        with contextlib.suppress(OSError, urllib.error.URLError):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2):
                print(dim(f"   图形界面服务已在 http://127.0.0.1:{port} 运行"))
                break
        time.sleep(0.5)
    else:
        print(red("   图形界面服务没有按时启动，看看 magnet service logs"))
        return 1
    return 0


def cmd_uninstall(a: argparse.Namespace) -> int:
    subprocess.run(["pkill", "-x", "MagnetKit"], capture_output=True)
    service_stop()
    for label in LABELS:
        with contextlib.suppress(OSError):
            agent_plist(label).unlink()
    if APP_BUNDLE.exists():
        subprocess.run([LSREGISTER, "-u", str(APP_BUNDLE)], capture_output=True)
        shutil.rmtree(APP_BUNDLE, ignore_errors=True)
    for link in (HOME / "Downloads" / "Magnet", HOME / ".local" / "bin" / "magnet"):
        if link.is_symlink():
            link.unlink()
    if a.purge:
        for d in (STATE_DIR, CONFIG_PATH.parent, LOG_DIR):
            shutil.rmtree(d, ignore_errors=True)
    print("已移除后台服务和 MagnetKit.app。" + ("配置、缓存和日志也已删除。" if a.purge else ""))
    return 0


# ───────────────────────────── 入口 ─────────────────────────────

EPILOG = """\
日常使用请直接打开 MagnetKit（应用程序 → MagnetKit），命令行适合脚本和高级用法：
  magnet info  'magnet:?xt=urn:btih:…'     只看文件列表，不下载
  magnet add   'magnet:?xt=urn:btih:…'     选择文件，确认后下载
  magnet add   'magnet:…' -s video -y       只下视频（-y 表示已确认）
  magnet play  'magnet:…'                   边下边播最大的视频（会先确认）
  magnet ls                                 查看进度

文件选择语法（-s / select / 交互输入，逗号或空格分隔）：
  1,3-5      按编号           video 视频 / audio / sub 字幕 / image / doc / archive / exe
  *.mkv      通配符           S01E0      文件名包含
  !*.txt     排除             rec 推荐（除可执行文件外全部）  all 全部  largest 最大的一个
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="magnet", description="磁力链接 / 种子：先看文件，确认后下载、边下边播（基于 rqbit）",
                                epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter, add_help=False)
    p._optionals.title = "选项"
    p.add_argument("-h", "--help", action="help", help="显示帮助")
    p.add_argument("-V", "--version", action="version", version=f"magnet {VERSION}", help="显示版本")
    sub = p.add_subparsers(title="命令", metavar="<命令>")

    s = sub.add_parser("info", help="解析并列出文件（不下载）")
    s.add_argument("target", help="磁力链接 / info hash / .torrent 文件或网址")
    s.add_argument("-t", "--timeout", type=int, help="等待元数据的秒数（默认 60）")
    s.add_argument("--json", action="store_true", help="输出 JSON")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("add", help="选择文件，确认后下载")
    s.add_argument("target", help="磁力链接 / info hash / .torrent 文件或网址")
    s.add_argument("-s", "--select", help="要下载的文件，如 1,3-5 / video / '*.mkv' / '!*.txt'")
    s.add_argument("-a", "--all", action="store_true", help="选择全部文件")
    s.add_argument("--video", action="store_true", help="只选视频和字幕")
    s.add_argument("--min-size", help="跳过小于此大小的文件，如 50M")
    s.add_argument("-d", "--dir", help="下载到指定目录")
    s.add_argument("-y", "--yes", action="store_true", help="已确认，不再询问（没指定 -s 时按推荐规则选文件）")
    s.add_argument("-p", "--play", action="store_true", help="开始下载后立即边下边播")
    s.add_argument("-q", "--queue", action="store_true", help="元数据超时时转入后台解析（拿到列表后等待确认）")
    s.add_argument("-t", "--timeout", type=int, help="等待元数据的秒数（默认 60）")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("play", help="边下边播（IINA / mpv / VLC）")
    s.add_argument("target", nargs="?", help="任务 ID / 名称，或磁力链接（默认最新任务）")
    s.add_argument("file", nargs="?", type=int, help="文件编号（默认最大的视频）")
    s.add_argument("--url", action="store_true", help="只输出播放地址，不打开播放器")
    s.add_argument("-y", "--yes", action="store_true", help="需要新下载文件时，表示已确认")
    s.add_argument("-t", "--timeout", type=int, help="等待元数据的秒数")
    s.set_defaults(func=cmd_play)

    s = sub.add_parser("open", help="打开下载目录 / 文件")
    s.add_argument("task", nargs="?", help="任务 ID / 名称（省略则打开下载根目录）")
    s.add_argument("file", nargs="?", type=int, help="文件编号")
    s.add_argument("-R", "--reveal", action="store_true", help="在访达中显示，而不是打开")
    s.set_defaults(func=cmd_open)

    s = sub.add_parser("ls", help="任务列表")
    s.add_argument("-q", "--quiet", action="store_true", help="不显示最近完成")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("show", help="查看任务里的文件和各自进度")
    s.add_argument("task", nargs="?", help="任务 ID / 名称（默认最新任务）")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("select", help="修改要下载的文件（新增文件会先确认）")
    s.add_argument("task", help="任务 ID / 名称")
    s.add_argument("spec", nargs="+", help="选择规则，同 add -s")
    s.add_argument("-y", "--yes", action="store_true", help="已确认，不再询问")
    s.set_defaults(func=cmd_select)

    s = sub.add_parser("pause", help="暂停（ID / 名称 / all）")
    s.add_argument("tasks", nargs="+")
    s.set_defaults(func=cmd_pause)

    s = sub.add_parser("resume", help="继续（ID / 名称 / all）")
    s.add_argument("tasks", nargs="+")
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser("rm", help="移除任务（默认保留已下载的文件）")
    s.add_argument("task")
    s.add_argument("--delete", action="store_true", help="同时删除已下载的文件")
    s.add_argument("-y", "--yes", action="store_true", help="不再确认")
    s.set_defaults(func=cmd_rm)

    s = sub.add_parser("queue", help="解析中 / 待确认的项目（clear 清空）")
    s.add_argument("action", nargs="?", choices=["list", "clear"], default="list")
    s.set_defaults(func=cmd_queue)

    s = sub.add_parser("ui", help="打开 MagnetKit 图形界面")
    s.add_argument("--browser", action="store_true", help="在浏览器里打开，而不是 App")
    s.add_argument("--rqbit", action="store_true", help="打开 rqbit 自带的网页界面")
    s.set_defaults(func=cmd_ui)

    s = sub.add_parser("service", help="后台服务：start / stop / restart / status / logs")
    s.add_argument("action", nargs="?", choices=["start", "stop", "restart", "status", "logs"], default="status")
    s.add_argument("-n", "--lines", type=int, default=30, help="logs 显示的行数")
    s.set_defaults(func=cmd_service)

    s = sub.add_parser("trackers", help="查看 / 更新公共 tracker 列表")
    s.add_argument("--update", action="store_true")
    s.set_defaults(func=cmd_trackers)

    s = sub.add_parser("config", help="查看 / 修改配置（config set <键> <值>）")
    s.add_argument("action", nargs="?", choices=["show", "set"], default="show")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("doctor", help="自检（--net 附带联网解析测试）")
    s.add_argument("--net", action="store_true")
    s.set_defaults(func=cmd_doctor)

    # 以下是内部命令（不在帮助里显示）
    sub.add_parser("_daemon").set_defaults(func=cmd_daemon)
    sub.add_parser("_watch").set_defaults(func=cmd_watch)
    s = sub.add_parser("_setup")
    s.add_argument("--python")
    s.set_defaults(func=cmd_setup)
    s = sub.add_parser("_uninstall")
    s.add_argument("--purge", action="store_true")
    s.set_defaults(func=cmd_uninstall)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and not argv[0].startswith("-") and looks_like_source(argv[0]):
        argv.insert(0, "add")                 # magnet 'magnet:?xt=…'  等同于  magnet add …
    parser = build_parser()
    a = parser.parse_args(argv)
    if not hasattr(a, "func"):
        parser.print_help()
        return 0
    try:
        return int(a.func(a) or 0)
    except KeyboardInterrupt:
        eprint("\n已中断")
        return 130
    except MagnetError as e:
        eprint(red("✗ ") + str(e))
        if os.environ.get("MAGNETKIT_DEBUG"):
            traceback.print_exc()
        return 2 if isinstance(e, ServiceDown) else 1


if __name__ == "__main__":
    sys.exit(main())
