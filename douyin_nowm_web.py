#!/usr/bin/env python3
"""
抖音 / 小红书 去水印原视频下载工具 - Web 版
启动后浏览器访问 http://localhost:8848 即可使用

用法：
    python douyin_nowm_web.py
    python douyin_nowm_web.py --port 9000
    python douyin_nowm_web.py --host 0.0.0.0  # 局域网共享
"""

import argparse
import io
import json
import os
import re
import shutil
import ssl
import tempfile
import zipfile
import urllib.request
import urllib.parse
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

# TikTok 下载需要 curl_cffi 模拟 Chrome TLS 指纹，绕过 Akamai 反爬
try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

# YouTube 下载需要 yt-dlp；ffmpeg 用 imageio-ffmpeg 提供的静态二进制（Render 无系统 ffmpeg）
try:
    import yt_dlp
    try:
        import imageio_ffmpeg
        FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        FFMPEG_EXE = None
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False
    FFMPEG_EXE = None

# ── 常量 ──────────────────────────────────────────────

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.6 Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
DOUYIN_REFERER = "https://www.douyin.com/"
XHS_REFERER = "https://www.xiaohongshu.com/"
PLAY_API = "https://aweme.snssdk.com/aweme/v1/play/"

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


# ── 分享文本提取 ──────────────────────────────────────

def extract_url_from_text(text: str) -> str:
    """从分享文本中提取 URL。
    
    用户可以直接粘贴抖音/小红书的完整分享文本，例如：
    "2.30 kCu:/ ... https://v.douyin.com/wsms3gTmpjo/ 复制此链接..."
    会自动提取其中的 URL。
    """
    text = text.strip()
    # 如果输入本身就是URL（以 http 开头且不含多余文本），直接返回
    if text.startswith("http") and " " not in text.split("\n")[0]:
        return text
    # 用正则提取所有 URL
    urls = re.findall(r'https?://[^\s<>"\']+', text)
    if not urls:
        # 尝试匹配不带协议的域名 URL
        urls = re.findall(r'(?:v\.douyin\.com|xhslink\.com|www\.xiaohongshu\.com|www\.douyin\.com|www\.iesdouyin\.com)/[^\s<>"\']+', text)
        if urls:
            return "https://" + urls[0]
        raise ValueError("未在输入文本中找到有效的链接")
    # 优先返回抖音/小红书相关的 URL
    for u in urls:
        lower = u.lower()
        if any(k in lower for k in ["douyin.com", "iesdouyin.com", "xiaohongshu.com", "xhslink.com", "xhs.cn"]):
            return u.rstrip("/")
    # 如果没有匹配到平台 URL，返回第一个 URL
    return urls[0].rstrip("/")


# ── 平台检测 ──────────────────────────────────────────

def detect_platform(url: str) -> str:
    """根据 URL 判断平台：douyin / xhs"""
    lower = url.lower()
    if any(k in lower for k in ["douyin.com", "iesdouyin.com", "v.douyin.com"]):
        return "douyin"
    if any(k in lower for k in ["xiaohongshu.com", "xhslink.com", "xhs.cn"]):
        return "xhs"
    raise ValueError("无法识别链接平台，目前支持抖音和小红书")


# ── 抖音核心逻辑 ──────────────────────────────────────

def dy_resolve_share_url(share_url: str) -> tuple[str, str]:
    """解析分享链接，返回 (最终URL, 视频ID)。"""
    if "v.douyin.com" in share_url:
        req = urllib.request.Request(share_url, headers={"User-Agent": MOBILE_UA})
        resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=15)
        final_url = resp.geturl()
        vid = _dy_extract_video_id(final_url)
        return final_url, vid
    vid = _dy_extract_video_id(share_url)
    if vid:
        if "iesdouyin.com" not in share_url and "douyin.com" not in share_url:
            return f"https://www.iesdouyin.com/share/video/{vid}/", vid
        return share_url, vid
    req = urllib.request.Request(share_url, headers={"User-Agent": MOBILE_UA})
    resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=15)
    final_url = resp.geturl()
    vid = _dy_extract_video_id(final_url)
    return final_url, vid


def _dy_extract_video_id(url: str) -> str:
    """从 URL 中提取视频 ID"""
    match = re.search(r"/video/(\d+)", url)
    if match:
        return match.group(1)
    match = re.search(r"/share/video/(\d+)", url)
    if match:
        return match.group(1)
    match = re.search(r"modal_id=(\d{15,})", url)
    if match:
        return match.group(1)
    match = re.search(r"/(\d{15,})", url)
    if match:
        return match.group(1)
    return ""


def dy_fetch_via_api(video_id: str) -> dict:
    """通过抖音专用 API 获取视频/图集详情（推荐方式）。

    接口: https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/?aweme_ids=[ID]
    返回: aweme_details 列表，包含完整视频/图集数据。
    图文笔记需要额外加 request_source=200 参数。
    """
    api_urls = [
        f"https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/?aweme_ids=%5B{video_id}%5D",
        f"https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/?aweme_ids=%5B{video_id}%5D&request_source=200",
    ]
    headers = {
        "User-Agent": MOBILE_UA,
        "Referer": DOUYIN_REFERER,
    }
    for api_url in api_urls:
        try:
            req = urllib.request.Request(api_url, headers=headers)
            resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=15)
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if data and data.get("aweme_details"):
                return data
        except Exception:
            continue
    raise ValueError("抖音 API 未返回有效数据")


def dy_extract_from_api(api_data: dict) -> dict:
    """从 API 返回的 aweme_details 中提取视频信息。"""
    details = api_data.get("aweme_details", [])
    if not details:
        raise ValueError("API 返回的 aweme_details 为空")
    item = details[0]

    video = item.get("video", {})
    play_addr = video.get("play_addr", {})
    uri = play_addr.get("uri", "")

    # 判断是否为图文（有 images 字段且非空）
    is_note = bool(item.get("images") and isinstance(item["images"], list) and len(item["images"]) > 0)

    # 提取图片列表（图文模式）
    images = []
    if is_note:
        for img in item["images"]:
            url_list = img.get("url_list", [])
            img_url = ""
            # 优先取非 webp 格式
            for u in url_list:
                if u and not u.endswith(".webp"):
                    img_url = u
                    break
            if not img_url and url_list:
                img_url = url_list[0]
            if img_url:
                images.append({"url": img_url})

    create_ts = item.get("create_time", 0)
    try:
        pub_date = datetime.fromtimestamp(create_ts).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        pub_date = ""

    result = {
        "aweme_id": item.get("aweme_id", ""),
        "desc": item.get("desc", ""),
        "nickname": item.get("author", {}).get("nickname", ""),
        "video_uri": uri,
        "width": video.get("width", 0),
        "height": video.get("height", 0),
        "duration_ms": video.get("duration", 0),
        "pub_date": pub_date,
        "digg_count": item.get("statistics", {}).get("digg_count", 0),
        "comment_count": item.get("statistics", {}).get("comment_count", 0),
        "share_count": item.get("statistics", {}).get("share_count", 0),
        "cover_url": "",
        "is_note": is_note,
        "note_images": images,
    }

    # 封面
    cover = video.get("cover", {})
    if isinstance(cover, dict):
        cl = cover.get("url_list", [])
        if cl:
            result["cover_url"] = cl[0]

    # 如果是图文，尝试从 play_addr 的 url_list 取无水印直链
    if is_note:
        url_list = play_addr.get("url_list", [])
        if url_list:
            result["download_url"] = url_list[0].replace("/playwm/", "/play/")
    else:
        # 视频：用 play_api 构建下载链接
        if uri:
            result["download_url"] = dy_build_download_url(uri)

    return result


def dy_fetch_page_html(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": MOBILE_UA,
        "Referer": DOUYIN_REFERER,
        "Accept": "text/html,application/xhtml+xml",
    })
    resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=20)
    return resp.read().decode("utf-8", errors="replace")


def dy_parse_router_data(html: str) -> dict:
    match = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>", html, re.DOTALL)
    if not match:
        raise ValueError("抖音页面中未找到 _ROUTER_DATA")
    raw = match.group(1)
    decoded = raw.replace("\\u002F", "/").replace("\\u0026", "&") \
                  .replace("\\u003C", "<").replace("\\u003E", ">")
    return json.loads(decoded)


def dy_extract_video_info(router_data: dict) -> dict:
    loader = router_data.get("loaderData", {})
    for key, val in loader.items():
        if not isinstance(val, dict):
            continue
        vir = val.get("videoInfoRes")
        if not vir:
            continue
        items = vir.get("item_list", [])
        if not items:
            continue
        item = items[0]
        video = item.get("video", {})
        play_addr = video.get("play_addr", {})
        uri = play_addr.get("uri", "")
        if not uri:
            raise ValueError("未能提取抖音 video uri")

        create_ts = item.get("create_time", 0)
        try:
            pub_date = datetime.fromtimestamp(create_ts).strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            pub_date = ""

        return {
            "aweme_id": item.get("aweme_id", ""),
            "desc": item.get("desc", ""),
            "nickname": item.get("author", {}).get("nickname", ""),
            "video_uri": uri,
            "width": video.get("width", 0),
            "height": video.get("height", 0),
            "duration_ms": video.get("duration", 0),
            "pub_date": pub_date,
            "digg_count": item.get("statistics", {}).get("digg_count", 0),
            "comment_count": item.get("statistics", {}).get("comment_count", 0),
            "share_count": item.get("statistics", {}).get("share_count", 0),
            "cover_url": video.get("cover", {}).get("url_list", [""])[0],
        }
    raise ValueError("未找到抖音视频信息")


def dy_build_download_url(uri: str, quality: str = "default") -> str:
    params = urllib.parse.urlencode({"video_id": uri, "ratio": quality, "line": 0})
    return f"{PLAY_API}?{params}"


def dy_probe_url(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": MOBILE_UA, "Referer": DOUYIN_REFERER}, method="HEAD")
    try:
        resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=15)
        return {"status": resp.status, "content_type": resp.headers.get("Content-Type", ""),
                "content_length": int(resp.headers.get("Content-Length", 0))}
    except Exception:
        req = urllib.request.Request(url, headers={"User-Agent": MOBILE_UA, "Referer": DOUYIN_REFERER})
        resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=15)
        total = int(resp.headers.get("Content-Length", 0))
        resp.read(1)
        return {"status": resp.status, "content_type": resp.headers.get("Content-Type", ""),
                "content_length": total}


def probe_browser_direct(url: str) -> tuple:
    """模拟「浏览器点击直连」的请求：桌面 UA + 不发送 Referer。

    返回 (是否可用, Content-Length, Content-Type)。
    抖音 CDN 对带外部 Referer 的请求会返回 403，浏览器直连能否成功取决于此，
    因此这里用与浏览器一致的头去探测，前端据此决定主按钮走直连还是中转。
    """
    headers = {
        "User-Agent": DESKTOP_UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        # 关键：不带 Referer，模拟地址栏直接访问
    }
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, headers=headers, method=method)
            resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=15)
            ok = 200 <= resp.status < 400
            length = int(resp.headers.get("Content-Length", 0) or 0)
            ctype = resp.headers.get("Content-Type", "")
            if method == "GET":
                resp.read(1)
            if ok and (length or ctype):
                return True, length, ctype
            if ok:
                return True, length, ctype
        except Exception:
            continue
    return False, 0, ""


def resolve_final_url(url: str, timeout: int = 15) -> str:
    """把重定向端点（如 aweme.snssdk.com/aweme/v1/play/）解析成最终 CDN 直链。

    浏览器用 fetch/xhr 跨域下载时，要求重定向链上「每一跳」都带 CORS 头。
    snssdk 的 play 端点本身不带 access-control-allow-origin，浏览器会在第一跳就报
    CORS 错误；而最终的 douyinvod CDN 直链带 `access-control-allow-origin: *`。
    因此这里先在服务端跟随 302 拿到最终直链，前端再去跨域拉流。
    """
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": DESKTOP_UA,
            "Accept": "*/*",
            "Range": "bytes=0-0",
        })
        resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=timeout)
        final = resp.geturl() or url
        try:
            resp.close()
        except Exception:
            pass
        return final
    except Exception:
        return url


def process_douyin(share_url: str, quality: str = "default") -> dict:
    # Step 1: 解析链接，获取视频 ID
    page_url, video_id = dy_resolve_share_url(share_url)
    if not video_id:
        raise ValueError("无法从链接中提取抖音视频 ID")

    info = None

    # Step 2: 优先通过专用 API 获取（推荐，不依赖页面 HTML 结构）
    try:
        api_data = dy_fetch_via_api(video_id)
        info = dy_extract_from_api(api_data)
    except Exception as api_err:
        api_error = str(api_err)
        # Step 3: API 失败时降级到旧版 HTML 解析
        try:
            html = dy_fetch_page_html(page_url)
            router = dy_parse_router_data(html)
            old_info = dy_extract_video_info(router)
            # 转换为统一格式
            dl_url = dy_build_download_url(old_info["video_uri"], quality)
            probe = dy_probe_url(dl_url)
            info = {
                "aweme_id": old_info["aweme_id"],
                "desc": old_info["desc"],
                "nickname": old_info["nickname"],
                "video_uri": old_info["video_uri"],
                "width": old_info["width"],
                "height": old_info["height"],
                "duration_ms": old_info["duration_ms"],
                "pub_date": old_info["pub_date"],
                "digg_count": old_info["digg_count"],
                "comment_count": old_info["comment_count"],
                "share_count": old_info["share_count"],
                "cover_url": old_info["cover_url"],
                "is_note": False,
                "note_images": [],
                "download_url": dl_url,
            }
        except Exception:
            raise ValueError(f"抖音解析失败（API: {api_error}，HTML 解析也不可用）")

    # Step 4: 构建返回结果
    is_note = info.get("is_note", False)

    if is_note:
        # 图文模式
        images = info.get("note_images", [])
        dl_url = info.get("download_url", "")
        return {
            "platform": "douyin",
            "ok": True,
            "type": "note",
            "video_id": info["aweme_id"],
            "author": info["nickname"],
            "desc": info["desc"],
            "pub_date": info["pub_date"],
            "images": images,
            "image_count": len(images),
            "digg_count": info["digg_count"],
            "cover_url": info.get("cover_url", ""),
        }

    # 视频模式
    if not info.get("download_url"):
        dl_url = dy_build_download_url(info["video_uri"], quality)
    else:
        dl_url = info["download_url"]
    # 关键：把 snssdk 重定向端点换成最终 CDN 直链，浏览器才能跨域直连下载
    dl_url = resolve_final_url(dl_url)
    probe = dy_probe_url(dl_url)
    direct_ok, direct_len, direct_ctype = probe_browser_direct(dl_url)
    return {
        "platform": "douyin",
        "ok": True,
        "type": "video",
        "video_id": info["aweme_id"],
        "author": info["nickname"],
        "desc": info["desc"],
        "resolution": f"{info['width']}x{info['height']}",
        "duration": format_duration(info["duration_ms"]),
        "digg_count": info["digg_count"],
        "comment_count": info["comment_count"],
        "share_count": info["share_count"],
        "cover_url": info["cover_url"],
        "pub_date": info["pub_date"],
        "download_url": dl_url,
        "file_size": probe["content_length"] or direct_len,
        "file_size_human": format_size(probe["content_length"] or direct_len),
        "content_type": probe["content_type"] or direct_ctype,
        "direct_ok": direct_ok,
    }


# ── 小红书核心逻辑 ────────────────────────────────────

def xhs_resolve_share_url(share_url: str) -> str:
    """解析小红书短链接，返回最终页面 URL（保留 xsec_token 等查询参数）"""
    if "xhslink.com" in share_url or "xhs.cn" in share_url:
        req = urllib.request.Request(share_url, headers={
            "User-Agent": MOBILE_UA,
            "Accept": "text/html",
        })
        resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=15)
        return resp.geturl()
    return share_url


def xhs_extract_note_id(url: str) -> str:
    """从 URL 中提取小红书笔记 ID"""
    match = re.search(r"/explore/([a-f0-9]+)", url)
    if match:
        return match.group(1)
    match = re.search(r"/discovery/item/([a-f0-9]+)", url)
    if match:
        return match.group(1)
    match = re.search(r"xhslink\.com/([A-Za-z0-9]+)", url)
    if match:
        return match.group(1)
    raise ValueError("无法从小红书链接提取笔记 ID")


def xhs_fetch_page_html(url: str, mobile: bool = True) -> str:
    """抓取小红书笔记页。

    2026 起 PC 端 SSR 对未登录请求只返回空壳（无 noteDetailMap），
    而移动端 H5 分享页仍会内联完整笔记数据（noteData.data.noteData），
    所以默认走移动 UA。
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": MOBILE_UA if mobile else DESKTOP_UA,
        "Referer": XHS_REFERER,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=20)
    return resp.read().decode("utf-8", errors="replace")


def xhs_parse_initial_state(html: str) -> dict:
    """解析小红书页面的 __INITIAL_STATE__"""
    match = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", html, re.DOTALL)
    if not match:
        raise ValueError("小红书页面中未找到 __INITIAL_STATE__")
    raw = match.group(1)
    decoded = raw.replace("\\u002F", "/").replace("\\u0026", "&") \
                  .replace("\\u003C", "<").replace("\\u003E", ">")
    # 小红书 JS 对象含 undefined，替换为 null 以兼容 JSON
    decoded = re.sub(r"\bundefined\b", "null", decoded)
    return json.loads(decoded)


def xhs_extract_note_info(initial_state: dict, note_id: str) -> dict:
    """从 __INITIAL_STATE__ 中提取笔记信息（视频或图文）。

    兼容两种页面结构：
      A. PC SSR：state["note"]["noteDetailMap"][note_id]["note"]
      B. H5 分享页：state["noteData"]["data"]["noteData"]（直接是 note 对象）
    """
    note = {}

    note_map = initial_state.get("note", {}).get("noteDetailMap", {})
    if note_map:
        entry = note_map.get(note_id) or next(iter(note_map.values()))
        if isinstance(entry, dict):
            note = entry.get("note", {}) or {}

    if not note:
        h5 = (initial_state.get("noteData") or {}).get("data") or {}
        cand = h5.get("noteData") or {}
        if isinstance(cand, dict) and cand:
            note = cand
            note_id = note.get("noteId") or note_id

    if not note:
        raise ValueError("小红书页面中未找到笔记数据")

    note_type = note.get("type", "normal")  # "video" 或 "normal"（图文）

    # 发布时间
    time_ms = note.get("time", 0) or note.get("lastUpdateTime", 0)
    pub_date = ""
    if time_ms:
        try:
            pub_date = datetime.fromtimestamp(time_ms // 1000).strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            pass

    # 交互数据
    interact = note.get("interactInfo", {})

    def safe_int(val):
        try:
            return int(val)
        except (TypeError, ValueError):
            return 0

    user = note.get("user") or {}
    # H5 结构里字段名是 nickName
    nickname = user.get("nickname") or user.get("nickName") or ""

    base = {
        "note_id": note_id,
        "note_type": note_type,
        "title": note.get("title", ""),
        "desc": note.get("desc", ""),
        "nickname": nickname,
        "pub_date": pub_date,
        "liked_count": safe_int(interact.get("likedCount", 0)),
        "comment_count": safe_int(interact.get("commentCount", 0)),
        "share_count": safe_int(interact.get("shareCount", 0)),
        "collected_count": safe_int(interact.get("collectedCount", 0)),
    }

    if note_type == "video":
        video = note.get("video", {})
        media = video.get("media", {})
        stream = media.get("stream", {})

        best_stream = None
        best_codec = ""
        for codec in ["h264", "h265", "h266", "av1"]:
            streams = stream.get(codec, [])
            if streams:
                best_stream = streams[0]
                best_codec = codec
                break

        if not best_stream:
            raise ValueError("未找到小红书视频流")

        master_url = best_stream.get("masterUrl", "")
        backup_urls = best_stream.get("backupUrls", [])
        download_url = master_url or (backup_urls[0] if backup_urls else "")
        if not download_url:
            raise ValueError("未找到小红书视频下载地址")
        # 统一转 HTTPS
        download_url = download_url.replace("http://", "https://")

        base.update({
            "download_url": download_url,
            "backup_urls": backup_urls,
            "width": best_stream.get("width", 0),
            "height": best_stream.get("height", 0),
            "duration_ms": best_stream.get("duration", 0),
            "fps": best_stream.get("fps", 0),
            "bitrate": best_stream.get("avgBitrate", 0),
            "codec": best_codec,
        })

        image_list = note.get("imageList", [])
        if image_list:
            cover = image_list[0].get("urlDefault", "") or image_list[0].get("url", "")
            base["cover_url"] = cover.replace("http://", "https://") if cover else ""
        else:
            base["cover_url"] = ""
    else:
        # 图文笔记：提取所有图片
        image_list = note.get("imageList", [])
        images = []
        for img in image_list:
            img_url = img.get("urlDefault", "") or img.get("url", "")
            if img_url:
                # 统一转 HTTPS，避免混合内容问题
                img_url = img_url.replace("http://", "https://")
                images.append({
                    "url": img_url,
                    "width": img.get("width", 0),
                    "height": img.get("height", 0),
                })
        if not images:
            raise ValueError("该图文笔记未找到图片")
        base["images"] = images
        base["cover_url"] = images[0]["url"] if images else ""

    return base


def xhs_probe_url(url: str) -> dict:
    """探测小红书资源 URL（图片 HEAD 不支持，用 GET 首字节）"""
    # 确保 URL 使用 HTTPS（避免混合内容问题）
    if url.startswith("http://"):
        url = "https://" + url[7:]
    headers = {"User-Agent": DESKTOP_UA, "Referer": XHS_REFERER}
    req = urllib.request.Request(url, headers=headers, method="HEAD")
    try:
        resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=15)
        return {"status": resp.status, "content_type": resp.headers.get("Content-Type", ""),
                "content_length": int(resp.headers.get("Content-Length", 0))}
    except Exception:
        pass
    try:
        # HEAD 不支持时降级为 GET（读取首字节后关闭）
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=15)
        total = int(resp.headers.get("Content-Length", 0))
        resp.read(1024)
        return {"status": resp.status, "content_type": resp.headers.get("Content-Type", ""),
                "content_length": total}
    except Exception:
        # 探测失败时返回默认值，不阻塞解析流程
        return {"status": 200, "content_type": "image/jpeg", "content_length": 0}


def process_xhs(share_url: str) -> dict:
    # 保留完整 URL（含 xsec_token 参数），小红书改版后需要 token 才能获取笔记数据
    page_url = xhs_resolve_share_url(share_url)
    note_id = xhs_extract_note_id(page_url)
    # 提取 xsec_token 供下载接口使用
    token_match = re.search(r"[?&]xsec_token=([^&]+)", page_url)
    xsec_token = urllib.parse.unquote(token_match.group(1)) if token_match else ""
    html = xhs_fetch_page_html(page_url)
    state = xhs_parse_initial_state(html)

    try:
        info = xhs_extract_note_info(state, note_id)
    except ValueError:
        # 页面里没有任何笔记数据：区分「失效」和「需要新 token」两种情况
        if "/404" in html[:5000] or "页面不存在" in html:
            raise ValueError("该小红书笔记可能已被删除或链接已失效")
        if "xsec_token" not in page_url:
            raise ValueError(
                "小红书改版后需要完整的分享链接（含 xsec_token 参数）。\n"
                "请在小红书 App 中点击「分享」→「复制链接」，粘贴完整的分享文本。"
            )
        raise ValueError("未能获取小红书笔记数据，链接中的 xsec_token 可能已过期，请重新分享获取新链接")

    if info["note_type"] == "video":
        probe = xhs_probe_url(info["download_url"])
        direct_ok, direct_len, direct_ctype = probe_browser_direct(info["download_url"])
        return {
            "platform": "xhs",
            "ok": True,
            "note_type": "video",
            "video_id": info["note_id"],
            "author": info["nickname"],
            "desc": info["title"] or info["desc"],
            "resolution": f"{info['width']}x{info['height']}",
            "duration": format_duration(info["duration_ms"]),
            "digg_count": info["liked_count"],
            "comment_count": info["comment_count"],
            "share_count": info["share_count"],
            "collected_count": info["collected_count"],
            "cover_url": info["cover_url"],
            "pub_date": info["pub_date"],
            "download_url": info["download_url"],
            "file_size": probe["content_length"] or direct_len,
            "file_size_human": format_size(probe["content_length"] or direct_len),
            "content_type": probe["content_type"] or direct_ctype,
            "direct_ok": direct_ok,
            "xsec_token": xsec_token,
        }
    else:
        # 图文笔记
        images = info["images"]
        image_results = []
        total_size = 0
        for img in images:
            probe = xhs_probe_url(img["url"])
            image_results.append({
                "url": img["url"],
                "width": img["width"],
                "height": img["height"],
                "file_size": probe["content_length"],
                "file_size_human": format_size(probe["content_length"]),
                "content_type": probe["content_type"],
            })
            total_size += probe["content_length"]

        return {
            "platform": "xhs",
            "ok": True,
            "note_type": "image",
            "video_id": info["note_id"],
            "author": info["nickname"],
            "desc": info["title"] or info["desc"],
            "resolution": f"{images[0]['width']}x{images[0]['height']}",
            "duration": f"{len(images)} 张图片",
            "digg_count": info["liked_count"],
            "comment_count": info["comment_count"],
            "share_count": info["share_count"],
            "collected_count": info["collected_count"],
            "cover_url": images[0]["url"],
            "pub_date": info["pub_date"],
            "images": image_results,
            "file_size": total_size,
            "file_size_human": format_size(total_size),
            "content_type": "image/jpeg",
            "xsec_token": xsec_token,
        }


# ── 统一入口 ──────────────────────────────────────────

TIKTOK_REFERER = "https://www.tiktok.com/"

def tk_resolve_url(share_url: str) -> str:
    """解析 TikTok 短链（vm.tiktok.com / vt.tiktok.com）到完整视频页 URL"""
    lower = share_url.lower()
    if "vm.tiktok.com" in lower or "vt.tiktok.com" in lower:
        req = urllib.request.Request(share_url, headers={"User-Agent": DESKTOP_UA})
        resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=15)
        return resp.geturl()
    return share_url

def process_tiktok(share_url: str) -> dict:
    """解析 TikTok 视频展示信息（含 bitrateInfo 画质详情）。

    注意：TikTok 官方视频直链（playAddr）绑定获取它的 cookie 会话，
    跨会话下载会被 Akamai 403。因此这里只取展示信息（作者/描述/封面等），
    真正的无水印原画下载由后端在「同一 curl_cffi 会话内」实时抓取并代理。
    """
    if not HAS_CURL_CFFI:
        raise ValueError("服务端缺少 curl_cffi 依赖，无法解析 TikTok")

    page_url = tk_resolve_url(share_url)

    s = cffi_requests.Session(impersonate="chrome")
    s.get("https://www.tiktok.com/", headers={
        "User-Agent": DESKTOP_UA, "Accept-Language": "en-US,en;q=0.9"
    }, timeout=30)
    r = s.get(page_url, headers={
        "User-Agent": DESKTOP_UA, "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.tiktok.com/"
    }, timeout=30)
    html = r.text.replace("\\u002F", "/").replace("\\u0026", "&") \
                 .replace("\\u003C", "<").replace("\\u003E", ">")

    def grab(name, default=""):
        m = re.search(r'"%s"\s*:\s*"([^"]*)"' % name, html)
        return m.group(1) if m else default

    def grabi(name, default=0):
        m = re.search(r'"%s"\s*:\s*(\d+)' % name, html)
        return int(m.group(1)) if m else default

    author = grab("uniqueId") or grab("nickname") or "tiktok_user"
    desc = grab("desc")
    cover = grab("originCover") or grab("cover")
    width = grabi("width")
    height = grabi("height")
    duration_s = grabi("duration")
    digg = grabi("diggCount")
    comment = grabi("commentCount")
    share = grabi("shareCount")
    collect = grabi("collectCount")

    # ---- 解析 bitrateInfo 获取画质详情 ----
    quality_detail = ""
    total_bitrates = 0
    best_gear = ""
    best_size = 0
    bm = re.search(r'"bitrateInfo"\s*(?::\s*)(\[.*?\])(?:\s*,|\s*\})', html, re.DOTALL)
    if bm:
        try:
            import json as _json
            raw = bm.group(1).rstrip()
            if raw.endswith(','):
                raw = raw[:-1]
            bitrates = _json.loads(raw)
            if isinstance(bitrates, list):
                total_bitrates = len(bitrates)
                sorted_br = sorted(bitrates, key=lambda x: x.get("Bitrate", 0), reverse=True)
                if sorted_br:
                    top = sorted_br[0]
                    pa = top.get("PlayAddr", {})
                    best_gear = top.get("GearName", "")
                    best_size = int(pa.get("DataSize", 0))
                    br_kbps = top.get("Bitrate", 0) // 1000
                    w = pa.get("Width", width)
                    h = pa.get("Height", height)
                    quality_detail = f"{w}x{h} \u00b7 {br_kbps}kbps \u00b7 {format_size(best_size)}"
                    if total_bitrates > 1:
                        quality_detail += f" ({total_bitrates}\u79cd\u753b\u8d28\u53ef\u9009\uff0c\u5df2\u9009\u6700\u9ad8)"
        except Exception:
            pass

    if not quality_detail:
        resolution = f"{width}x{height}" if width and height else "\u539f\u753b"
        quality_detail = resolution

    duration = f"{duration_s // 60:02d}:{duration_s % 60:02d}" if duration_s else ""

    return {
        "platform": "tiktok",
        "ok": True,
        "author": author,
        "desc": desc,
        "cover_url": cover,
        "resolution": f"{width}x{height}" if width and height else "\u539f\u753b",
        "duration": duration,
        "download_url": page_url,
        "file_size_human": format_size(best_size) if best_size else "\u670d\u52a1\u7aef\u4ee3\u7406\u4e0b\u8f7d",
        "file_size": best_size,
        "content_type": "video/mp4",
        "digg_count": digg,
        "comment_count": comment,
        "share_count": share,
        "collected_count": collect,
        "server_download": True,
        "quality_detail": quality_detail,
        "gear_name": best_gear,
    }


def process_link(share_url: str, quality: str = "default") -> dict:
    # 自动从分享文本中提取 URL
    url = extract_url_from_text(share_url)
    platform = detect_platform(url)
    if platform == "douyin":
        return process_douyin(url, quality)
    elif platform == "xhs":
        return process_xhs(url)
    raise ValueError("不支持的平台（目前仅支持抖音和小红书）")


# ── YouTube 核心逻辑 ──────────────────────────────────────

# YouTube 客户端优先级列表（从服务器/数据中心 IP 访问时）
# None = 使用 yt-dlp 默认客户端选择（配合 bgutil PO Token 插件自动供给 token，
# 这是数据中心 IP 唯一稳定的方案）；其余为无 token 时的手动兜底。
_YT_CLIENTS = [None, "tv_embedded", "android", "ios", "mweb", "web"]


def _yt_base_opts(download: bool = False, client=None) -> dict:
    """yt-dlp 通用选项。download=True 用于真正下载并合并。

    client=None 表示不强制指定客户端（让 yt-dlp 默认策略 + PO Token 插件工作）。
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": False,
        "nocheckcertificate": True,
    }
    if client:
        opts["extractor_args"] = {
            "youtube": {
                "player_client": client,
            }
        }
    if FFMPEG_EXE:
        opts["ffmpeg_location"] = FFMPEG_EXE
    return opts


def _yt_try_clients(url, action_fn, action_name="extract"):
    """依次尝试多个 YouTube 客户端，第一个成功即返回结果。

    action_fn(client, opts) -> result
    遇到 bot 拦截 / 网络错误时自动切换下一个客户端。
    """
    last_err = None
    for client in _YT_CLIENTS:
        try:
            opts = _yt_base_opts(download=(action_name == "download"), client=client)
            return action_fn(client, opts), client
        except Exception as e:
            last_err = e
            err_msg = str(e).lower()
            # bot 拦截 / 登录要求 / 429 限流 → 换客户端重试
            if any(kw in err_msg for kw in [
                "bot", "sign in", "confirm you're not", "429",
                "too many requests", "forbidden", "concurrent",
                "failed to extract any player response", "unable to extract",
                "player response", "no video formats", "requested format",
            ]):
                continue
            # 其他异常（如 URL 格式错）不重试，直接抛出
            raise
    raise ValueError(f"YouTube {action_name} 失败（所有客户端均被拦截）: {last_err}")


def process_youtube(share_url: str, quality: str = "best") -> dict:
    """解析 YouTube 视频展示信息（标题/作者/分辨率/大小等）。

    注意：YouTube 直链受严格反爬 + 音视频分离（DASH），无法像 TikTok 那样直接代理。
    真正的「最高清」下载由后端用 yt-dlp 在服务端抓取并合并（见 yt_proxy_download）。
    """
    if not HAS_YTDLP:
        raise ValueError("服务端缺少 yt-dlp 依赖，无法解析 YouTube")

    url = extract_url_from_text(share_url)

    def _probe(client, opts):
        po = dict(opts)
        po.update({"simulate": True, "format": "bestvideo+bestaudio/best"})
        with yt_dlp.YoutubeDL(po) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info, used_client = _yt_try_clients(url, _probe, action_name="extract")
    except Exception as e:
        raise ValueError(f"YouTube 解析失败（可能被反爬拦截，可重试或换链接）: {e}")

    # 处理播放列表：取第一个视频
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        if not entries:
            raise ValueError("YouTube 播放列表为空或解析失败")
        info = entries[0]

    title = info.get("title", "youtube_video")
    author = info.get("uploader") or info.get("channel") or info.get("uploader_id") or "youtube"
    duration_s = int(info.get("duration") or 0)
    width = info.get("width") or 0
    height = info.get("height") or 0
    # 优先用合并后格式的文件大小
    size = int(info.get("filesize") or info.get("filesize_approx") or 0)
    if not size and info.get("formats"):
        best_fmt = max(
            [f for f in info["formats"] if f.get("filesize")],
            key=lambda f: f.get("filesize", 0), default={}
        )
        size = int(best_fmt.get("filesize", 0))

    resolution = f"{width}x{height}" if width and height else "原画"
    duration = f"{duration_s // 60:02d}:{duration_s % 60:02d}" if duration_s else ""

    return {
        "platform": "youtube",
        "ok": True,
        "author": author,
        "desc": title,
        "cover_url": info.get("thumbnail") or "",
        "resolution": resolution,
        "duration": duration,
        "download_url": url,
        "file_size_human": format_size(size) if size else "服务端代理下载",
        "file_size": size,
        "content_type": "video/mp4",
        "note": f"YouTube 最高清（音视频合并，客户端:{used_client or 'default+pot'}），由服务端代理下载",
    }


# ── 工具函数 ──────────────────────────────────────────

def format_size(n: int) -> str:
    if n >= 1073741824: return f"{n/1073741824:.2f} GB"
    if n >= 1048576: return f"{n/1048576:.1f} MB"
    if n >= 1024: return f"{n/1024:.1f} KB"
    return f"{n} B"


def format_duration(ms: int) -> str:
    s = ms // 1000
    return f"{s//60:02d}:{s%60:02d}"


# ── 前端页面 ──────────────────────────────────────────

# Service Worker：把「CDN 直连」包装成浏览器原生下载。
# 浏览器拦截 /__dl 请求，在这里补上 Content-Disposition 再把流交回去，
# 于是下载进度/暂停/续传/中文文件名都由浏览器下载器负责，而数据依然直连 CDN。
SW_JS = r"""/* direct-download service worker */
const DL_PATH = '/__dl';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

self.addEventListener('fetch', (event) => {
  const req = event.request;
  let url;
  try { url = new URL(req.url); } catch (e) { return; }
  if (url.origin !== self.location.origin) return;
  if (url.pathname !== DL_PATH) return;
  event.respondWith(handleDownload(url));
});

async function handleDownload(url) {
  const p = url.searchParams;
  const target = p.get('u');
  const name = p.get('n') || 'download';
  const fb = p.get('f') || '';
  if (!target) {
    return new Response('missing url', { status: 400, headers: { 'Content-Type': 'text/plain' } });
  }

  try {
    const resp = await fetch(target, {
      referrerPolicy: 'no-referrer',
      credentials: 'omit',
      mode: 'cors',
      cache: 'no-store',
      redirect: 'follow',
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);

    const headers = new Headers();
    headers.set('Content-Type', resp.headers.get('content-type') || 'application/octet-stream');
    const len = resp.headers.get('content-length');
    if (len) headers.set('Content-Length', len);
    headers.set('Content-Disposition', "attachment; filename*=UTF-8''" + encodeURIComponent(name));
    headers.set('Cache-Control', 'no-store');
    headers.set('X-Content-Type-Options', 'nosniff');
    return new Response(resp.body, { status: 200, statusText: 'OK', headers });
  } catch (err) {
    // 直连被拒 → 退回服务端中转（同样是原生下载，只是慢一点）
    if (fb) {
      try {
        return Response.redirect(new URL(fb, self.location.origin).href, 302);
      } catch (e) {}
    }
    return new Response('direct download failed: ' + err.message, {
      status: 502,
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
    });
  }
}
"""


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>去水印 · 原视频/图片下载（抖音 / 小红书）</title>
<style>
  :root {
    --bg: #0f0f13;
    --card: #1a1a22;
    --card-hover: #22222e;
    --border: #2e2e3a;
    --text: #e8e8ef;
    --text-dim: #8888a0;
    --accent: #ff2c55;
    --accent-hover: #ff4470;
    --accent-dim: rgba(255,44,85,.12);
    --xhs: #ff2442;
    --xhs-hover: #ff4466;
    --xhs-dim: rgba(255,36,66,.12);
    --green: #00d68f;
    --radius: 16px;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
  }
  .container { width: 100%; max-width: 580px; }

  /* Header */
  .header { text-align: center; margin-bottom: 28px; }
  .logo {
    display: inline-flex; align-items: center; gap: 10px;
    font-size: 22px; font-weight: 700; margin-bottom: 6px;
  }
  .logo-icon {
    width: 38px; height: 38px; border-radius: 12px;
    background: linear-gradient(135deg, var(--accent), #ff6b8a);
    display: flex; align-items: center; justify-content: center;
    font-size: 20px;
  }
  .subtitle { color: var(--text-dim); font-size: 13px; }

  /* Platform tabs */
  .platform-tabs {
    display: flex; gap: 8px; margin-bottom: 14px; justify-content: center;
  }
  .platform-tab {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 8px 16px; border-radius: 10px;
    background: var(--card); border: 1px solid var(--border);
    font-size: 13px; color: var(--text-dim); cursor: pointer;
    transition: all .2s;
  }
  .platform-tab.active {
    background: var(--accent-dim); border-color: var(--accent); color: var(--accent);
  }
  .platform-tab.active.xhs {
    background: var(--xhs-dim); border-color: var(--xhs); color: var(--xhs);
  }
  .platform-tab:hover { border-color: var(--text-dim); }

  /* Input Card */
  .input-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 24px;
  }
  .input-row { display: flex; gap: 12px; }
  .input-wrap { flex: 1; position: relative; }
  .input-wrap input {
    width: 100%;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 14px 16px 14px 42px;
    color: var(--text);
    font-size: 15px;
    outline: none;
    transition: border-color .2s;
  }
  .input-wrap input:focus { border-color: var(--accent); }
  .input-wrap input:focus.xhs { border-color: var(--xhs); }
  .input-wrap input::placeholder { color: var(--text-dim); }
  .input-icon {
    position: absolute; left: 14px; top: 50%; transform: translateY(-50%);
    font-size: 18px; opacity: .5;
  }
  .btn-parse {
    background: var(--accent);
    color: #fff;
    border: none;
    border-radius: 12px;
    padding: 14px 24px;
    font-size: 15px;
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
    transition: background .2s, transform .1s;
  }
  .btn-parse.xhs { background: var(--xhs); }
  .btn-parse:hover { background: var(--accent-hover); }
  .btn-parse.xhs:hover { background: var(--xhs-hover); }
  .btn-parse:active { transform: scale(.96); }
  .btn-parse:disabled { opacity: .5; cursor: not-allowed; }

  /* Quality toggle (抖音 only) */
  .quality-row {
    display: flex; align-items: center; gap: 8px;
    margin-top: 14px; font-size: 13px; color: var(--text-dim);
    transition: opacity .2s;
  }
  .quality-row.disabled { opacity: .3; pointer-events: none; }
  .quality-toggle {
    display: inline-flex; background: var(--bg); border-radius: 10px; padding: 3px;
    border: 1px solid var(--border);
  }
  .quality-toggle button {
    background: none; border: none; color: var(--text-dim);
    padding: 6px 14px; border-radius: 8px; font-size: 13px; cursor: pointer;
    transition: all .2s;
  }
  .quality-toggle button.active {
    background: var(--accent-dim); color: var(--accent);
  }

  /* Loading */
  .loading {
    text-align: center; padding: 40px 0; color: var(--text-dim);
  }
  .spinner {
    width: 36px; height: 36px;
    border: 3px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin .7s linear infinite;
    margin: 0 auto 16px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Error */
  .error {
    background: rgba(255,44,85,.1);
    border: 1px solid rgba(255,44,85,.3);
    border-radius: 12px;
    padding: 16px;
    color: #ff6b8a;
    font-size: 14px;
    margin-top: 16px;
  }
  /* Tip（下载已交给浏览器时提示） */
  .tip {
    background: rgba(37,244,238,.09);
    border: 1px solid rgba(37,244,238,.28);
    border-radius: 12px;
    padding: 14px 16px;
    color: var(--green);
    font-size: 13px;
    line-height: 1.6;
    margin-top: 16px;
    animation: slideUp .25s ease;
  }
  .tip b { color: var(--text); font-weight: 700; }

  /* Result Card */
  .result-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    margin-top: 20px;
    animation: slideUp .3s ease;
  }
  @keyframes slideUp {
    from { opacity: 0; transform: translateY(16px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .result-cover {
    width: 100%; max-height: 220px; object-fit: cover;
    display: block;
  }
  .result-body { padding: 20px; }
  .result-author {
    display: flex; align-items: center; gap: 8px;
    font-size: 14px; margin-bottom: 6px; font-weight: 600;
  }
  .result-author.douyin { color: var(--accent); }
  .result-author.xhs { color: var(--xhs); }
  .platform-badge {
    font-size: 10px; padding: 2px 8px; border-radius: 6px;
    background: var(--accent-dim); color: var(--accent);
  }
  .platform-badge.xhs { background: var(--xhs-dim); color: var(--xhs); }
  .result-desc {
    font-size: 14px; color: var(--text); margin-bottom: 16px;
    line-height: 1.5;
  }

  /* Stats grid */
  .stats {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px;
    margin-bottom: 20px;
  }
  .stat {
    background: var(--bg); border-radius: 10px; padding: 12px 8px;
    text-align: center;
  }
  .stat-val { font-size: 16px; font-weight: 700; }
  .stat-label { font-size: 11px; color: var(--text-dim); margin-top: 2px; }

  /* Download bar */
  .download-bar {
    display: flex; gap: 12px; align-items: center;
    background: var(--bg); border-radius: 12px; padding: 14px 16px;
  }
  .download-info { flex: 1; min-width: 0; }
  .download-quality { font-size: 13px; font-weight: 600; color: var(--green); }
  .download-size { font-size: 12px; color: var(--text-dim); margin-top: 2px; }
  .btn-download {
    background: var(--green);
    color: #002618;
    border: none;
    border-radius: 10px;
    padding: 10px 20px;
    font-size: 14px;
    font-weight: 700;
    font-family: inherit;
    cursor: pointer;
    text-decoration: none;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    transition: opacity .2s, transform .1s;
  }
  .btn-download:disabled, .btn-direct:disabled { opacity: .7; cursor: default; }
  .btn-download:hover { opacity: .85; }
  .btn-download:active { transform: scale(.95); }
  /* 直连下载（浏览器直连源站 CDN，国内速度更快） */
  .btn-direct {
    background: transparent;
    color: var(--green);
    border: 1px solid var(--green);
    border-radius: 10px;
    padding: 9px 14px;
    font-size: 13px;
    font-weight: 600;
    font-family: inherit;
    cursor: pointer;
    text-decoration: none;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    white-space: nowrap;
    transition: opacity .2s, transform .1s;
  }
  .btn-direct:hover { opacity: .8; }
  .btn-direct:active { transform: scale(.95); }

  /* Copy link */
  .copy-row {
    display: flex; gap: 8px; margin-top: 12px;
  }
  .copy-input {
    flex: 1; background: var(--bg); border: 1px solid var(--border);
    border-radius: 10px; padding: 10px 12px; color: var(--text-dim);
    font-size: 12px; outline: none;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .btn-copy {
    background: var(--card-hover); border: 1px solid var(--border);
    border-radius: 10px; padding: 10px 16px; color: var(--text);
    font-size: 13px; cursor: pointer; white-space: nowrap;
    transition: background .2s;
  }
  .btn-copy:hover { background: var(--border); }
  .btn-copy.copied { background: var(--green); color: #002618; border-color: var(--green); }

  /* Image gallery */
  .image-gallery {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 10px;
    margin-top: 16px;
  }
  .img-card {
    position: relative;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
  }
  .img-thumb {
    width: 100%;
    aspect-ratio: 3/4;
    object-fit: cover;
    display: block;
    cursor: pointer;
    transition: opacity .2s;
  }
  .img-thumb:hover { opacity: .85; }
  .img-info {
    display: flex;
    justify-content: space-between;
    padding: 8px 10px;
    font-size: 11px;
    color: var(--text-dim);
  }
  .btn-img-download {
    position: absolute;
    top: 8px; right: 8px;
    width: 32px; height: 32px;
    background: rgba(0,0,0,.6);
    color: #fff;
    border: none;
    border-radius: 8px;
    font-size: 16px;
    font-family: inherit;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    text-decoration: none;
    opacity: 0;
    transition: opacity .2s;
  }
  .img-card:hover .btn-img-download { opacity: 1; }

  /* Footer */
  .footer {
    text-align: center; color: var(--text-dim); font-size: 12px;
    margin-top: 24px;
  }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="logo">
      <div class="logo-icon">🎬</div>
      <span>去水印下载</span>
    </div>
    <p class="subtitle">抖音 / 小红书 · 粘贴链接，一键获取无水印原视频/原图</p>
  </div>

  <div class="platform-tabs">
    <div class="platform-tab active" id="tab-douyin" onclick="setPlatform('douyin')">🎵 抖音</div>
    <div class="platform-tab" id="tab-xhs" onclick="setPlatform('xhs')">📕 小红书</div>
  </div>

  <div class="input-card">
    <div class="input-row">
      <div class="input-wrap">
        <span class="input-icon">🔗</span>
        <input type="text" id="url-input" placeholder="粘贴抖音 / 小红书分享链接或完整分享文本..." autofocus>
      </div>
      <button class="btn-parse" id="btn-parse" onclick="parseLink()">解析</button>
    </div>
    <div class="quality-row" id="quality-row">
      <span>清晰度：</span>
      <div class="quality-toggle">
        <button class="active" data-q="default" onclick="setQuality('default')">原画</button>
        <button data-q="720p" onclick="setQuality('720p')">720P</button>
      </div>
    </div>
    <div id="error-msg"></div>
  </div>

  <div id="result-area"></div>

  <div class="footer">
    本工具仅供学习交流使用 · 请尊重创作者版权
  </div>
</div>

<script>
let platform = 'douyin';
let quality = 'default';
const urlInput = document.getElementById('url-input');
const btnParse = document.getElementById('btn-parse');
const resultArea = document.getElementById('result-area');
const errorEl = document.getElementById('error-msg');
const qualityRow = document.getElementById('quality-row');
let currentRawUrl = '';  // 保存用户原始输入，供 TikTok 重新获取直链使用

urlInput.addEventListener('input', () => {
  const v = urlInput.value.trim().toLowerCase();
  if (v.includes('xiaohongshu') || v.includes('xhslink') || v.includes('xhs.cn')) {
    if (platform !== 'xhs') setPlatform('xhs');
  } else if (v.includes('douyin') || v.includes('iesdouyin')) {
    if (platform !== 'douyin') setPlatform('douyin');
  }
});
urlInput.addEventListener('keydown', e => { if (e.key === 'Enter') parseLink(); });

function setPlatform(p) {
  platform = p;
  document.querySelectorAll('.platform-tab').forEach(t => t.classList.remove('active', 'xhs'));
  const tab = document.getElementById('tab-' + p);
  tab.classList.add('active');
  if (p === 'xhs') tab.classList.add('xhs');

  btnParse.classList.remove('xhs');
  urlInput.classList.remove('xhs');

  if (p === 'xhs') {
    urlInput.placeholder = '粘贴小红书分享链接...';
    btnParse.classList.add('xhs');
    urlInput.classList.add('xhs');
    qualityRow.classList.add('disabled');
  } else {
    urlInput.placeholder = '粘贴抖音分享链接...';
    qualityRow.classList.remove('disabled');
  }
  urlInput.focus();
}

function setQuality(q) {
  quality = q;
  document.querySelectorAll('.quality-toggle button').forEach(b => {
    b.classList.toggle('active', b.dataset.q === q);
  });
}

function showError(msg) {
  errorEl.innerHTML = '<div class="error">⚠️ ' + msg + '</div>';
}
function showTip(html) {
  errorEl.innerHTML = '<div class="tip">' + html + '</div>';
}
function clearError() { errorEl.innerHTML = ''; }

function formatNum(n) {
  if (!n) return '0';
  if (n >= 10000) return (n / 10000).toFixed(1) + '万';
  return n.toLocaleString();
}

// ── 浏览器原生下载 ──────────────────────────────────────────────
// 跨域链接的 download 属性会被浏览器忽略（文件名变哈希、只会在线播放），
// 而后端中转又慢。折中做法：注册一个 Service Worker 拦住同源的 /__dl，
// 由 SW 去直连 CDN 拉流，再补上 Content-Disposition 交还给浏览器。
// 这样速度仍是 CDN 直连，但下载/进度/断点/命名全部由浏览器下载器接管。
function attrEnc(s) {
  return encodeURIComponent(String(s || '')).replace(/'/g, '%27');
}

// SW 就绪 promise（拿不到就为 null，走下面的 Blob 兜底）
const swReady = (function () {
  if (!('serviceWorker' in navigator)) return Promise.resolve(null);
  try {
    return navigator.serviceWorker
      .register('/sw-dl.js', { scope: '/' })
      .then(() => navigator.serviceWorker.ready)
      .catch(() => null);
  } catch (e) { return Promise.resolve(null); }
})();

function withTimeout(p, ms) {
  return Promise.race([p, new Promise(r => setTimeout(() => r(null), ms))]);
}

// 用同源 <a download> 触发；节点必须留在 DOM 里一小会儿，
// 立刻 remove 会让部分 Chrome 版本把下载标记为「已取消」。
function triggerNativeDownload(url, filename, relay) {
  let q = '/__dl?u=' + encodeURIComponent(url) + '&n=' + encodeURIComponent(filename);
  if (relay) q += '&f=' + encodeURIComponent(relay);
  const a = document.createElement('a');
  a.href = q;
  a.download = filename;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { try { a.remove(); } catch (e) {} }, 600000);
}

// 兜底：SW 不可用时，自己拉流拼 Blob 再保存
async function blobDownload(url, filename, relay, setTxt) {
  const mb = n => (n / 1048576).toFixed(1);
  const resp = await fetch(url, {
    referrerPolicy: 'no-referrer', credentials: 'omit', mode: 'cors', cache: 'no-store',
  });
  if (!resp.ok) throw new Error('HTTP ' + resp.status);
  const total = Number(resp.headers.get('content-length') || 0);
  let blob = null;
  if (resp.body && resp.body.getReader) {
    const reader = resp.body.getReader();
    const chunks = [];
    let loaded = 0, tick = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      loaded += value.length;
      const now = Date.now();
      if (now - tick > 200) {
        tick = now;
        setTxt(total
          ? `⏳ ${Math.floor(loaded / total * 100)}%  ${mb(loaded)}/${mb(total)}MB`
          : `⏳ ${mb(loaded)}MB`);
      }
    }
    blob = new Blob(chunks, { type: resp.headers.get('content-type') || 'application/octet-stream' });
  } else {
    blob = await resp.blob();
  }
  if (!blob.size) throw new Error('文件为空');
  const objUrl = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = objUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { try { a.remove(); } catch (e) {} }, 600000);
  setTimeout(() => URL.revokeObjectURL(objUrl), 600000);
}

async function directDownload(btn) {
  const url = decodeURIComponent(btn.dataset.url || '');
  const filename = decodeURIComponent(btn.dataset.name || 'download.mp4');
  const relay = btn.dataset.relay || '';
  if (!url || btn.dataset.busy === '1') return;

  if (!btn.dataset.label) btn.dataset.label = btn.textContent.trim();
  btn.dataset.busy = '1';
  btn.disabled = true;
  const setTxt = t => { btn.textContent = t; };

  try {
    const reg = await withTimeout(swReady, 4000);
    if (reg) {
      // 主路径：浏览器原生下载器接管（可暂停/续传、真实速度、自动命名）
      triggerNativeDownload(url, filename, relay);
      setTxt('✔ 已开始下载');
      showTip('已交给浏览器下载，请查看浏览器<b>右上角下载条</b>（或按 <b>⌘/Ctrl + J</b>）。<br>文件名：<b>'
        + filename + '</b>　若下载条显示失败，请点右侧「🛰 中转」。');
    } else {
      setTxt('⏳ 下载中…');
      await blobDownload(url, filename, relay, setTxt);
      setTxt('✔ 已保存');
      clearError();
    }
  } catch (e) {
    if (relay) {
      setTxt('⚠ 转中转下载…');
      showError('直连被源站拒绝（' + e.message + '），已自动切换到「🛰 中转」下载，速度会慢一些属正常。');
      setTimeout(() => { window.location.href = relay; }, 600);
    } else {
      setTxt('⚠ 直连失败');
      showError('下载失败（' + e.message + '）：源站不允许浏览器直连，请点旁边的「🛰 中转」下载。');
    }
  } finally {
    btn.dataset.busy = '0';
    setTimeout(() => {
      btn.textContent = btn.dataset.label;
      btn.disabled = false;
    }, 4000);
  }
}

async function parseLink() {
  const url = urlInput.value.trim();
  if (!url) { showError('请输入视频链接'); return; }
  currentRawUrl = url;  // 记录原始输入

  clearError();
  resultArea.innerHTML = '';
  btnParse.disabled = true;
  btnParse.textContent = '解析中...';

  resultArea.innerHTML = `
    <div class="result-card"><div class="loading">
      <div class="spinner"></div>
      <p>正在解析视频信息...</p>
    </div></div>`;

  try {
    const resp = await fetch('/api/parse', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, quality, platform })
    });
    const data = await resp.json();

    if (!data.ok) throw new Error(data.error || '解析失败');

    const isXHS = data.platform === 'xhs';
    const pClass = isXHS ? 'xhs' : 'douyin';
    const pLabel = isXHS ? '小红书' : '抖音';
    const pIcon = isXHS ? '📕' : '🎵';
    const isImage = data.note_type === 'image';

    const badge = isXHS
      ? (isImage
        ? `<span style="color:var(--green)">● 原图高清</span>`
        : `<span style="color:var(--green)">● 原始高清</span>`)
      : (quality === 'default'
        ? `<span style="color:var(--green)">● 原始高清</span>`
        : `<span style="color:var(--text-dim)">● 720P 压缩</span>`);

    if (isImage) {
      // 图文笔记：显示图片画廊
      let imgCards = '';
      data.images.forEach((img, i) => {
        const imgName = (data.author + (data.pub_date ? '_' + data.pub_date : '') + '_' + (i+1) + '.jpg').replace(/[\\/:*?"<>|#]/g, '_');
        imgCards += `
          <div class="img-card">
            <img class="img-thumb" src="${img.url}" alt="图${i+1}" referrerpolicy="no-referrer" loading="lazy">
            <div class="img-info">
              <span class="img-dim">${img.width}x${img.height}</span>
              <span class="img-size">${img.file_size_human}</span>
            </div>
            <button type="button" class="btn-img-download" data-url="${attrEnc(img.url)}" data-name="${attrEnc(imgName)}" onclick="directDownload(this)" title="点击即自动保存为「${imgName}」">⬇</button>
          </div>`;
      });

      const totalFilename = (data.author + '_' + (data.pub_date || '') + '_全部图片.zip').replace(/[\\/:*?"<>|#]/g, '_');

      resultArea.innerHTML = `
        <div class="result-card">
          <div class="result-body">
            <div class="result-author ${pClass}">
              <span class="platform-badge ${isXHS ? 'xhs' : ''}">${pIcon} ${pLabel}</span>
              @${data.author}${data.pub_date ? ` · ${data.pub_date}` : ''}
            </div>
            <div class="result-desc">${data.desc || '无描述'}</div>
            <div class="stats">
              <div class="stat">
                <div class="stat-val">${data.resolution}</div>
                <div class="stat-label">首图尺寸</div>
              </div>
              <div class="stat">
                <div class="stat-val">${data.duration}</div>
                <div class="stat-label">图片数</div>
              </div>
              <div class="stat">
                <div class="stat-val">${formatNum(data.digg_count)}</div>
                <div class="stat-label">点赞</div>
              </div>
              <div class="stat">
                <div class="stat-val">${formatNum(data.collected_count || data.comment_count)}</div>
                <div class="stat-label">收藏</div>
              </div>
            </div>
            <div class="download-bar">
              <div class="download-info">
                <div class="download-quality">${badge}</div>
                <div class="download-size">共 ${data.images.length} 张 · ${data.file_size_human}</div>
              </div>
              <a class="btn-download" href="/api/download_zip?note_id=${data.video_id}&author=${encodeURIComponent(data.author)}&pub_date=${encodeURIComponent(data.pub_date||'')}&xsec_token=${encodeURIComponent(data.xsec_token||'')}" target="_blank">
                ⬇ 打包下载
              </a>
            </div>
            <div class="image-gallery">${imgCards}</div>
          </div>
        </div>`;
    } else {
      // 视频笔记：文件名 = 达人名称 + 发布时间
      const safeName = s => String(s || '').replace(/[\\/:*?"<>|#]/g, '_').trim();
      const datePart = safeName(data.pub_date);
      const filename = (safeName(data.author) || 'video') + (datePart ? '_' + datePart : '') + '.mp4';

      // 直连是否可行由后端用「浏览器同款请求（桌面 UA、不带 Referer）」探测得出；
      // 抖音 CDN 对带外部 Referer 的请求返回 403，此时主按钮自动切到中转。
      const directOk = data.direct_ok !== false;
      const relayHref = `/api/download?url=${encodeURIComponent(data.download_url)}&platform=${data.platform}&filename=${encodeURIComponent(filename)}`;
      const directBtn = `<button type="button" class="btn-${directOk ? 'download' : 'direct'}" data-url="${attrEnc(data.download_url)}" data-name="${attrEnc(filename)}" data-relay="${attrEnc(relayHref)}" onclick="directDownload(this)" title="浏览器直连源站 CDN（速度最快），点击即自动保存为「${filename}」">⚡ 直连下载</button>`;
      const relayBtn = `<a class="btn-${directOk ? 'direct' : 'download'}" href="${relayHref}" title="经服务器中转下载，兼容性最好（直连 403/失败时用这个），同样自动命名为「${filename}」">🛰 中转</a>`;
      const tipText = directOk
        ? `⚡ 直连下载不经服务器中转，国内速度通常快 10 倍以上；点击后自动保存为「${filename}」。若提示失败请改用「🛰 中转」。`
        : '该视频源站不允许浏览器直连（会返回 403），已默认走「🛰 中转」，速度受服务器带宽限制。也可点「⚡ 直连下载」自己试一次。';

      resultArea.innerHTML = `
        <div class="result-card">
          ${data.cover_url ? `<img class="result-cover" src="${data.cover_url}" alt="cover" referrerpolicy="no-referrer">` : ''}
          <div class="result-body">
            <div class="result-author ${pClass}">
              <span class="platform-badge ${isXHS ? 'xhs' : ''}">${pIcon} ${pLabel}</span>
              @${data.author}${data.pub_date ? ` · ${data.pub_date}` : ''}
            </div>
            <div class="result-desc">${data.desc || '无描述'}</div>
            <div class="stats">
              <div class="stat">
                <div class="stat-val">${data.resolution}</div>
                <div class="stat-label">分辨率</div>
              </div>
              <div class="stat">
                <div class="stat-val">${data.duration}</div>
                <div class="stat-label">时长</div>
              </div>
              <div class="stat">
                <div class="stat-val">${formatNum(data.digg_count)}</div>
                <div class="stat-label">点赞</div>
              </div>
              <div class="stat">
                <div class="stat-val">${formatNum(data.collected_count || data.comment_count)}</div>
                <div class="stat-label">${isXHS ? '收藏' : '评论'}</div>
              </div>
            </div>
            <div class="download-bar">
              <div class="download-info">
                <div class="download-quality">${badge}</div>
                <div class="download-size">${data.file_size_human} · ${data.content_type}</div>
              </div>
              ${directBtn}
              ${relayBtn}
            </div>
            <div class="copy-row">
              <input class="copy-input" id="dl-url" value="${data.download_url}" readonly>
              <button class="btn-copy" onclick="copyLink(this)">复制链接</button>
            </div>
            <div class="tiktok-tip">${tipText}</div>
          </div>
        </div>`;
    }
  } catch (err) {
    resultArea.innerHTML = '';
    showError(err.message);
  } finally {
    btnParse.disabled = false;
    btnParse.textContent = '解析';
  }
}

function copyLink(btn) {
  const input = document.getElementById('dl-url');
  navigator.clipboard.writeText(input.value).then(() => {
    btn.textContent = '已复制 ✓';
    btn.classList.add('copied');
    setTimeout(() => {
      btn.textContent = '复制链接';
      btn.classList.remove('copied');
    }, 2000);
  });
}

// TikTok：点击时后端重新抓取最新直链（规避签名过期），再用浏览器打开
async function tiktokDownload(e) {
  e.preventDefault();
  if (!currentRawUrl) return;
  const btn = e.currentTarget;
  const oldText = btn.textContent;
  btn.textContent = '获取直链...';
  btn.disabled = true;
  try {
    const resp = await fetch('/api/parse', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: currentRawUrl })
    });
    const data = await resp.json();
    if (!data.ok || !data.download_url) throw new Error(data.error || '获取直链失败');
    const inp = document.getElementById('dl-url');
    if (inp) inp.value = data.download_url;
    const w = window.open(data.download_url, '_blank', 'noreferrer');
    if (!w) throw new Error('浏览器拦截了弹窗，请允许弹窗后重试，或复制链接下载');
  } catch (err) {
    alert('获取直链失败：' + err.message + '\n\n请复制下方链接，用支持 TikTok 的下载工具或能访问 TikTok 的浏览器获取。');
  } finally {
    btn.textContent = oldText;
    btn.disabled = false;
  }
}
</script>
</body>
</html>"""


# ── Web Server ────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/" or parsed.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            # SCF 网关默认会加 Content-Disposition: attachment，覆盖为 inline
            self.send_header("Content-Disposition", "inline")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))

        elif parsed.path == "/sw-dl.js":
            body = SW_JS.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.send_header("Service-Worker-Allowed", "/")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        elif parsed.path == "/__dl":
            # 正常由 Service Worker 拦截并补 Content-Disposition；
            # 若 SW 未生效（首次访问/隐私模式）就走服务端中转兜底。
            qs = urllib.parse.parse_qs(parsed.query)
            dl_url = qs.get("u", [""])[0]
            filename = qs.get("n", ["video.mp4"])[0]
            if not dl_url:
                self.send_error(400, "Missing u")
                return
            platform = "xhs" if "xhscdn" in dl_url or "xiaohongshu" in dl_url else "douyin"
            self._proxy_download(dl_url, filename, platform)

        elif parsed.path == "/api/download":
            qs = urllib.parse.parse_qs(parsed.query)
            dl_url = qs.get("url", [""])[0]
            filename = qs.get("filename", ["video.mp4"])[0]
            platform = qs.get("platform", ["douyin"])[0]
            if not dl_url:
                self.send_error(400, "Missing url")
                return
            if platform == "tiktok":
                self.tk_proxy_download(dl_url, filename)
            elif platform == "youtube":
                maxh = qs.get("maxh", [""])[0]
                self.yt_proxy_download(dl_url, filename, maxh)
            else:
                self._proxy_download(dl_url, filename, platform)

        elif parsed.path == "/api/download_zip":
            qs = urllib.parse.parse_qs(parsed.query)
            note_id = qs.get("note_id", [""])[0]
            author = qs.get("author", ["author"])[0]
            pub_date = qs.get("pub_date", [""])[0]
            xsec_token = qs.get("xsec_token", [""])[0]
            if not note_id:
                self.send_error(400, "Missing note_id")
                return
            self._proxy_download_zip(note_id, author, pub_date, xsec_token)

        else:
            self.send_error(404, "Not Found")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/parse":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len)
            try:
                data = json.loads(body)
                url = data.get("url", "").strip()
                quality = data.get("quality", "default")
                if not url:
                    self._json(400, {"ok": False, "error": "请输入链接"})
                    return
                result = process_link(url, quality)
                self._json(200, result)
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
        else:
            self.send_error(404, "Not Found")

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        # SCF 网关默认会加 Content-Disposition: attachment，覆盖为 inline
        self.send_header("Content-Disposition", "inline")
        # 增加跨域头，防止部分浏览器/网关触发 CORS 拦截
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _proxy_download(self, dl_url, filename, platform="douyin"):
        """代理下载：服务端请求 CDN，流式转发给浏览器"""
        if platform == "xhs":
            ua = DESKTOP_UA
            referer = XHS_REFERER
        else:
            ua = MOBILE_UA
            referer = DOUYIN_REFERER

        req = urllib.request.Request(dl_url, headers={
            "User-Agent": ua,
            "Referer": referer,
        })
        try:
            resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=120)
        except Exception as e:
            self.send_error(502, f"下载失败: {e}")
            return

        total = resp.headers.get("Content-Length", "")
        content_type = resp.headers.get("Content-Type", "application/octet-stream")
        # 根据文件名后缀修正 Content-Type
        if filename.lower().endswith(".jpg") or filename.lower().endswith(".jpeg"):
            content_type = "image/jpeg"
        elif filename.lower().endswith(".png"):
            content_type = "image/png"
        elif filename.lower().endswith(".mp4"):
            content_type = "video/mp4"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if total:
            self.send_header("Content-Length", total)
        encoded_fn = urllib.parse.quote(filename)
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded_fn}")
        self.end_headers()

        chunk_size = 256 * 1024
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            try:
                self.wfile.write(chunk)
            except BrokenPipeError:
                break

    def _proxy_download_zip(self, note_id, author, pub_date, xsec_token=""):
        """重新获取小红书笔记页面，打包所有图片为 ZIP"""
        try:
            # 通过 note_id 构造页面 URL 并重新获取（必须带 xsec_token）
            page_url = f"https://www.xiaohongshu.com/explore/{note_id}"
            if xsec_token:
                page_url += f"?xsec_token={urllib.parse.quote(xsec_token)}"
            html = xhs_fetch_page_html(page_url)
            state = xhs_parse_initial_state(html)
            info = xhs_extract_note_info(state, note_id)

            if info["note_type"] != "image":
                self._json(400, {"ok": False, "error": "该笔记不是图文类型"})
                return

            images = info["images"]
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, img in enumerate(images):
                    req = urllib.request.Request(img["url"], headers={
                        "User-Agent": DESKTOP_UA,
                        "Referer": XHS_REFERER,
                    })
                    resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=30)
                    img_data = resp.read()
                    ext = ".jpg"
                    ct = resp.headers.get("Content-Type", "")
                    if "png" in ct:
                        ext = ".png"
                    fname = f"{author}_{pub_date}_{i+1}{ext}".replace("/", "_")
                    zf.writestr(fname, img_data)

            zip_data = zip_buffer.getvalue()
            zip_filename = f"{author}_{pub_date}_全部图片.zip".replace("/", "_")

            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", len(zip_data))
            encoded_fn = urllib.parse.quote(zip_filename)
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded_fn}")
            self.end_headers()
            self.wfile.write(zip_data)

        except Exception as e:
            self._json(500, {"ok": False, "error": f"打包下载失败: {e}"})

    def _tk_extract_best_video_url(self, html: str) -> tuple:
        """从 TikTok 页面 HTML 中提取最佳画质视频地址。

        优先级：
        1. bitrateInfo 中 Bitrate 最高的 PlayAddr.UrlList[0]（去水印）
        2. 顶层 downloadAddr（去水印）
        3. 顶层 playAddr（去水印）

        返回 (url, quality_info_dict) 或 (None, None)
        """
        h = html.replace("\\u002F", "/").replace("\\u0026", "&") \
                 .replace("\\u003C", "<").replace("\\u003E", ">")

        # ---- 1) 尝试解析 bitrateInfo，选最高码率 ----
        best_url = None
        best_bitrate = 0
        best_info = None
        bm = re.search(r'"bitrateInfo"\s*(?::\s*)(\[.*?\])(?:\s*,|\s*\})', h, re.DOTALL)
        if bm:
            try:
                import json as _json
                # 修复 JSON 中可能的 trailing comma 等问题
                raw = bm.group(1).rstrip()
                if raw.endswith(','):
                    raw = raw[:-1]
                bitrates = _json.loads(raw)
                if isinstance(bitrates, list):
                    for item in bitrates:
                        if not isinstance(item, dict):
                            continue
                        br = item.get("Bitrate", 0)
                        if br > best_bitrate:
                            pa = item.get("PlayAddr", {})
                            urls = pa.get("UrlList", [])
                            if urls:
                                best_bitrate = br
                                best_url = urls[0].replace("/playwm/", "/play/")
                                best_info = {
                                    "bitrate": br,
                                    "gear_name": item.get("GearName", ""),
                                    "quality_type": item.get("QualityType", 0),
                                    "width": pa.get("Width", 0),
                                    "height": pa.get("Height", 0),
                                    "data_size": int(pa.get("DataSize", 0)),
                                }
            except Exception:
                pass

        if best_url:
            return best_url, best_info

        # ---- 2) 尝试 downloadAddr ----
        m = re.search(r'"downloadAddr"\s*:\s*"(https://[^"]+)"', h)
        if m:
            return m.group(1).replace("/playwm/", "/play/"), {"source": "downloadAddr"}

        # ---- 3) 回退到 playAddr ----
        m = re.search(r'"playAddr"\s*:\s*"(https://[^"]+)"', h)
        if m:
            return m.group(1).replace("/playwm/", "/play/"), {"source": "playAddr"}

        return None, None

    def tk_proxy_download(self, share_url: str, filename: str):
        """TikTok 无水印原画下载：同一 curl_cffi 会话��抓页面+下载。

        关键点：playAddr 直链绑定获取它的 cookie 会话，跨会话会被 Akamai 403。
        因此必须在一个 session 里完成「首页拿 ttwid → 页面拿最佳playAddr(去水印) →
        同会话下载」。fallback：官方失败时尝试 tikwm 第三方直链。
        """
        if not HAS_CURL_CFFI:
            self.send_error(500, "服务端缺少 curl_cffi 依赖")
            return

        UA = DESKTOP_UA
        sess = cffi_requests.Session(impersonate="chrome")
        # 1) 首页拿 ttwid cookie
        sess.get("https://www.tiktok.com/", headers={
            "User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"
        }, timeout=30)

        # 2) 抓页面，提取最佳画质地址
        page_html = sess.get(share_url, headers={
            "User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.tiktok.com/"
        }, timeout=30).text

        play_url, quality_info = self._tk_extract_best_video_url(page_html)

        # 3) 同会话下载（curl_cffi 不启用 stream，否则 resp.content 为空）
        video_bytes = None
        ctype = "video/mp4"
        if play_url:
            try:
                resp = sess.get(play_url, headers={
                    "User-Agent": UA,
                    "Accept": "video/webm,video/mp4,*/*;q=0.8",
                    "Referer": "https://www.tiktok.com/"
                }, timeout=120)
                if resp.status_code == 200 and resp.content[:4] not in (b"<HTM", b"<htm", b"<!DO"):
                    video_bytes = resp.content
                    ctype = resp.headers.get("Content-Type", "video/mp4")
                    # 记录实际下载大小
                    if quality_info:
                        quality_info["actual_size"] = len(resp.content)
            except Exception:
                video_bytes = None

        # 4) fallback：tikwm 第三方直链（画质略低但可用）
        if not video_bytes:
            try:
                import json as _json
                api = sess.get(
                    f"https://www.tikwm.com/api/?url={urllib.parse.quote(share_url)}",
                    headers={"User-Agent": UA, "Referer": "https://www.tikwm.com/"},
                    timeout=20
                )
                d = _json.loads(api.text)
                if d.get("code") == 0 and d.get("data", {}).get("play"):
                    dl = d["data"]["play"]
                    r2 = sess.get(dl, headers={"User-Agent": UA}, timeout=120)
                    if r2.status_code == 200 and r2.content[:4] not in (b"<HTM",):
                        video_bytes = r2.content
                        quality_info = {"source": "tikwm_fallback", "actual_size": len(r2.content)}
            except Exception:
                pass

        if not video_bytes:
            self.send_error(502, "TikTok video fetch failed (official and fallback both failed)")
            return

        # 构建更清晰的文件名（含画质信息）
        if quality_info and quality_info.get("gear_name"):
            gn = quality_info["gear_name"]
            if filename.endswith(".mp4"):
                filename = filename[:-4] + f"_{gn}.mp4"

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(video_bytes))
        enc = urllib.parse.quote(filename)
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{enc}")
        self.end_headers()
        self.wfile.write(video_bytes)

    def yt_proxy_download(self, share_url: str, filename: str, maxh: str = ""):
        """YouTube 最高清下载：服务端用 yt-dlp 抓取并合并音视频，流式返回。

        YouTube 音视频是分离的 DASH 流，必须下载后用 ffmpeg 合并才能得到完整最高清文件，
        因此无法像抖音/TikTok 那样直接代理直链。ffmpeg 用 imageio-ffmpeg 的静态二进制。
        多客户端自动重试：default(PO Token) → tv_embedded → android → ios → web

        maxh: 限制最高分辨率（如 "1080"）。4K 文件常有数百 MB，经免费服务器跨境中转
        非常慢，因此默认前端传 1080 以大幅缩短等待时间；留空表示不限（最高清）。
        """
        if not HAS_YTDLP:
            self.send_error(500, "Server missing yt-dlp dependency")
            return

        url = extract_url_from_text(share_url)
        tmp_dir = tempfile.mkdtemp(prefix="yt_")
        out_tmpl = os.path.join(tmp_dir, "%(id)s.%(ext)s")

        # 分辨率限制（仅在 maxh 为合法数字时生效）
        hf = f"[height<={int(maxh)}]" if maxh.isdigit() else ""
        # 依次尝试：最高清（任意编码，ffmpeg 合并为 mp4）> 单文件 mp4 > 任意 best
        formats_to_try = [
            f"bestvideo{hf}+bestaudio/best{hf}/best",
            f"best[ext=mp4]{hf}/best",
            "best",
        ]

        def _download(client, opts):
            """在指定客户端下尝试下载，返回 (info, filepath) 或抛异常。"""
            for fmt in formats_to_try:
                po = dict(opts)
                po.update({
                    "format": fmt,
                    "outtmpl": out_tmpl,
                    "merge_output_format": "mp4",
                    "noprogress": True,
                })
                with yt_dlp.YoutubeDL(po) as ydl:
                    info = ydl.extract_info(url, download=True)
                fp = (info.get("requested_downloads") or [{}])[0].get("filepath")
                if not fp or not os.path.exists(fp):
                    files = [os.path.join(tmp_dir, f) for f in os.listdir(tmp_dir)
                             if os.path.isfile(os.path.join(tmp_dir, f))]
                    fp = max(files, key=os.path.getsize) if files else None
                if fp and os.path.exists(fp):
                    return info, fp
            raise Exception("所有格式均下载失败")

        filepath = None
        last_err = None
        try:
            # 多客户端重试：每个客户端内再遍历格式列表
            for client in _YT_CLIENTS:
                try:
                    opts = _yt_base_opts(download=True, client=client)
                    _, fp = _download(client, opts)
                    if fp and os.path.exists(fp):
                        filepath = fp
                        break
                except Exception as e:
                    last_err = e
                    err_msg = str(e).lower()
                    if any(kw in err_msg for kw in [
                        "bot", "sign in", "confirm you're not", "429",
                        "too many requests", "forbidden", "concurrent",
                    ]):
                        continue
                    # 非反爬类错误直接终止
                    break

            if not filepath:
                self.send_error(502, f"YouTube download failed: {last_err}")
                return

            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            try:
                self.send_header("Content-Length", os.path.getsize(filepath))
            except Exception:
                pass
            enc = urllib.parse.quote(filename)
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{enc}")
            self.end_headers()
            # 分块流式写出，避免大文件（如 4K）占满内存 / 连接中断
            with open(filepath, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except Exception:
                        break
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    import os
    parser = argparse.ArgumentParser(description="去水印原视频下载 Web 版（抖音 / 小红书）")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8848)), help="端口号（默认 8848）")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"), help="监听地址")
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), Handler)
    print(f"╔════════════════════════════════════════════╗")
    print(f"║  🎬 去水印下载工具已启动（抖音 / 小红书）   ║")
    print(f"╠════════════════════════════════════════════╣")
    print(f"║  访问地址: http://{args.host}:{args.port:<26}║")
    print(f"║  按 Ctrl+C 停止服务                        ║")
    print(f"╚════════════════════════════════════════════╝")

    # 仅本地运行时自动打开浏览器
    if args.host == "127.0.0.1" and not os.environ.get("PORT"):
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{args.port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.server_close()


if __name__ == "__main__":
    main()
