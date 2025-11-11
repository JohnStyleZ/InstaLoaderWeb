from pathlib import Path
from django.conf import settings
from django.shortcuts import render
from django.http import JsonResponse, HttpResponseBadRequest
import instaloader, os, shutil, threading, time, base64, tempfile

# ---------- helpers ----------

def _parse_shortcode(url: str) -> str | None:
    """Extract the shortcode from a post/reel URL."""
    if not url:
        return None
    url = url.strip()
    if not url.startswith("https://www.instagram.com/"):
        return None
    parts = url.rstrip("/").split("/")
    # /p/<shortcode>/ or /reel/<shortcode>/ or /tv/<shortcode>/
    if len(parts) >= 2:
        return parts[-1]
    return None

def _instaloader_with_env(media_root: Path) -> instaloader.Instaloader:
    L = instaloader.Instaloader(
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        max_connection_attempts=1,
        dirname_pattern=str(media_root)
    )

    user = os.environ.get("IG_USER")
    session_b64 = os.environ.get("IG_SESSION_B64")
    session_path = None  # prefer B64

    if session_b64:
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.write(base64.b64decode(session_b64))
        tmp.flush(); tmp.close()
        session_path = tmp.name

    if user and session_path and Path(session_path).exists():
        try:
            L.load_session_from_file(user, session_path)
            print("Instaloader version:", instaloader.__version__)
            print("[instaloader] test_login:", L.test_login())
            who = L.test_login()
            print("[instaloader] test_login:", who)
            print(f"[instaloader] loaded session for {user} (decoded from base64)")
        except Exception as e:
            print(f"[instaloader] session load failed: {e}")

    # Optional UA / proxies
    ua = os.environ.get("IG_USER_AGENT")
    if ua:
        L.context.user_agent = ua
    http_proxy = os.environ.get("HTTP_PROXY", "")
    https_proxy = os.environ.get("HTTPS_PROXY", "")
    if http_proxy or https_proxy:
        L.context._session.proxies.update({"http": http_proxy, "https": https_proxy})

    return L

def _collect_public_urls(media_root: Path, shortcode: str | None) -> tuple[list[str], list[str]]:
    """Return public /media/ URLs for images and videos just written into MEDIA_ROOT."""
    exts_img = {".png", ".jpg", ".jpeg", ".gif"}
    exts_vid = {".mp4", ".avi", ".mov", ".wmv"}

    # Prefer files that include the shortcode in their filename
    candidates = []
    if shortcode:
        candidates = list(media_root.glob(f"*{shortcode}*"))

    # Fallback: files modified in the last 2 minutes
    if not candidates:
        now = time.time()
        candidates = [p for p in media_root.glob("*") if (now - p.stat().st_mtime) < 120]

    images, videos = [], []
    for p in sorted(candidates):
        if p.suffix.lower() in exts_img | exts_vid:
            rel = p.relative_to(media_root).as_posix()
            url = f"{settings.MEDIA_URL}{rel}"
            if p.suffix.lower() in exts_img:
                images.append(url)
            else:
                videos.append(url)
    return images, videos

# ---------- views ----------

def index(request):
    return render(request, "downloader/index.html")

def posts(request):
    """
    POST form field: name="postURL" with a post/reel URL.
    Downloads into MEDIA_ROOT and returns template with /media/... URLs.
    """
    images = videos = []
    error = None

    if request.method == "POST":
        url = str(request.POST.get("postURL", "")).strip()
        shortcode = _parse_shortcode(url)
        if not shortcode:
            return render(request, "downloader/posts.html", {"error": "Invalid URL."})

        try:
            print("FINDING POST")
            media_root = Path(settings.MEDIA_ROOT)
            media_root.mkdir(parents=True, exist_ok=True)

            # (Optional) If you *really* want to clear old files created by THIS app only:
            # Be careful on Render's disk if you share MEDIA_ROOT with other things.
            # for p in media_root.glob("*"):
            #     if p.is_file():
            #         p.unlink()

            L = _instaloader_with_env(media_root)
            post = instaloader.Post.from_shortcode(L.context, shortcode)
            print("FOUND")
            L.download_post(post, target=str(media_root))
            print("DOWNLOADED")

            images, videos = _collect_public_urls(media_root, shortcode)
        except Exception as e:
            error = f"An error occurred: {e}"

    return render(
        request,
        "downloader/posts.html",
        {"data": bool(images or videos), "images": images, "videos": videos, "error": error},
    )

def reels(request):
    """
    Same flow as posts() but renders reels.html.
    """
    images = videos = []
    error = None

    if request.method == "POST":
        url = str(request.POST.get("postURL", "")).strip()
        shortcode = _parse_shortcode(url)
        if not shortcode:
            return render(request, "downloader/reels.html", {"error": "Invalid URL."})

        try:
            print("FINDING POST")
            media_root = Path(settings.MEDIA_ROOT)
            media_root.mkdir(parents=True, exist_ok=True)

            L = _instaloader_with_env(media_root)
            post = instaloader.Post.from_shortcode(L.context, shortcode)
            print("FOUND")
            L.download_post(post, target=str(media_root))
            print("DOWNLOADED")

            images, videos = _collect_public_urls(media_root, shortcode)
        except Exception as e:
            error = f"An error occurred: {e}"

    return render(
        request,
        "downloader/reels.html",
        {"data": bool(images or videos), "images": images, "videos": videos, "error": error},
    )

# ---------- background "all posts" (not suited for Render dynos) ----------

def _download_posts_in_background(username: str):
    """
    Caution: Render web services sleep/scale. Long-running background threads are not reliable.
    Prefer a job runner/cron or do this locally.
    """
    try:
        # Save under MEDIA_ROOT/username
        base = Path(settings.MEDIA_ROOT) / username
        base.mkdir(parents=True, exist_ok=True)

        L = _instaloader_with_env(base)
        L.dirname_pattern = str(base)
        L.download_profile(username)
        print("Download Completed.")
    except Exception as e:
        print(f"An error occurred: {str(e)}")

def allposts(request):
    if request.method == "POST":
        username = str(request.POST.get("postURL", "")).strip()
        if not username:
            return render(request, "downloader/allposts.html", {"error": "Username required."})
        try:
            threading.Thread(target=_download_posts_in_background, args=(username,), daemon=True).start()
            return render(
                request,
                "downloader/allposts.html",
                {"message": "Download started! This may take a while. Please keep this page open."},
            )
        except Exception as e:
            return render(request, "downloader/allposts.html", {"error": f"An error occurred: {e}"})

    return render(request, "downloader/allposts.html")
