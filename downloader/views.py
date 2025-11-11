from pathlib import Path
from django.conf import settings
from django.shortcuts import render
from django.http import HttpResponse, HttpResponseBadRequest
import instaloader, os, base64, tempfile, requests
from urllib.parse import urlparse, urlsplit, urlencode

# ---------- helpers ----------

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

    # Tip: do NOT set custom IG_USER_AGENT / proxies unless required.
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
                    # video_url may be None if IG blocked access; requires valid session
                    if node.video_url:
                        vid_urls.append(node.video_url)
                else:
                    # display_url is the full-size image URL
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

# ---------- views ----------

def index(request):
    return render(request, "downloader/index.html")

def posts(request):
    """
    POST field name="postURL" with a post/reel URL.
    Does NOT write to MEDIA_ROOT; renders direct CDN URLs for preview/download.
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

            images, videos = _collect_cdn_urls(post)
            print("CDN URLS:", images, videos)

            if not images and not videos:
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

            images, videos = _collect_cdn_urls(post)
            print("CDN URLS:", images, videos)

            if not images and not videos:
                error = ("Could not obtain media URLs. "
                         "Session may be unauthenticated or rate-limited.")
        except Exception as e:
            error = f"An error occurred: {e}"

    return render(
        request,
        "downloader/reels.html",
        {"data": bool(images or videos), "images": images, "videos": videos, "error": error},
    )

# ---------- OPTIONAL: server proxy for downloads ----------

ALLOW_PROXY = os.environ.get("ALLOW_PROXY_DOWNLOADS", "false").lower() == "true"

def proxy(request):
    """
    Optional endpoint to stream a remote file through your server, so the browser
    gets a same-origin download (works around cross-origin 'download' restrictions).
    Enable by setting ALLOW_PROXY_DOWNLOADS=true env var.
    """
    if not ALLOW_PROXY:
        return HttpResponseBadRequest("Proxy disabled")

    src = request.GET.get("u")
    if not src:
        return HttpResponseBadRequest("Missing url")
    try:
        r = requests.get(src, stream=True, timeout=20)
        r.raise_for_status()
    except Exception as e:
        return HttpResponseBadRequest(f"Fetch failed: {e}")

    # Try to keep the content-type, and set a download filename from path
    ctype = r.headers.get("Content-Type", "application/octet-stream")
    name = os.path.basename(urlsplit(src).path) or "download"
    resp = HttpResponse(r.iter_content(chunk_size=8192), content_type=ctype)
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp
