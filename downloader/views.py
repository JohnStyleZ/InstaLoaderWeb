from django.shortcuts import render
from django.http import StreamingHttpResponse, HttpResponseBadRequest, HttpResponse
import os, base64, tempfile, requests, mimetypes, re
import instaloader
from urllib.parse import urlsplit, urlencode

# =========================
# Config / Feature toggles
# =========================
# We render previews via /proxy to avoid IG CDN hotlink issues.
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

def _sanitize_url(url: str) -> str:
    """Trim & strip query/fragment noise from an Instagram URL."""
    if not url:
        return ""
    url = url.strip()
    if "instagram.com" not in url:
        return ""
    parts = urlsplit(url)
    # remove ?query and #fragment
    clean = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return clean.rstrip("/")

def _parse_ig_url(url: str):
    """
    Returns (kind, token)
      kind ∈ {"post","story"}
      token:
        - for post: shortcode (e.g., DQ192xEEdMf)
        - for story: numeric media id (e.g., 3762806023376780800)

    Supported paths:
      /p/<shortcode>
      /reel/<shortcode>
      /tv/<shortcode>
      /stories/<username>/<mediaid>
      /stories/highlights/<mediaid>/...
    """
    if not url:
        return (None, None)
    path = urlsplit(url).path.rstrip("/")

    # stories/<username>/<mediaid>
    m = re.match(r"^/stories/[^/]+/(\d+)$", path)
    if m:
        return ("story", m.group(1))

    # stories/highlights/<mediaid>/...
    m = re.match(r"^/stories/highlights/(\d+)(?:/.*)?$", path)
    if m:
        return ("story", m.group(1))

    # post / reel / tv
    m = re.match(r"^/(p|reel|tv)/([^/]+)$", path)
    if m:
        return ("post", m.group(2))

    return (None, None)

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
    return L

def _collect_post_cdn_urls(post: instaloader.Post):
    """Return (images, videos) CDN URLs for posts/reels/TV (incl. sidecar)."""
    imgs, vids = [], []
    try:
        if post.typename == "GraphSidecar":
            for node in post.get_sidecar_nodes():
                if node.is_video and node.video_url:
                    vids.append(node.video_url)
                elif not node.is_video and node.display_url:
                    imgs.append(node.display_url)
        else:
            if post.is_video and getattr(post, "video_url", None):
                vids.append(post.video_url)
            elif getattr(post, "url", None):
                imgs.append(post.url)
    except Exception as e:
        print(f"[collect_post_cdn_urls] error: {e}")
    # de-dup
    imgs = list(dict.fromkeys(imgs))
    vids = list(dict.fromkeys(vids))
    return imgs, vids

def _collect_story_cdn_urls(L: instaloader.Instaloader, media_id: str):
    """Return (images, videos) CDN URLs for a single story media id."""
    imgs, vids = [], []
    try:
        item = instaloader.StoryItem.from_mediaid(L.context, int(media_id))
        if getattr(item, "video_url", None):
            vids.append(item.video_url)
        elif getattr(item, "url", None):
            imgs.append(item.url)
    except Exception as e:
        print(f"[collect_story_cdn_urls] error: {e}")
    return imgs, vids

def _make_media_pairs(urls: list[str]) -> list[dict]:
    """
    Build list of dicts for template:
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
    One box handles:
      - Post/Reel/TV URLs
      - Stories & Highlights URLs
    Renders proxied preview + single Download button.
    """
    images = []
    videos = []
    error = None

    if request.method == "POST":
        raw = (request.POST.get("postURL") or "").strip()
        cleaned = _sanitize_url(raw)
        kind, token = _parse_ig_url(cleaned)

        if not kind or not token:
            return render(request, "downloader/posts.html", {"error": "Invalid or unsupported Instagram URL."})

        try:
            print("FINDING MEDIA")
            L = _instaloader_with_env()

            if kind == "post":
                post = instaloader.Post.from_shortcode(L.context, token)
                img_cdn, vid_cdn = _collect_post_cdn_urls(post)
            else:
                img_cdn, vid_cdn = _collect_story_cdn_urls(L, token)

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
    """Keeps a separate page if you still link to /reels; same handling as /posts."""
    images = []
    videos = []
    error = None

    if request.method == "POST":
        raw = (request.POST.get("postURL") or "").strip()
        cleaned = _sanitize_url(raw)
        kind, token = _parse_ig_url(cleaned)

        if not kind or not token:
            return render(request, "downloader/reels.html", {"error": "Invalid or unsupported Instagram URL."})

        try:
            print("FINDING MEDIA")
            L = _instaloader_with_env()
            if kind == "post":
                post = instaloader.Post.from_shortcode(L.context, token)
                img_cdn, vid_cdn = _collect_post_cdn_urls(post)
            else:
                img_cdn, vid_cdn = _collect_story_cdn_urls(L, token)

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

    # Forward useful headers
    for h in ("Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified", "Cache-Control"):
        if h in r.headers:
            resp[h] = r.headers[h]

    # Force attachment only when requested
    if request.GET.get("download") == "1":
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'

    return resp
