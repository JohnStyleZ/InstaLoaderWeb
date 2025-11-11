from pathlib import Path
from django.conf import settings
from django.shortcuts import render
from django.http import JsonResponse, HttpResponseBadRequest
import instaloader, os, threading, base64, tempfile

# ---------- helpers ----------

def _parse_shortcode(url: str) -> str | None:
    """Extract the shortcode from a post/reel/tv URL."""
    if not url:
        return None
    url = url.strip()
    if not url.startswith("https://www.instagram.com/"):
        return None
    parts = url.rstrip("/").split("/")
    # expect .../<type>/<shortcode>
    if len(parts) >= 2:
        return parts[-1]
    return None

def _snap_files(root: Path) -> set[Path]:
    """Return a set of all files under root (recursive)."""
    return {p for p in root.rglob("*") if p.is_file()}

def _as_public_urls(paths: list[Path]) -> tuple[list[str], list[str]]:
    """Map filesystem paths → public /media/ URLs, split into images/videos."""
    exts_img = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    exts_vid = {".mp4", ".avi", ".mov", ".wmv", ".m4v"}
    images, videos = [], []
    root = Path(settings.MEDIA_ROOT)
    for p in sorted(paths):
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            # Skip anything outside MEDIA_ROOT (shouldn't happen, but be safe)
            continue
        url = f"{settings.MEDIA_URL}{rel}"
        if p.suffix.lower() in exts_img:
            images.append(url)
        elif p.suffix.lower() in exts_vid:
            videos.append(url)
    return images, videos

def _instaloader_with_env(media_root: Path) -> instaloader.Instaloader:
    """Configure Instaloader and load session from IG_SESSION_B64 if provided."""
    L = instaloader.Instaloader(
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        max_connection_attempts=1,
        dirname_pattern=str(media_root)  # ensure downloads land under MEDIA_ROOT
    )

    user = os.environ.get("IG_USER")
    session_b64 = os.environ.get("IG_SESSION_B64")  # preferred
    session_path = None

    if session_b64:
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.write(base64.b64decode(session_b64))
        tmp.flush()
        tmp.close()
        session_path = tmp.name

    if user and session_path and Path(session_path).exists():
        try:
            L.load_session_from_file(user, session_path)
            # Debug helpers (leave on while stabilizing)
            print("Instaloader version:", instaloader.__version__)
            print("[instaloader] test_login:", L.test_login())
            print(f"[instaloader] loaded session for {user} (decoded from base64)")
        except Exception as e:
            print(f"[instaloader] session load failed: {e}")

    # Strongly recommend: DO NOT set a custom IG_USER_AGENT; let Instaloader choose.
    # If you added HTTP_PROXY/HTTPS_PROXY envs earlier, consider removing them unless required.

    return L

# ---------- views ----------

def index(request):
    return render(request, "downloader/index.html")

def posts(request):
    """
    POST field name="postURL" with a post/reel URL.
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

            # Snapshot BEFORE download
            before = _snap_files(media_root)

            # Download
            L = _instaloader_with_env(media_root)
            post = instaloader.Post.from_shortcode(L.context, shortcode)
            print("FOUND")
            L.download_post(post, target=str(media_root))
            print("DOWNLOADED")

            # Snapshot AFTER download & diff
            after = _snap_files(media_root)
            new_paths = [p for p in (after - before)
                         if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".avi", ".mov", ".wmv", ".m4v"}]

            images, videos = _as_public_urls(new_paths)

            # Fallback: if diff empty (rare), show any media under MEDIA_ROOT
            if not images and not videos:
                any_media = [p for p in after
                             if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".avi", ".mov", ".wmv", ".m4v"}]
                images, videos = _as_public_urls(any_media)

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

            before = _snap_files(media_root)

            L = _instaloader_with_env(media_root)
            post = instaloader.Post.from_shortcode(L.context, shortcode)
            print("FOUND")
            L.download_post(post, target=str(media_root))
            print("DOWNLOADED")

            after = _snap_files(media_root)
            new_paths = [p for p in (after - before)
                         if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".avi", ".mov", ".wmv", ".m4v"}]

            images, videos = _as_public_urls(new_paths)
            if not images and not videos:
                any_media = [p for p in after
                             if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".avi", ".mov", ".wmv", ".m4v"}]
                images, videos = _as_public_urls(any_media)

        except Exception as e:
            error = f"An error occurred: {e}"

    return render(
        request,
        "downloader/reels.html",
        {"data": bool(images or videos), "images": images, "videos": videos, "error": error},
    )

# ---------- background "all posts" (caution on Render) ----------

def _download_posts_in_background(username: str):
    """
    Caution: Render web services sleep/scale. Long-running background threads are not reliable.
    Prefer a job runner/cron or do this locally.
    """
    try:
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
