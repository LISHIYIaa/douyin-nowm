# -*- coding: utf-8 -*-
"""腾讯云 SCF 事件函数入口（API 网关触发）

返回格式兼容 SCF 同步调用：
{
  "isBase64Encoded": bool,
  "statusCode": int,
  "headers": dict,
  "body": str
}
"""
import base64
import io
import json
import urllib.parse
import urllib.request
import zipfile

from douyin_nowm_web import (
    HTML_PAGE,
    process_link,
    xhs_fetch_page_html,
    xhs_parse_initial_state,
    xhs_extract_note_info,
    SSL_CTX,
    MOBILE_UA,
    DESKTOP_UA,
    DOUYIN_REFERER,
    XHS_REFERER,
)

# SCF 事件函数返回体限制约 6MB，base64 后膨胀 33%，安全阈值设为 4MB
MAX_PROXY_SIZE = 4 * 1024 * 1024


def _resp(status, body, content_type, extra_headers=None, binary=False):
    headers = {"Content-Type": content_type}
    if extra_headers:
        headers.update(extra_headers)

    if binary:
        if isinstance(body, str):
            body = body.encode("utf-8")
        return {
            "isBase64Encoded": True,
            "statusCode": status,
            "headers": headers,
            "body": base64.b64encode(body).decode("ascii"),
        }
    else:
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        return {
            "isBase64Encoded": False,
            "statusCode": status,
            "headers": headers,
            "body": body,
        }


def _proxy_download(dl_url, filename, platform="douyin"):
    """代理下载；返回二进制文件内容"""
    ua = DESKTOP_UA if platform == "xhs" else MOBILE_UA
    referer = XHS_REFERER if platform == "xhs" else DOUYIN_REFERER

    req = urllib.request.Request(dl_url, headers={"User-Agent": ua, "Referer": referer})
    try:
        resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=120)
    except Exception as e:
        return _resp(502, json.dumps({"ok": False, "error": f"下载失败: {e}"}, ensure_ascii=False), "application/json")

    data = resp.read()
    content_type = resp.headers.get("Content-Type", "application/octet-stream")
    if filename.lower().endswith((".jpg", ".jpeg")):
        content_type = "image/jpeg"
    elif filename.lower().endswith(".png"):
        content_type = "image/png"
    elif filename.lower().endswith(".mp4"):
        content_type = "video/mp4"

    encoded_fn = urllib.parse.quote(filename)
    return _resp(
        200,
        data,
        content_type,
        {"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_fn}"},
        binary=True,
    )


def _proxy_download_zip(note_id, author, pub_date, xsec_token="", xsec_source="pc_feed"):
    """重新获取小红书笔记并打包图片（需要 xsec_token 才能获取笔记数据）"""
    try:
        # 构造带 xsec_token 的完整 URL
        page_url = f"https://www.xiaohongshu.com/explore/{note_id}"
        if xsec_token:
            page_url += f"?xsec_token={urllib.parse.quote(xsec_token)}&xsec_source={xsec_source}"
        html = xhs_fetch_page_html(page_url)
        state = xhs_parse_initial_state(html)

        # 检查 noteDetailMap 是否有数据
        note_map = state.get("note", {}).get("noteDetailMap", {})
        if not note_map:
            return _resp(400, json.dumps({"ok": False, "error": "笔记数据获取失败，链接中的 xsec_token 可能已过期，请重新解析后再下载"}, ensure_ascii=False), "application/json")

        info = xhs_extract_note_info(state, note_id)

        if info["note_type"] != "image":
            return _resp(400, json.dumps({"ok": False, "error": "该笔记不是图文类型"}, ensure_ascii=False), "application/json")

        images = info["images"]
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, img in enumerate(images):
                req = urllib.request.Request(img["url"], headers={"User-Agent": DESKTOP_UA, "Referer": XHS_REFERER})
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
        encoded_fn = urllib.parse.quote(zip_filename)
        return _resp(
            200,
            zip_data,
            "application/zip",
            {"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_fn}"},
            binary=True,
        )
    except Exception as e:
        return _resp(500, json.dumps({"ok": False, "error": f"打包下载失败: {e}"}, ensure_ascii=False), "application/json")


def main_handler(event, context):
    method = event.get("httpMethod", "GET")
    path = event.get("path", "/")
    query = event.get("queryString", {}) or {}
    body = event.get("body", "") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")

    # CORS 预检
    if method == "OPTIONS":
        return _resp(204, "", "text/plain", {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        })

    # 首页
    if method == "GET" and path in ("/", "/index.html"):
        return _resp(200, HTML_PAGE, "text/html; charset=utf-8", {
            "Content-Disposition": "inline",
            "Access-Control-Allow-Origin": "*",
        })

    # 解析接口
    if method == "POST" and path == "/api/parse":
        try:
            data = json.loads(body)
            url = data.get("url", "").strip()
            quality = data.get("quality", "default")
            if not url:
                return _resp(400, json.dumps({"ok": False, "error": "请输入链接"}, ensure_ascii=False), "application/json")
            result = process_link(url, quality)
            return _resp(200, json.dumps(result, ensure_ascii=False), "application/json; charset=utf-8", {
                "Content-Disposition": "inline",
                "Access-Control-Allow-Origin": "*",
            })
        except Exception as e:
            return _resp(500, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), "application/json")

    # 单文件下载代理
    if method == "GET" and path == "/api/download":
        dl_url = query.get("url", "")
        filename = query.get("filename", "download")
        platform = query.get("platform", "douyin")
        if not dl_url:
            return _resp(400, json.dumps({"ok": False, "error": "缺少 url 参数"}, ensure_ascii=False), "application/json")
        return _proxy_download(dl_url, filename, platform)

    # 图文 ZIP 打包下载
    if method == "GET" and path == "/api/download_zip":
        note_id = query.get("note_id", "")
        author = query.get("author", "author")
        pub_date = query.get("pub_date", "")
        xsec_token = query.get("xsec_token", "")
        xsec_source = query.get("xsec_source", "pc_feed")
        if not note_id:
            return _resp(400, json.dumps({"ok": False, "error": "缺少 note_id 参数"}, ensure_ascii=False), "application/json")
        return _proxy_download_zip(note_id, author, pub_date, xsec_token, xsec_source)

    return _resp(404, json.dumps({"ok": False, "error": "Not Found"}, ensure_ascii=False), "application/json")
