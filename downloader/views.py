from django.shortcuts import render
from django.http import StreamingHttpResponse, HttpResponseBadRequest, HttpResponse
import os, base64, tempfile, requests, mimetypes
import instaloader
from urllib.parse import urlsplit, urlencode

# =========================
# Config / Feature toggles
# =========================
# We always render previews via our /proxy to avoid IG CDN hotlink issues.
PREVIEW_VIA_PROXY = os.environ.get("PREVIEW_VIA_PROXY", "true").lower() == "true"

# Allow common Instagram/Facebook regional CDN hosts.
ALLOWED_CDN_SUBSTRINGS = (
    ".cdninstagram.com",
    ".fbcdn.net",
    ".fna.fbcdn.net",
)

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
    # Handle .../<type>/<shortcode>
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
        tmp.flush()
        tmp.close()
        try:
            L.load_session_from_file(user, tmp.name)
            print("Instaloader version:", instaloader.__version__)
            print("[instaloader] test_login:", L.test_login())
            print(f"[instaloader] loaded session for {user} (decoded from base64)")
        except Exception as e:
            print(f"[instaloader] session load failed: {e}")

    return L

def _collect_cdn_urls(post: instaloader.Post) -> tuple[list[str], list[str]]:
    """Return (images, videos) as direct CDN URLs (no server download)."""
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

    # de-dup
    img_urls = list(dict.fromkeys(img_urls))
    vid_urls = list(dict.fromkeys(vid_urls))
    return img_urls, vid_urls

def _make_media_pairs(urls: list[str]) -> list[dict]:
    """
    Build list:
      [{"preview": "/proxy?...download=0", "download": "/proxy?...download=1"}, ...]
    """
    items: list[dict] = []
    for u in urls or []:
        q_prev = urlencode({"u": u, "download": "0"})
        q_dl   = urlencode({"u": u, "download": "1"})
        items.append({"preview": f"/proxy?{q_prev}", "download": f"/proxy?{q_dl}"})
    return items

def _host_allowed(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
        return any(sub in host for sub in ALLOWED_CDN_SUBSTRINGS)
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
    No media is stored on server; we render proxied preview + single Download button.
    """
    images = []
    videos = []
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

            images = _make_media_pairs(img_cdn)
            videos = _make_media_pairs(vid_cdn)

            if not (img_cdn or vid_cdn):
                error = "Could not obtain media URLs. Login may be required or rate-limited."
        except Exception as e:
            error = f"An error occurred: {e}"

    return render(
        request,
        "downloader/posts.html",
        {"data": bool(images or videos), "images": images, "videos": videos, "error": error},
    )

def reels(request):
    """Same as posts(), but renders reels.html."""
    images = []
    videos = []
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

            images = _make_media_pairs(img_cdn)
            videos = _make_media_pairs(vid_cdn)

            if not (img_cdn or vid_cdn):
                error = "Could not obtain media URLs. Login may be required or rate-limited."
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
    - Forces download only when ?download=1
    """
    src = request.GET.get("u")
    if not src:
        return HttpResponseBadRequest("Missing url")
    if not _host_allowed(src):
        return HttpResponseBadRequest("Host not allowed")

    # Pass through Range header for video scrubbing
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

    # forward useful headers
    for h in ("Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified", "Cache-Control"):
        if h in r.headers:
            resp[h] = r.headers[h]

    # force attachment only when requested
    if request.GET.get("download") == "1":
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'

    return resp
