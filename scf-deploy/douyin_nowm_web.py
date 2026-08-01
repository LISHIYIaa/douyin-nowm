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
import re
import ssl
import zipfile
import urllib.request
import urllib.parse
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

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

def dy_resolve_share_url(share_url: str) -> str:
    if "v.douyin.com" in share_url:
        req = urllib.request.Request(share_url, headers={"User-Agent": MOBILE_UA})
        resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=15)
        return resp.geturl()
    match = re.search(r"/video/(\d+)", share_url)
    if match:
        vid = match.group(1)
        return f"https://www.iesdouyin.com/share/video/{vid}/"
    if "iesdouyin.com/share/video" in share_url:
        return share_url
    req = urllib.request.Request(share_url, headers={"User-Agent": MOBILE_UA})
    resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=15)
    return resp.geturl()


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


def process_douyin(share_url: str, quality: str = "default") -> dict:
    page_url = dy_resolve_share_url(share_url)
    html = dy_fetch_page_html(page_url)
    router = dy_parse_router_data(html)
    info = dy_extract_video_info(router)
    dl_url = dy_build_download_url(info["video_uri"], quality)
    probe = dy_probe_url(dl_url)
    return {
        "platform": "douyin",
        "ok": True,
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
        "file_size": probe["content_length"],
        "file_size_human": format_size(probe["content_length"]),
        "content_type": probe["content_type"],
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


def xhs_fetch_page_html(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": DESKTOP_UA,
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
    """从 __INITIAL_STATE__ 中提取笔记信息（视频或图文）"""
    note_map = initial_state.get("note", {}).get("noteDetailMap", {})
    note_data = note_map.get(note_id)
    if not note_data:
        if note_map:
            note_id = list(note_map.keys())[0]
            note_data = note_map[note_id]
        else:
            raise ValueError("小红书页面中未找到笔记数据")

    note = note_data.get("note", {})
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

    base = {
        "note_id": note_id,
        "note_type": note_type,
        "title": note.get("title", ""),
        "desc": note.get("desc", ""),
        "nickname": note.get("user", {}).get("nickname", ""),
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

    # 检查 noteDetailMap 是否有数据（小红书改版后需要 xsec_token）
    note_map = state.get("note", {}).get("noteDetailMap", {})
    if not note_map:
        # 检查是否被重定向到 404 页面
        if "/404" in html[:5000]:
            raise ValueError("该小红书笔记可能已被删除或链接已失效")
        # 检查 URL 中是否缺少 xsec_token
        if "xsec_token" not in page_url:
            raise ValueError(
                "小红书改版后需要完整的分享链接（含 xsec_token 参数）。\n"
                "请在小红书 App 中点击「分享」→「复制链接」，粘贴完整的分享文本。"
            )
        raise ValueError("未能获取小红书笔记数据，链接中的 xsec_token 可能已过期，请重新分享获取新链接")

    info = xhs_extract_note_info(state, note_id)

    if info["note_type"] == "video":
        probe = xhs_probe_url(info["download_url"])
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
            "file_size": probe["content_length"],
            "file_size_human": format_size(probe["content_length"]),
            "content_type": probe["content_type"],
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

def process_link(share_url: str, quality: str = "default") -> dict:
    # 自动从分享文本中提取 URL
    url = extract_url_from_text(share_url)
    platform = detect_platform(url)
    if platform == "douyin":
        return process_douyin(url, quality)
    elif platform == "xhs":
        return process_xhs(url)
    raise ValueError("不支持的平台")


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
    cursor: pointer;
    text-decoration: none;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    transition: opacity .2s, transform .1s;
  }
  .btn-download:hover { opacity: .85; }
  .btn-download:active { transform: scale(.95); }

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
        <input type="text" id="url-input" placeholder="粘贴抖音分享链接..." autofocus>
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

  if (p === 'xhs') {
    urlInput.placeholder = '粘贴小红书分享链接...';
    btnParse.classList.add('xhs');
    urlInput.classList.add('xhs');
    qualityRow.classList.add('disabled');
  } else {
    urlInput.placeholder = '粘贴抖音分享链接...';
    btnParse.classList.remove('xhs');
    urlInput.classList.remove('xhs');
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
function clearError() { errorEl.innerHTML = ''; }

function formatNum(n) {
  if (!n) return '0';
  if (n >= 10000) return (n / 10000).toFixed(1) + '万';
  return n.toLocaleString();
}

async function parseLink() {
  const url = urlInput.value.trim();
  if (!url) { showError('请输入视频链接'); return; }

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
        const imgFilename = (data.author + '_' + (data.pub_date || '') + '_' + (i+1) + '.jpg').replace(/[\\/:*?"<>|#]/g, '_');
        imgCards += `
          <div class="img-card">
            <img class="img-thumb" src="${img.url}" alt="图${i+1}" referrerpolicy="no-referrer" loading="lazy">
            <div class="img-info">
              <span class="img-dim">${img.width}x${img.height}</span>
              <span class="img-size">${img.file_size_human}</span>
            </div>
            <a class="btn-img-download" href="/api/download?url=${encodeURIComponent(img.url)}&platform=xhs&filename=${encodeURIComponent(imgFilename)}" download>⬇</a>
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
      // 视频笔记
      const filename = (data.author + '_' + (data.pub_date || '') + '.mp4').replace(/[\\/:*?"<>|#]/g, '_');

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
              <a class="btn-download" href="/api/download?url=${encodeURIComponent(data.download_url)}&platform=${data.platform}&filename=${encodeURIComponent(filename)}">
                ⬇ 下载
              </a>
            </div>
            <div class="copy-row">
              <input class="copy-input" id="dl-url" value="${data.download_url}" readonly>
              <button class="btn-copy" onclick="copyLink(this)">复制链接</button>
            </div>
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

        elif parsed.path == "/api/download":
            qs = urllib.parse.parse_qs(parsed.query)
            dl_url = qs.get("url", [""])[0]
            filename = qs.get("filename", ["video.mp4"])[0]
            platform = qs.get("platform", ["douyin"])[0]
            if not dl_url:
                self.send_error(400, "Missing url")
                return
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
