from pathlib import Path
from django.conf import settings
from django.shortcuts import render
from django.http import StreamingHttpResponse, HttpResponseBadRequest, HttpResponse
import instaloader, os, base64, tempfile, requests, mimetypes
from urllib.parse import urlsplit, urlencode, quote

# =========================
# Config / Feature toggles
# =========================
# Always show previews via proxy so IG CDN can't block hotlinking.
PREVIEW_VIA_PROXY = os.environ.get("PREVIEW_VIA_PROXY", "true").lower() == "true"

# Allowlist of CDN hosts we will proxy. Add/remove as needed.
ALLOWED_CDN_HOSTS = {
    "scontent.cdninstagram.com",
    "scontent.xx.fbcdn.net",
    "instagram.fcdn.net",
}

# =================
# Helper functions
# =================

def _parse_shortcode(url: str) -> str | None:
    """Extract the shortcode from a post/reel/tv URL."""
    if not url:
        return None
    url = url.strip()
    if not url.startswith("https://www.instagram.com/"):
        return None
    parts = url.rstrip("/").split("/")
    # .../<type>/<shortcode>
    if len(parts) >= 2:
        return parts[-1]
    return None

def _instaloader_with_env() -> instaloader.Instaloader:
    """Configure Instaloader and load session from IG_SESSION_B64 if provided."""
    L = instaloader.Instaloader(
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        max_connection_attempts=1,
    )

    user = os.environ.get("IG_USER")
    session_b64 = os.environ.get("IG_SESSION_B64")

    if session_b64:
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.write(base64.b64decode(session_b64))
        tmp.flush(); tmp.close()
        try:
            L.load_session_from_file(user, tmp.name)
            print("Instaloader version:", instaloader.__version__)
            print("[instaloader] test_login:", L.test_login())
            print(f"[instaloader] loaded session for {user} (decoded from base64)")
        except Exception as e:
            print(f"[instaloader] session load failed: {e}")

    # Tip: do NOT set a custom IG_USER_AGENT / proxies unless required.
    return L

def _collect_cdn_urls(post: instaloader.Post) -> tuple[list[str], list[str]]:
    """
    Return (images, videos) as direct CDN URLs (no server download).
    Works for single media and sidecar (carousel) posts.
    """
    img_urls: list[str] = []
    vid_urls: list[str] = []

    try:
        if post.typename == "GraphSidecar":
            for node in post.get_sidecar_nodes():
                if node.is_video:
                    if node.video_url:
                        vid_urls.append(node.video_url)
                else:
                    if node.display_url:
                        img_urls.append(node.display_url)
        else:
            if post.is_video:
                if post.video_url:
                    vid_urls.append(post.video_url)
            else:
                if post.url:
                    img_urls.append(post.url)
    except Exception as e:
        print(f"[collect_cdn_urls] error: {e}")

    # De-dup just in case
    img_urls = list(dict.fromkeys(img_urls))
    vid_urls = list(dict.fromkeys(vid_urls))
    return img_urls, vid_urls

def _wrap_proxy(urls: list[str], *, download: bool = False) -> list[str]:
    """Convert CDN URLs to /proxy URLs for same-origin preview/download."""
    if not urls:
        return urls
    base = "/proxy"
    out = []
    for u in urls:
        q = urlencode({"u": u, "download": "1" if download else "0"})
        out.append(f"{base}?{q}")
    return out

def _host_allowed(url: str) -> bool:
    try:
        host = urlsplit(url).hostname or ""
        return any(host.endswith(h) for h in ALLOWED_CDN_HOSTS)
    except Exception:
        return False

# =========
#  Views
# =========

def index(request):
    return render(request, "downloader/index.html")

def posts(request):
    """
    POST field name="postURL" with a post/reel URL.
    Does NOT write to MEDIA_ROOT; renders proxied URLs for reliable preview.
    """
    images = videos = []
    error = None

    if request.method == "POST":
        url = (request.POST.get("postURL") or "").strip()
        shortcode = _parse_shortcode(url)
        if not shortcode:
            return render(request, "downloader/posts.html", {"error": "Invalid URL."})

        try:
            print("FINDING POST")
            L = _instaloader_with_env()
            post = instaloader.Post.from_shortcode(L.context, shortcode)
            print("FOUND")

            img_cdn, vid_cdn = _collect_cdn_urls(post)
            print("CDN URLS:", img_cdn, vid_cdn)

            # For preview, always proxy (same-origin). "Open" links in the template can still go direct.
            images = _wrap_proxy(img_cdn) if PREVIEW_VIA_PROXY else img_cdn
            videos = _wrap_proxy(vid_cdn) if PREVIEW_VIA_PROXY else vid_cdn

            if not (img_cdn or vid_cdn):
                error = ("Could not obtain media URLs. "
                         "Session may be unauthenticated or rate-limited.")
        except Exception as e:
            error = f"An error occurred: {e}"

    return render(
        request,
        "downloader/posts.html",
        {"data": bool(images or videos), "images": images, "videos": videos, "error": error},
    )

def reels(request):
    """
    Same flow as posts(), but renders reels.html.
    """
    images = videos = []
    error = None

    if request.method == "POST":
        url = (request.POST.get("postURL") or "").strip()
        shortcode = _parse_shortcode(url)
        if not shortcode:
            return render(request, "downloader/reels.html", {"error": "Invalid URL."})

        try:
            print("FINDING POST")
            L = _instaloader_with_env()
            post = instaloader.Post.from_shortcode(L.context, shortcode)
            print("FOUND")

            img_cdn, vid_cdn = _collect_cdn_urls(post)
            print("CDN URLS:", img_cdn, vid_cdn)

            images = _wrap_proxy(img_cdn) if PREVIEW_VIA_PROXY else img_cdn
            videos = _wrap_proxy(vid_cdn) if PREVIEW_VIA_PROXY else vid_cdn

            if not (img_cdn or vid_cdn):
                error = ("Could not obtain media URLs. "
                         "Session may be unauthenticated or rate-limited.")
        except Exception as e:
            error = f"An error occurred: {e}"

    return render(
        request,
        "downloader/reels.html",
        {"data": bool(images or videos), "images": images, "videos": videos, "error": error},
    )

# ===========================
# Proxy endpoint (preview & DL)
# ===========================

def proxy(request):
    """
    Stream a remote Instagram CDN file via same-origin.
    - Supports HTTP Range (video scrubbing)
    - Doesn’t force download unless ?download=1
    """
    src = request.GET.get("u")
    if not src:
        return HttpResponseBadRequest("Missing url")
    if not _host_allowed(src):
        return HttpResponseBadRequest("Host not allowed")

    # Forward Range for videos
    headers = {}
    if "Range" in request.headers:
        headers["Range"] = request.headers["Range"]

    try:
        r = requests.get(src, stream=True, timeout=20, headers=headers)
    except Exception as e:
        return HttpResponseBadRequest(f"Fetch failed: {e}")

    if r.status_code not in (200, 206):
        return HttpResponse(f"Upstream returned {r.status_code}", status=r.status_code)

    path = urlsplit(src).path
    filename = os.path.basename(path) or "file"
    ctype = r.headers.get("Content-Type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"

    resp = StreamingHttpResponse(r.iter_content(8192), content_type=ctype, status=r.status_code)

    # Pass through useful headers
    for h in ("Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified", "Cache-Control"):
        if h in r.headers:
            resp[h] = r.headers[h]

    # Force download only when requested
    dl = (request.GET.get("download") == "1")
    if dl:
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'

    return resp
