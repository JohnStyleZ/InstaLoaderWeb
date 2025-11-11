from pathlib import Path
from django.conf import settings
from django.shortcuts import render
from django.http import StreamingHttpResponse, HttpResponseBadRequest, HttpResponse
import instaloader, os, base64, tempfile, requests, mimetypes, re
from urllib.parse import urlsplit, urlencode

# =========================
# Config / Feature toggles
# =========================
# Always show previews via proxy so IG CDN can't block hotlinking.
PREVIEW_VIA_PROXY = os.environ.get("PREVIEW_VIA_PROXY", "true").lower() == "true"

# Allow any Instagram/Facebook CDN edge host (regionalized).
# We match by substring so hosts like instagram.fhnl2-1.fna.fbcdn.net are allowed.
ALLOWED_CDN_SUBSTRINGS = (
    ".cdninstagram.com",
    ".fbcdn.net",
    ".fna.fbcdn.net",
)

# =================
# Helper functions
# =================

def _parse_ig_target(raw_url: str):
    """
    Detect whether the URL is a post/reel/tv (return type='post' and shortcode)
    or a story item (return type='story' and media_id).
    Returns a dict like:
      {"type": "post", "shortcode": "..."}  OR  {"type": "story", "media_id": "...", "username": "..."}
    On failure, returns None.
    """
    if not raw_url:
        return None

    raw_url = raw_url.strip()
    if not raw_url.startswith("https://www.instagram.com/"):
        return None

    # Strip query/fragment safely
    parts = urlsplit(raw_url)
    path = parts.path.rstrip("/")  # e.g. /p/ABC123 or /stories/user/1234567890
    segments = [seg for seg in path.split("/") if seg]  # remove empty

    if not segments:
        return None

    # Stories URL: /stories/<username>/<media_id>
    if len(segments) >= 3 and segments[0] == "stories":
        username = segments[1]
        media_id = segments[2]
        if not media_id.isdigit():
            return None
        return {"type": "story", "media_id": media_id, "username": username}

    # Post/Reel/TV URL: /p/<shortcode>, /reel/<shortcode>, /tv/<shortcode>
    if len(segments) >= 2 and segments[0] in {"p", "reel", "tv"}:
        shortcode = segments[1]
        # Shortcodes are typically alphanumeric + underscore; be lenient
        if re.match(r"^[A-Za-z0-9_-]+$", shortcode):
            return {"type": "post", "shortcode": shortcode}

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


def _collect_cdn_urls_from_post(post: instaloader.Post) -> tuple[list[str], list[str]]:
    """
    Return (images, videos) as direct CDN URLs from a Post (no server download).
    Handles single media and sidecars.
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
        print(f"[collect_cdn_urls_from_post] error: {e}")

    # De-dup
    img_urls = list(dict.fromkeys(img_urls))
    vid_urls = list(dict.fromkeys(vid_urls))
    return img_urls, vid_urls


def _collect_cdn_urls_from_storyitem(item) -> tuple[list[str], list[str]]:
    """
    Return (images, videos) from a StoryItem.
    """
    imgs, vids = [], []
    try:
        if getattr(item, "is_video", False):
            if getattr(item, "video_url", None):
                vids.append(item.video_url)
        else:
            if getattr(item, "url", None):
                imgs.append(item.url)
    except Exception as e:
        print(f"[collect_cdn_urls_from_storyitem] error: {e}")
    return imgs, vids


def _wrap_proxy(urls: list[str], *, download: bool = False) -> list[str]:
    """Convert CDN URLs to /proxy URLs for same-origin preview/download. Skip if already proxied."""
    out = []
    for u in urls or []:
        if not u:
            continue
        if u.startswith("/proxy?"):
            out.append(u)  # already proxied
        else:
            q = urlencode({"u": u, "download": "1" if download else "0"})
            out.append(f"/proxy?{q}")
    return out


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
    Unified endpoint:
      - If user pastes a Post/Reel/TV URL => fetch via shortcode
      - If user pastes a Story URL     => fetch via media_id
    Renders proxied URLs for reliable preview + a single "Download" button.
    """
    images = videos = []
    error = None

    if request.method == "POST":
        raw_url = (request.POST.get("postURL") or "").strip()
        target = _parse_ig_target(raw_url)
        if not target:
            return render(request, "downloader/posts.html", {"error": "Invalid URL."})

        try:
            print("FINDING MEDIA")
            L = _instaloader_with_env()

            if target["type"] == "post":
                post = instaloader.Post.from_shortcode(L.context, target["shortcode"])
                print("FOUND POST/REEL")
                img_cdn, vid_cdn = _collect_cdn_urls_from_post(post)

            else:  # story
                media_id = int(target["media_id"])
                # StoryItem is available through instaloader (requires login + visibility)
                story_item = instaloader.StoryItem.from_mediaid(L.context, media_id)
                print("FOUND STORY")
                img_cdn, vid_cdn = _collect_cdn_urls_from_storyitem(story_item)

            print("CDN URLS:", img_cdn, vid_cdn)

            images = _wrap_proxy(img_cdn) if PREVIEW_VIA_PROXY else img_cdn
            videos = _wrap_proxy(vid_cdn) if PREVIEW_VIA_PROXY else vid_cdn

            if not (img_cdn or vid_cdn):
                error = ("Could not obtain media URLs. "
                         "Story may be expired/private, or session may lack access.")
        except Exception as e:
            error = f"An error occurred: {e}"

    return render(
        request,
        "downloader/posts.html",
        {"data": bool(images or videos), "images": images, "videos": videos, "error": error},
    )


def reels(request):
    """
    (Kept for backward compatibility) Post/Reel flow identical to posts(), but without story parsing.
    """
    images = videos = []
    error = None

    if request.method == "POST":
        raw_url = (request.POST.get("postURL") or "").strip()
        target = _parse_ig_target(raw_url)
        if not target or target["type"] != "post":
            return render(request, "downloader/reels.html", {"error": "Invalid Reel URL."})

        try:
            print("FINDING POST/REEL")
            L = _instaloader_with_env()
            post = instaloader.Post.from_shortcode(L.context, target["shortcode"])
            print("FOUND")
            img_cdn, vid_cdn = _collect_cdn_urls_from_post(post)
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
    if request.GET.get("download") == "1":
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'

    return resp
