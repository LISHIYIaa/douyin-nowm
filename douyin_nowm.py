#!/usr/bin/env python3
"""
抖音去水印原视频下载链接提取工具
输入：抖音视频分享链接（短链或长链）
输出：无水印原始高清视频下载链接

用法：
    python douyin_nowm.py <视频链接>
    python douyin_nowm.py <视频链接> --download [文件名]
    python douyin_nowm.py <视频链接> --info

示例：
    python douyin_nowm.py https://v.douyin.com/wnUiHlWzvsY/
    python douyin_nowm.py https://v.douyin.com/wnUiHlWzvsY/ --download 雾里偷喝汽水7.16.mp4
"""

import argparse
import json
import re
import ssl
import sys
import os
import urllib.request
import urllib.parse
from urllib.error import HTTPError, URLError


# ── 常量 ──────────────────────────────────────────────

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.6 Mobile/15E148 Safari/604.1"
)

DOUYIN_REFERER = "https://www.douyin.com/"

# 无水印播放接口（playwm → play 去水印）
PLAY_API = "https://aweme.snssdk.com/aweme/v1/play/"

# 质量档位：default = 原始最高清，720p = 压缩低清
QUALITY_DEFAULT = "default"
QUALITY_720P = "720p"

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


# ── 核心函数 ──────────────────────────────────────────

def resolve_share_url(share_url: str) -> str:
    """
    解析分享链接，返回包含 _ROUTER_DATA 的页面 URL。

    短链 v.douyin.com/xxx → 跳转到 iesdouyin.com/share/video/{id}/（含数据）
    长链 www.douyin.com/video/{id} → 需转换为 iesdouyin.com/share/video/{id}/
    """
    # 如果是短链，直接跟随重定向获取完整 share URL
    if "v.douyin.com" in share_url or "v.kuaishou" in share_url:
        req = urllib.request.Request(
            share_url,
            headers={"User-Agent": MOBILE_UA},
        )
        resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=15)
        return resp.geturl()

    # 如果已经是 douyin.com/video/{id} 格式，转换为 iesdouyin share 链接
    match = re.search(r"/video/(\d+)", share_url)
    if match:
        video_id = match.group(1)
        return f"https://www.iesdouyin.com/share/video/{video_id}/"

    # 如果已经是 iesdouyin share 链接，直接返回
    if "iesdouyin.com/share/video" in share_url:
        return share_url

    # 其他情况尝试跟随重定向
    req = urllib.request.Request(
        share_url,
        headers={"User-Agent": MOBILE_UA},
    )
    resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=15)
    return resp.geturl()


def extract_video_id(url: str) -> str:
    """从 URL 中提取视频 ID"""
    match = re.search(r"/video/(\d+)", url)
    if match:
        return match.group(1)
    match = re.search(r"/share/video/(\d+)", url)
    if match:
        return match.group(1)
    # 尝试从路径最后一段提取数字
    match = re.search(r"/(\d{15,})", url)
    if match:
        return match.group(1)
    raise ValueError(f"无法从 URL 提取视频 ID: {url}")


def fetch_page_html(share_url: str) -> str:
    """
    获取抖音视频页面 HTML。
    使用 iesdouyin.com share 链接（含 _ROUTER_DATA）。
    """
    req = urllib.request.Request(
        share_url,
        headers={
            "User-Agent": MOBILE_UA,
            "Referer": DOUYIN_REFERER,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=20)
    return resp.read().decode("utf-8", errors="replace")


def parse_router_data(html: str) -> dict:
    """从页面 HTML 中解析 _ROUTER_DATA JSON"""
    match = re.search(
        r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>",
        html,
        re.DOTALL,
    )
    if not match:
        raise ValueError("页面中未找到 _ROUTER_DATA")

    raw = match.group(1)
    # 解码 Unicode 转义
    decoded = (
        raw
        .replace("\\u002F", "/")
        .replace("\\u0026", "&")
        .replace("\\u003C", "<")
        .replace("\\u003E", ">")
    )
    return json.loads(decoded)


def extract_video_info(router_data: dict) -> dict:
    """从 _ROUTER_DATA 中提取视频信息"""
    loader = router_data.get("loaderData", {})

    # 查找包含 videoInfoRes 的页面数据
    for key, val in loader.items():
        if not isinstance(val, dict):
            continue
        video_info_res = val.get("videoInfoRes")
        if not video_info_res:
            continue

        item_list = video_info_res.get("item_list", [])
        if not item_list:
            continue

        item = item_list[0]
        video = item.get("video", {})
        aweme_id = item.get("aweme_id", "")
        desc = item.get("desc", "")
        author = item.get("author", {})
        nickname = author.get("nickname", "")
        statistics = item.get("statistics", {})

        play_addr = video.get("play_addr", {})
        uri = play_addr.get("uri", "")
        height = video.get("height", 0)
        width = video.get("width", 0)
        duration = video.get("duration", 0)  # 毫秒

        if not uri:
            raise ValueError("未能从页面数据中提取 video uri")

        return {
            "aweme_id": aweme_id,
            "desc": desc,
            "nickname": nickname,
            "video_uri": uri,
            "width": width,
            "height": height,
            "duration_ms": duration,
            "digg_count": statistics.get("digg_count", 0),
            "comment_count": statistics.get("comment_count", 0),
            "share_count": statistics.get("share_count", 0),
        }

    raise ValueError("未找到视频信息 (item_list 为空)")


def build_download_url(video_uri: str, quality: str = QUALITY_DEFAULT) -> str:
    """
    构建无水印下载链接

    关键点：
    1. 使用 /play/ 而非 /playwm/ → 去水印
    2. ratio=default → 获取原始最高清源视频
    3. ratio=720p   → 获取压缩低清版本
    """
    params = urllib.parse.urlencode({
        "video_id": video_uri,
        "ratio": quality,
        "line": 0,
    })
    return f"{PLAY_API}?{params}"


def probe_url(url: str) -> dict:
    """探测下载链接的 HTTP 状态、文件大小、类型"""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": MOBILE_UA, "Referer": DOUYIN_REFERER},
        method="HEAD",
    )
    try:
        resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=15)
        return {
            "status": resp.status,
            "content_type": resp.headers.get("Content-Type", ""),
            "content_length": int(resp.headers.get("Content-Length", 0)),
        }
    except HTTPError:
        # HEAD 可能不支持，用 GET 读 1 字节
        req = urllib.request.Request(url, headers={"User-Agent": MOBILE_UA, "Referer": DOUYIN_REFERER})
        resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=15)
        total = int(resp.headers.get("Content-Length", 0))
        resp.read(1)  # 只读 1 字节就关闭
        return {
            "status": resp.status,
            "content_type": resp.headers.get("Content-Type", ""),
            "content_length": total,
        }


def download_video(url: str, output_path: str, quiet: bool = False) -> str:
    """下载视频到指定路径"""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": MOBILE_UA, "Referer": DOUYIN_REFERER},
    )
    resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=120)
    total = int(resp.headers.get("Content-Length", 0))

    downloaded = 0
    chunk_size = 1024 * 1024  # 1MB

    with open(output_path, "wb") as f:
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if total and not quiet:
                pct = downloaded * 100 // total
                mb_done = downloaded / 1024 / 1024
                mb_total = total / 1024 / 1024
                print(
                    f"\r  下载进度: {mb_done:.1f}MB / {mb_total:.1f}MB ({pct}%)",
                    end="",
                    flush=True,
                )
    if not quiet:
        print()
    return output_path


# ── 格式化输出 ────────────────────────────────────────

def format_size(size_bytes: int) -> str:
    if size_bytes >= 1024 * 1024 * 1024:
        return f"{size_bytes / 1024 / 1024 / 1024:.2f} GB"
    elif size_bytes >= 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def format_duration(ms: int) -> str:
    seconds = ms // 1000
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"


def print_info(info: dict, source_url: str, no_wm_url: str, probe: dict | None):
    """打印视频信息"""
    print("=" * 60)
    print("  抖音去水印视频信息")
    print("=" * 60)
    print(f"  视频ID:   {info['aweme_id']}")
    print(f"  作者:     {info['nickname']}")
    print(f"  描述:     {info['desc'][:80]}")
    print(f"  分辨率:   {info['width']}×{info['height']}")
    print(f"  时长:     {format_duration(info['duration_ms'])}")
    print(f"  点赞:     {info['digg_count']:,}")
    print(f"  评论:     {info['comment_count']:,}")
    print(f"  分享:     {info['share_count']:,}")
    print("-" * 60)
    print(f"  原始链接: {source_url}")
    print(f"  下载链接: {no_wm_url}")
    if probe:
        print(f"  文件大小: {format_size(probe['content_length'])}")
        print(f"  文件类型: {probe['content_type']}")
    print("=" * 60)


# ── 主入口 ────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="抖音去水印原视频下载链接提取工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s https://v.douyin.com/wnUiHlWzvsY/
  %(prog)s https://v.douyin.com/wnUiHlWzvsY/ --download
  %(prog)s https://v.douyin.com/wnUiHlWzvsY/ --download 自定义文件名.mp4
  %(prog)s https://v.douyin.com/wnUiHlWzvsY/ --quality 720p
        """,
    )
    parser.add_argument("url", help="抖音视频分享链接（短链或长链）")
    parser.add_argument(
        "--download", "-d",
        nargs="?",
        const="auto",
        default=None,
        help="直接下载视频（可选指定文件名，默认自动命名）",
    )
    parser.add_argument(
        "--quality", "-q",
        choices=["default", "720p"],
        default="default",
        help="视频质量: default=原始高清(默认), 720p=压缩低清",
    )
    parser.add_argument(
        "--info", "-i",
        action="store_true",
        help="仅显示视频信息，不下载",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果",
    )

    args = parser.parse_args()

    # 1. 解析分享链接 → 获取含 _ROUTER_DATA 的页面 URL
    print(f"[1/4] 解析分享链接...")
    page_url = resolve_share_url(args.url)
    video_id = extract_video_id(page_url)
    print(f"      视频ID: {video_id}")

    # 2. 获取页面并解析
    print(f"[2/4] 获取视频页面数据...")
    html = fetch_page_html(page_url)
    router_data = parse_router_data(html)

    # 3. 提取视频信息
    print(f"[3/4] 提取无水印下载链接...")
    info = extract_video_info(router_data)
    download_url = build_download_url(info["video_uri"], args.quality)

    # 探测下载链接
    probe = probe_url(download_url)

    # 4. 输出结果
    print(f"[4/4] 完成!\n")

    if args.json:
        result = {
            "video_id": info["aweme_id"],
            "author": info["nickname"],
            "description": info["desc"],
            "resolution": f"{info['width']}x{info['height']}",
            "duration": format_duration(info["duration_ms"]),
            "download_url": download_url,
            "file_size": probe["content_length"],
            "file_size_human": format_size(probe["content_length"]),
            "content_type": probe["content_type"],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print_info(info, args.url, download_url, probe)

    # 下载
    if args.download:
        if args.download == "auto":
            # 自动命名：作者+描述前20字
            safe_desc = re.sub(r'[\\/:*?"<>|]', "_", info["desc"][:20])
            filename = f"{info['nickname']}_{safe_desc}.mp4"
        else:
            filename = args.download

        print(f"\n开始下载: {filename}")
        print(f"预计大小: {format_size(probe['content_length'])}\n")
        download_video(download_url, filename)
        print(f"下载完成: {filename} ({format_size(os.path.getsize(filename))})")


if __name__ == "__main__":
    main()
