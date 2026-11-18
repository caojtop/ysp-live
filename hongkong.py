"""凤凰卫视（香港三台）独立扩展模块。

这个文件只负责凤凰秀相关逻辑：
- 三个频道的元数据（slug / 名称 / live_id / logo / tvg-id）
- Token 读取、登录、自动续期、原子写回
- FLV 签名地址换取与 302 / relay 转发
- 凤凰秀官方 EPG 解析
- /fs/status 与 /diag 所需的状态数据
- 通过 channels() / handle_request() / startup() / epg_day() / diag_lines()
  接入 ysp-live 的通用扩展接口

ysp-live-v9.5.py 不包含任何凤凰/香港三台逻辑；main.py 负责把本模块注册为扩展。
"""

from __future__ import annotations

import datetime
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

# ==========================================
# 频道定义
# 官方直播资源 id 与 akiralereal/iptv 的 extractors/fengshows/channels.js 一致。
# 游客 480p；配置了 Token 后取 720p。播放时实时换取带签名的 FLV 地址。
# ==========================================
CHANNELS = [
    (
        "fsinfo",
        "凤凰资讯",
        "7c96b084-60e1-40a9-89c5-682b994fb680",
        "https://q1.fengshows.com/a/2021_22/79dcc3a9da358a3.png",
        "凤凰资讯",
    ),
    (
        "fschinese",
        "凤凰中文",
        "f7f48462-9b13-485b-8101-7b54716411ec",
        "https://q1.fengshows.com/a/2021_22/ede3d9e09be28e5.png",
        "凤凰中文",
    ),
    (
        "fshongkong",
        "凤凰香港",
        "15e02d92-1698-416c-af2f-3e9a872b4d78",
        "https://q1.fengshows.com/a/2021_23/325d941090bee17.png",
        "凤凰香港",
    ),
]
SLUGS = {channel[0] for channel in CHANNELS}
epg_provider_name = "hongkong"
routes = ["/fs/status", "/<channel>.flv"]

# ==========================================
# 接口与运行参数
# ==========================================
API = "https://api.fengshows.cn"
LOGIN_API = "https://m.fengshows.com/api/v3/mp/user/login"
CLIENT = "app(fs-web,1000000);"
URL_TTL = 20.0
CHECK_INTERVAL = float(os.environ.get("FS_CHECK_INTERVAL", "1800") or 1800)
TOKEN_MAX_AGE = float(os.environ.get("FS_TOKEN_MAX_AGE", "21600") or 21600)

_url_cache = {}
_cache_lock = threading.Lock()
_token_warned = False
_last_token = ""
_play_log = {}
_token_status = "unknown"
_token_written_at = 0.0
_renew_lock = threading.Lock()
_renew_last_err = 0.0
_renew_next_try = 0.0

# 由主程序注入的依赖，避免 hongkong.py 反向 import 主程序造成循环依赖。
_get_meta = None
_log = print
_urlopen = None
_ua = "Mozilla/5.0"
_epg_get = None
_epg_tz = datetime.timezone(datetime.timedelta(hours=8))


class TokenExpired(Exception):
    """官网返回 status=10005，表示 Token 已失效。"""


def configure(*, get_meta, log_fn, urlopen, ua, epg_get=None, epg_tz=None):
    """主程序启动时调用一次，注入频道目录、日志与网络函数。"""
    global _get_meta, _log, _urlopen, _ua, _epg_get, _epg_tz
    _get_meta = get_meta
    _log = log_fn
    _urlopen = urlopen
    _ua = ua
    if epg_get is not None:
        _epg_get = epg_get
    if epg_tz is not None:
        _epg_tz = epg_tz


def _require_configured():
    if _get_meta is None or _urlopen is None:
        raise RuntimeError("hongkong 模块未初始化，请先调用 hongkong.configure()")


# ==========================================
# Token
# ==========================================
def parse_token(raw):
    value = (raw or "").strip()
    if not value:
        return ""
    value = re.sub(r"^\s*Cookie\s*:\s*", "", value, flags=re.I)
    match = re.search(r"(?:^|;)\s*App\.user\.token=([^;]*)", value)
    if match:
        value = match.group(1)
    value = value.strip().strip('"')
    try:
        return urllib.parse.unquote(value)
    except Exception:
        return value


def token_file_path():
    return os.environ.get("FS_TOKEN_FILE") or os.path.join(
        os.environ.get("YSP_DATA_DIR", "/app/data"), "fengshows-token.json"
    )


def token():
    env_token = (os.environ.get("FS_TOKEN") or "").strip()
    if env_token:
        return parse_token(env_token)
    path = token_file_path()
    try:
        with open(path, encoding="utf-8") as file:
            body = file.read()
        try:
            data = json.loads(body)
            raw = data.get("token", "") if isinstance(data, dict) else str(data)
        except ValueError:
            raw = body
        return parse_token(raw)
    except OSError:
        return ""


def write_token(value):
    global _token_written_at
    path = token_file_path()
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as file:
        json.dump({"token": value, "updated_at": int(time.time())}, file, ensure_ascii=False)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _token_written_at = time.time()


def login():
    phone = (os.environ.get("FS_PHONE") or "").strip()
    password = os.environ.get("FS_PASSWORD") or ""
    if not phone or not password:
        raise RuntimeError("未配置 FS_PHONE / FS_PASSWORD")
    area = (os.environ.get("FS_AREA_CODE") or "86").strip() or "86"
    payload = json.dumps(
        {"phone": phone, "code": area, "password": password, "keep_alive": True}
    ).encode("utf-8")
    request = urllib.request.Request(
        LOGIN_API,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "fengshows-client": CLIENT,
        },
    )
    with _urlopen(request, timeout=15) as response:
        body = json.loads(response.read().decode("utf-8", "replace"))
    data = body.get("data")
    value = data.get("token", "") if isinstance(data, dict) else ""
    value = value or body.get("token") or ""
    if not value:
        raise RuntimeError(body.get("message") or f'登录失败 status={body.get("status")}')
    return parse_token(value)


def request_live_url(live_id, value):
    quality = "fhd" if value else "hd"
    url = f"{API}/hub/live/auth-url?live_qa={quality}&live_id={live_id}"
    headers = {"Accept": "application/json", "fengshows-client": CLIENT}
    if value:
        headers["token"] = value
    request = urllib.request.Request(url, headers=headers)
    with _urlopen(request, timeout=10) as response:
        body = json.loads(response.read().decode("utf-8", "replace"))
    status = str(body.get("status"))
    if status == "0":
        return body["data"]["live_url"], quality
    if status == "10005":
        raise TokenExpired()
    raise RuntimeError(f'凤凰秀接口 status={status} {body.get("message", "")}')


def check_token(value):
    """True=720p 可用；False=Token 过期；None=网络/接口异常（不要据此重新登录）。"""
    if not value:
        return False
    url = f"{API}/hub/live/auth-url?live_qa=fhd&live_id={CHANNELS[0][2]}"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "fengshows-client": CLIENT, "token": value},
    )
    try:
        with _urlopen(request, timeout=12) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
    except Exception:
        return None
    status = str(body.get("status"))
    if status == "0":
        return True
    if status == "10005":
        return False
    return None


def token_renew_once(force=False):
    """校验现有 Token，失效或超过 TOKEN_MAX_AGE 就重新登录并写文件。"""
    with _renew_lock:
        current = token()
        if current and not force and (time.time() - _token_written_at) < TOKEN_MAX_AGE:
            state = check_token(current)
            if state is not False:
                return False
        elif current and not force:
            state = check_token(current)
            if state is None:
                return False
        new_token = login()
        if check_token(new_token) is not True:
            raise RuntimeError("新 Token 未通过官网 720p 校验")
        write_token(new_token)
        _log(f"[凤凰卫视] 自动续期成功，新 Token 已写入 {token_file_path()}（720p 校验通过）")
        return True


def note_renew_failure():
    """启动时自动续期失败后，把下一次重试推迟到 1 小时后。"""
    global _renew_last_err, _renew_next_try
    now = time.time()
    _renew_last_err = now
    _renew_next_try = now + 3600


def token_keeper():
    if (os.environ.get("FS_TOKEN") or "").strip():
        _log("[凤凰卫视] 已手动指定 FS_TOKEN（环境变量优先级最高），跳过自动续期")
        return
    if not (os.environ.get("FS_PHONE") or "").strip() or not (os.environ.get("FS_PASSWORD") or ""):
        _log("[凤凰卫视] 未配置 FS_PHONE / FS_PASSWORD，跳过自动续期；只能手动放 Token")
        return
    global _renew_last_err, _renew_next_try
    _log(
        f"[凤凰卫视] 自动续期已启用：每 {int(CHECK_INTERVAL)} 秒校验一次，"
        f"最多 {int(TOKEN_MAX_AGE // 3600)} 小时主动换新"
    )
    while True:
        wait = _renew_next_try - time.time()
        if wait > 0:
            time.sleep(min(CHECK_INTERVAL, wait))
            continue
        try:
            token_renew_once()
            _renew_next_try = 0.0
        except Exception as exc:
            now = time.time()
            _renew_next_try = now + 3600
            if now - _renew_last_err >= 3600:
                _log(f"[凤凰卫视] 自动续期失败: {exc}（1 小时内不再重试）")
                _renew_last_err = now
        time.sleep(CHECK_INTERVAL)


# ==========================================
# 画质、取流与日志
# ==========================================
def quality_label(quality):
    # 凤凰秀接口档位名是 fhd，实际分辨率 1280x720；hd 是 854x480。
    return "720p" if quality == "fhd" else "480p"


def note_for(tag):
    if tag == "token":
        return "Token 已生效"
    if tag == "expired":
        return "Token 已过期，已自动降级"
    return "游客档，未配置 Token"


def token_summary():
    if not token():
        return "未配置，游客 480p"
    if _token_status == "expired":
        return "已配置，但官网已判定过期（当前 480p）"
    if _token_status == "token":
        return "已配置，720p 生效"
    return "已配置（尚未校验）"


def live_url(slug):
    """返回 (带签名的 FLV 地址, 档位 fhd/hd, 来源 token/guest/expired)。"""
    _require_configured()
    global _token_warned, _last_token, _token_status
    now = time.time()
    current = token()
    # Token 文件被续期器改写后，下一轮立刻用新 Token，无需重启。
    if current != _last_token:
        if _last_token and current:
            _log("[凤凰卫视] 检测到 Token 变更，已清除签名缓存，下一次换签使用新 Token")
        _last_token = current
        _token_warned = False
        with _cache_lock:
            _url_cache.clear()
    key = (slug, bool(current))
    with _cache_lock:
        cached = _url_cache.get(key)
    if cached and now - cached[3] < URL_TTL:
        return cached[0], cached[1], cached[2]
    meta = _get_meta(slug)
    if not meta or meta.get("kind") != "flv":
        raise RuntimeError("未知频道")
    tag = "token" if current else "guest"
    try:
        url, quality = request_live_url(meta["live_id"], current)
    except TokenExpired:
        # Token 过期自动降级为游客 480p，频道不中断。
        if not _token_warned:
            _log("[凤凰卫视] Token 已被官网判定过期，三台自动改用游客 480p；请更新 Token")
            _token_warned = True
        url, quality = request_live_url(meta["live_id"], "")
        tag = "expired"
    with _cache_lock:
        _url_cache[key] = (url, quality, tag, time.time())
    _token_status = tag
    return url, quality, tag


def log_play(slug, quality, tag):
    """docker logs 里能看到当前是 480p 还是 720p；同档位 5 分钟内不重复刷。"""
    now = time.time()
    previous = _play_log.get(slug)
    if previous and previous[0] == quality and previous[1] == tag and now - previous[2] < 300:
        return
    _play_log[slug] = (quality, tag, now)
    meta = _get_meta(slug) or {}
    _log(f'[凤凰卫视] {meta.get("name", slug)} 当前画质={quality_label(quality)}（{note_for(tag)}）')


def startup_report():
    if not token():
        _log("[凤凰卫视] 未配置 Token，凤凰资讯 / 凤凰中文 / 凤凰香港 均为游客 480p")
        return
    for slug, name, live_id, logo, tvg_id in CHANNELS:
        try:
            _url, quality, tag = live_url(slug)
            _log(f"[凤凰卫视] {name} 当前画质={quality_label(quality)}（{note_for(tag)}）")
        except Exception as exc:
            _log(f"[凤凰卫视] {name} 取流检查失败: {exc}")


def final_url(url):
    """跟随上游 301，拿到最终 CDN 地址，播放器只需要跟一次跳转。
    用 GET 而不是 HEAD：凤凰的上游对 HEAD 会跳到下载页，GET 才是正常 FLV。"""
    request = urllib.request.Request(url, headers={"User-Agent": _ua, "Accept": "*/*"})
    with _urlopen(request, timeout=10) as response:
        return response.geturl()


def stream_to(handler, url, head_only):
    request = urllib.request.Request(url, headers={"User-Agent": _ua, "Accept": "*/*"})
    response = _urlopen(request, timeout=20)
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "video/x-flv")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Cache-Control", "no-cache, no-store, max-age=0")
        handler.send_header("Connection", "close")
        handler.end_headers()
        if head_only:
            return
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            handler.wfile.write(chunk)
    finally:
        try:
            response.close()
        except Exception:
            pass


# ==========================================
# 凤凰秀 EPG
# ==========================================
def _default_epg_get(url, headers, timeout=10):
    request = urllib.request.Request(url, headers=headers)
    with _urlopen(request, timeout=timeout) as response:
        return response.read()


def epg_day(live_id, day):
    getter = _epg_get or _default_epg_get
    url = (
        f"https://api.fengshows.cn/live/{live_id}/resources?dir=asc&date={day}"
        "&page=1&page_size=200"
    )
    raw = getter(url, {"Accept": "application/json", "fengshows-client": CLIENT}, timeout=10)
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise RuntimeError("凤凰秀节目单不是 JSON")
    if not isinstance(payload, list):
        raise RuntimeError("凤凰秀节目单格式异常")
    day_start = datetime.datetime(int(day[:4]), int(day[4:6]), int(day[6:8]), tzinfo=_epg_tz)
    start_ms = day_start.timestamp() * 1000
    end_ms = start_ms + 86400000
    items = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        if row.get("available") == 0:
            continue
        row_type = row.get("type")
        if row_type is not None and row_type != "schedule":
            continue
        title = str(row.get("title") or "").strip()
        event_time = str(row.get("event_time") or "").strip()
        if not title or not event_time:
            continue
        try:
            dt = datetime.datetime.fromisoformat(event_time.replace("Z", "+00:00"))
        except ValueError:
            continue
        start = dt.timestamp() * 1000
        if start < start_ms or start >= end_ms:
            continue
        items.append({"title": title, "start": start})
    items.sort(key=lambda item: item["start"])
    unique = []
    for item in items:
        if unique and item["start"] == unique[-1]["start"]:
            continue
        unique.append(item)
    out = []
    for index, item in enumerate(unique):
        stop = unique[index + 1]["start"] if index + 1 < len(unique) else end_ms
        if stop > item["start"]:
            out.append({"title": item["title"], "start": item["start"], "stop": stop})
    return out


# ==========================================
# 状态与诊断
# ==========================================
def status_rows(probe=False):
    token_on = bool(token())
    rows = []
    for slug, name, live_id, logo, tvg_id in CHANNELS:
        record = _play_log.get(slug)
        row = {
            "slug": slug,
            "name": name,
            "token_configured": token_on,
            "token_status": token_summary(),
        }
        if record:
            row["quality"] = record[0]
            row["quality_label"] = quality_label(record[0])
            row["source"] = record[1]
            row["checked_ago_sec"] = int(time.time() - record[2])
        if probe:
            try:
                _url, quality, tag = live_url(slug)
                row["quality"] = quality
                row["quality_label"] = quality_label(quality)
                row["source"] = tag
                row["checked_ago_sec"] = 0
            except Exception as exc:
                row["error"] = str(exc)
        rows.append(row)
    return rows


def diag_lines(now=None):
    now = time.time() if now is None else now
    lines = ["--- 凤凰卫视（FLV）---", f"Token：{token_summary()}"]
    for slug, name, live_id, logo, tvg_id in CHANNELS:
        record = _play_log.get(slug)
        if record:
            seen = f"{int(now - record[2])}s前"
            lines.append(f"{name}：{quality_label(record[0])}（{note_for(record[1])}，{seen}）")
        else:
            lines.append(f"{name}：尚未播放")
    return lines


# ==========================================
# ysp-live 通用扩展接口
# 核心程序只认这些约定，不认识凤凰/香港业务。
# ==========================================
def channels():
    """返回要注入核心频道目录的频道元数据。"""
    out = []
    for slug, name, live_id, logo, tvg_id in CHANNELS:
        out.append({
            "slug": slug,
            "name": name,
            "sid": "",
            "pid": "",
            "defn": "fhd",
            "live_id": live_id,
            "logo": logo,
            "group": "香港",
            "tvg_id": tvg_id,
            "kind": "flv",
            "ext": ".flv",
            "epg_provider": epg_provider_name,
            "epg_key": live_id,
        })
    return out


def startup():
    """核心启动时调用：拉起 Token 自动续期和启动画质报告线程。"""
    if (os.environ.get("FS_PHONE") or "").strip() and (os.environ.get("FS_PASSWORD") or "").strip():
        try:
            token_renew_once()
        except Exception as exc:
            _log(f"[凤凰卫视] 启动时自动获取 Token 失败: {exc}")
            note_renew_failure()
    threading.Thread(target=startup_report, daemon=True, name="fs-report").start()
    threading.Thread(target=token_keeper, daemon=True, name="fs-renew").start()


def handle_request(handler, path, parts, head_only):
    """核心请求分发钩子：处理 /fs/status 与 /<slug>.flv，其余返回 False。"""
    if path == "/fs/status":
        qs = urllib.parse.parse_qs(parts.query)
        probe = qs.get("probe", ["0"])[0].lower() in ("1", "true", "yes")
        handler._send(
            200,
            json.dumps({"channels": status_rows(probe)}, ensure_ascii=False, indent=2),
            "application/json; charset=utf-8",
            head_only=head_only,
        )
        return True

    match = re.match(r"^/([\w-]+)\.flv$", path)
    if not match:
        return False

    slug = match.group(1).lower()
    if slug not in SLUGS:
        handler._send(404, "未知频道\n", head_only=head_only)
        return True

    qs = urllib.parse.parse_qs(parts.query)
    relay = qs.get("relay", ["0"])[0].lower() in ("1", "true", "yes")
    try:
        upstream, quality, tag = live_url(slug)
        log_play(slug, quality, tag)
        if relay:
            stream_to(handler, upstream, head_only)
        else:
            handler._send(
                302,
                "",
                head_only=head_only,
                extra_headers={"Location": final_url(upstream)},
            )
    except Exception as exc:
        handler._send(502, f"凤凰卫视取流失败: {exc}\n", head_only=head_only)
    return True
