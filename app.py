import io
import json
import mimetypes
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import qrcode


# ============================================================
# 기본 설정
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
PHOTO_DIR = BASE_DIR / "photos"
NO_JEKYLL_FILE = BASE_DIR / ".nojekyll"

PORT = 8000

QR_PUBLIC_BASE_URL = os.environ.get(
    "QR_PUBLIC_BASE_URL",
    "",
).strip().rstrip("/")

QR_GIT_AUTO_PUBLISH = os.environ.get(
    "QR_GIT_AUTO_PUBLISH",
    "0",
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

SUPPORTED_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
}

PHOTO_DIR.mkdir(parents=True, exist_ok=True)


def run_git_command(arguments):
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
    except OSError as error:
        return subprocess.CompletedProcess(
            args=["git", *arguments],
            returncode=1,
            stdout="",
            stderr=str(error),
        )


def parse_github_remote(remote_url):
    normalized = remote_url.strip()

    for prefix in (
        "https://github.com/",
        "http://github.com/",
        "git@github.com:",
        "ssh://git@github.com/",
    ):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    else:
        return None

    if normalized.endswith(".git"):
        normalized = normalized[:-4]

    parts = normalized.strip("/").split("/")

    if len(parts) != 2 or not all(parts):
        return None

    return parts[0], parts[1]


def infer_public_base_url():
    if QR_PUBLIC_BASE_URL:
        return QR_PUBLIC_BASE_URL

    if not (BASE_DIR / ".git").exists():
        return ""

    remote_result = run_git_command(["remote", "get-url", "origin"])

    if remote_result.returncode != 0:
        return ""

    parsed = parse_github_remote(remote_result.stdout)

    if parsed is None:
        return ""

    owner, repo = parsed

    return f"https://{owner}.github.io/{repo}/photos"


PUBLIC_BASE_URL = infer_public_base_url()
GIT_PUBLISH_ENABLED = QR_GIT_AUTO_PUBLISH and bool(PUBLIC_BASE_URL)
git_lock = threading.Lock()
last_published_photo_key = None


# ============================================================
# 네트워크 주소 확인
# ============================================================

def get_local_ip():
    """
    같은 Wi-Fi 또는 같은 사내 네트워크에 연결된 휴대폰에서
    접근할 수 있는 PC의 IPv4 주소를 확인합니다.
    """

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        # 실제 데이터를 전송하지 않고 로컬 IP만 확인합니다.
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]

    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"

    finally:
        sock.close()


LOCAL_IP = get_local_ip()


# ============================================================
# 사진 관련 함수
# ============================================================

def get_photos():
    """
    photos 폴더 안의 지원되는 이미지 목록을 가져옵니다.
    가장 최근에 수정된 사진이 첫 번째로 나옵니다.
    """

    photos = []

    for path in PHOTO_DIR.iterdir():
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            photos.append(path)

    photos.sort(
        key=lambda photo: max(
            photo.stat().st_mtime,
            photo.stat().st_ctime,
        ),
        reverse=True,
    )

    return photos


def get_photo_by_name(filename):
    """
    요청받은 파일이 photos 폴더 내부의 안전한 파일인지 확인합니다.
    """

    safe_name = Path(filename).name
    photo_path = (PHOTO_DIR / safe_name).resolve()

    try:
        photo_path.relative_to(PHOTO_DIR.resolve())
    except ValueError:
        return None

    if not photo_path.is_file():
        return None

    if photo_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return None

    return photo_path


def encoded_filename(filename):
    return urllib.parse.quote(filename, safe="")


def get_public_photo_url(filename):
    if not PUBLIC_BASE_URL:
        return ""

    return f"{PUBLIC_BASE_URL}/{encoded_filename(filename)}"


def publish_photo_to_github(photo_path):
    global last_published_photo_key

    if not GIT_PUBLISH_ENABLED:
        return False, "GitHub Pages 자동 공개가 비활성화되어 있습니다."

    if not (BASE_DIR / ".git").exists():
        return False, "현재 폴더가 Git 저장소가 아닙니다."

    with git_lock:
        photo_key = (
            photo_path.name,
            photo_path.stat().st_mtime_ns,
        )

        if last_published_photo_key == photo_key:
            return True, "이미 최신 사진이 GitHub Pages에 반영되어 있습니다."

        NO_JEKYLL_FILE.touch(exist_ok=True)

        relative_photo = photo_path.relative_to(BASE_DIR)

        add_result = run_git_command(
            ["add", str(relative_photo), NO_JEKYLL_FILE.name]
        )

        if add_result.returncode != 0:
            return False, add_result.stderr.strip() or "git add 실패"

        diff_result = run_git_command(["diff", "--cached", "--quiet"])

        if diff_result.returncode == 0:
            last_published_photo_key = photo_key
            return True, "업로드할 변경 사항이 없습니다."

        commit_result = run_git_command(
            ["commit", "-m", f"Publish {photo_path.name}"]
        )

        if commit_result.returncode != 0:
            message = commit_result.stderr.strip() or commit_result.stdout.strip()
            return False, message or "git commit 실패"

        push_result = run_git_command(["push", "origin", "HEAD"])

        if push_result.returncode != 0:
            message = push_result.stderr.strip() or push_result.stdout.strip()
            return False, message or "git push 실패"

        last_published_photo_key = photo_key
        return True, "최신 사진을 GitHub Pages로 업로드했습니다."


def build_share_status(download_url):
    if GIT_PUBLISH_ENABLED and download_url.startswith(PUBLIC_BASE_URL):
        return "공개 QR 모드: GitHub Pages 주소로 연결됩니다."

    if QR_GIT_AUTO_PUBLISH and not PUBLIC_BASE_URL:
        return "GitHub 저장소 origin을 연결하면 공개 QR 모드가 활성화됩니다."

    return "현재 QR은 같은 Wi-Fi 또는 같은 네트워크에서만 열립니다."


def create_download_url(filename):
    """
    QR 코드에 들어가는 주소입니다.

    GitHub Pages 자동 공개가 활성화되어 있으면 공개 사진 주소를,
    아니면 기존 LAN 다운로드 페이지 주소를 반환합니다.
    """

    photo = get_photo_by_name(filename)

    if photo is not None and GIT_PUBLISH_ENABLED:
        published, _ = publish_photo_to_github(photo)

        if published:
            return get_public_photo_url(photo.name)

    encoded = encoded_filename(filename)

    return (
        f"http://{LOCAL_IP}:{PORT}"
        f"/view?file={encoded}"
    )


# ============================================================
# HTML 화면
# ============================================================

INDEX_HTML = """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta
        name="viewport"
        content="width=device-width, initial-scale=1.0"
    >

    <title>AI 미래 직업 포토부스</title>

    <style>
        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            min-height: 100vh;
            font-family:
                "Segoe UI",
                "Malgun Gothic",
                Arial,
                sans-serif;
            background:
                radial-gradient(
                    circle at top left,
                    #24456d 0,
                    transparent 36%
                ),
                linear-gradient(
                    135deg,
                    #07111f,
                    #101d32 55%,
                    #081321
                );
            color: white;
            overflow-x: hidden;
        }

        .page {
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            padding: 24px 30px 28px;
        }

        .header {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 20px;
            margin-bottom: 18px;
        }

        .title-area h1 {
            margin: 0;
            font-size: clamp(28px, 3vw, 48px);
            line-height: 1.1;
            letter-spacing: -1.5px;
        }

        .title-area p {
            margin: 10px 0 0;
            color: #b9c8db;
            font-size: clamp(14px, 1.3vw, 19px);
        }

        .server-status {
            display: flex;
            align-items: center;
            gap: 9px;
            color: #bcead3;
            font-size: 14px;
            background: rgba(26, 59, 51, 0.65);
            border: 1px solid rgba(76, 220, 151, 0.35);
            border-radius: 999px;
            padding: 10px 16px;
            white-space: nowrap;
        }

        .status-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #42e38f;
            box-shadow: 0 0 12px #42e38f;
        }

        .content {
            flex: 1;
            display: grid;
            grid-template-columns: minmax(0, 1.6fr) minmax(300px, 0.8fr);
            gap: 24px;
            min-height: 0;
        }

        .card {
            background: rgba(255, 255, 255, 0.075);
            border: 1px solid rgba(255, 255, 255, 0.13);
            border-radius: 26px;
            box-shadow: 0 22px 60px rgba(0, 0, 0, 0.32);
            backdrop-filter: blur(18px);
        }

        .photo-card {
            min-height: 620px;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 18px;
            overflow: hidden;
            position: relative;
        }

        .photo-card img {
            max-width: 100%;
            max-height: calc(100vh - 175px);
            object-fit: contain;
            border-radius: 18px;
            box-shadow: 0 14px 42px rgba(0, 0, 0, 0.45);
        }

        .right-card {
            min-height: 620px;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 28px;
            text-align: center;
        }

        .step-label {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            background: #2a75ff;
            color: white;
            border-radius: 999px;
            padding: 7px 15px;
            font-size: 14px;
            font-weight: 700;
            margin-bottom: 14px;
        }

        .right-card h2 {
            margin: 0;
            font-size: clamp(24px, 2.3vw, 36px);
            letter-spacing: -1px;
        }

        .right-card .guide {
            margin: 10px 0 20px;
            color: #bdcadc;
            line-height: 1.65;
            font-size: 16px;
        }

        .qr-box {
            background: white;
            border-radius: 22px;
            padding: 17px;
            width: min(100%, 350px);
            box-shadow: 0 18px 45px rgba(0, 0, 0, 0.28);
        }

        .qr-box img {
            width: 100%;
            display: block;
        }

        .file-name {
            width: 100%;
            margin-top: 20px;
            background: rgba(255, 255, 255, 0.07);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 14px;
            padding: 12px 15px;
            color: #d2dceb;
            font-size: 13px;
            word-break: break-all;
        }

        .updated {
            margin-top: 12px;
            color: #8090a5;
            font-size: 12px;
        }

        .share-status {
            margin-top: 10px;
            color: #9bd6ff;
            font-size: 13px;
            line-height: 1.55;
        }

        .empty {
            max-width: 580px;
            text-align: center;
            padding: 35px;
        }

        .empty-icon {
            font-size: 80px;
            margin-bottom: 20px;
        }

        .empty h2 {
            margin: 0 0 12px;
            font-size: 32px;
        }

        .empty p {
            margin: 0;
            color: #bdcadc;
            font-size: 17px;
            line-height: 1.7;
        }

        .folder {
            display: inline-block;
            margin-top: 18px;
            background: rgba(255, 255, 255, 0.09);
            border: 1px solid rgba(255, 255, 255, 0.13);
            border-radius: 12px;
            padding: 12px 16px;
            font-family: Consolas, monospace;
            color: #dbe8f7;
            word-break: break-all;
        }

        .hidden {
            display: none !important;
        }

        @media (max-width: 900px) {
            .page {
                padding: 18px;
            }

            .header {
                align-items: stretch;
                flex-direction: column;
            }

            .server-status {
                align-self: flex-start;
            }

            .content {
                grid-template-columns: 1fr;
            }

            .photo-card,
            .right-card {
                min-height: auto;
            }

            .photo-card img {
                max-height: 65vh;
            }
        }
    </style>
</head>

<body>
    <main class="page">
        <header class="header">
            <div class="title-area">
                <h1>나의 미래 직업 포토부스</h1>
                <p>QR 코드를 스캔하여 내 사진을 휴대폰에 저장하세요.</p>
            </div>

            <div class="server-status">
                <span class="status-dot"></span>
                다운로드 서버 연결됨
            </div>
        </header>

        <section id="emptyState" class="card photo-card hidden">
            <div class="empty">
                <div class="empty-icon">📷</div>
                <h2>새 사진을 기다리고 있습니다</h2>

                <p>
                    아래 폴더에 사진을 넣으면<br>
                    자동으로 화면에 표시됩니다.
                </p>

                <div class="folder" id="folderPath"></div>
            </div>
        </section>

        <section id="photoContent" class="content hidden">
            <div class="card photo-card">
                <img
                    id="mainPhoto"
                    alt="생성된 미래 직업 사진"
                >
            </div>

            <aside class="card right-card">
                <div class="step-label">
                    휴대폰으로 스캔
                </div>

                <h2>사진을 가져가세요</h2>

                <p class="guide">
                    카메라로 아래 QR 코드를 스캔하면<br>
                    휴대폰에서 사진을 바로 열 수 있습니다.
                </p>

                <div class="qr-box">
                    <img
                        id="qrImage"
                        alt="사진 다운로드 QR 코드"
                    >
                </div>

                <div class="file-name" id="fileName"></div>
                <div class="updated" id="updatedTime"></div>
                <div class="share-status" id="shareStatus"></div>
            </aside>
        </section>
    </main>

    <script>
        let currentFile = null;

        const emptyState = document.getElementById("emptyState");
        const photoContent = document.getElementById("photoContent");
        const mainPhoto = document.getElementById("mainPhoto");
        const qrImage = document.getElementById("qrImage");
        const fileName = document.getElementById("fileName");
        const updatedTime = document.getElementById("updatedTime");
        const shareStatus = document.getElementById("shareStatus");
        const folderPath = document.getElementById("folderPath");

        async function updateLatestPhoto() {
            try {
                const response = await fetch(
                    "/api/latest?t=" + Date.now(),
                    { cache: "no-store" }
                );

                if (!response.ok) {
                    throw new Error("서버 응답 오류");
                }

                const data = await response.json();

                folderPath.textContent = data.photo_folder;

                if (!data.has_photo) {
                    currentFile = null;
                    emptyState.classList.remove("hidden");
                    photoContent.classList.add("hidden");
                    shareStatus.textContent = data.share_status || "";
                    return;
                }

                emptyState.classList.add("hidden");
                photoContent.classList.remove("hidden");

                /*
                 * 새 사진일 때만 이미지와 QR 주소를 교체합니다.
                 * 같은 사진을 불필요하게 계속 다시 읽지 않습니다.
                 */
                if (currentFile !== data.filename) {
                    currentFile = data.filename;

                    mainPhoto.src =
                        data.photo_url +
                        "?v=" +
                        encodeURIComponent(data.modified_ns);

                    qrImage.src =
                        data.qr_url +
                        "?v=" +
                        encodeURIComponent(data.modified_ns);

                    fileName.textContent = data.filename;
                }

                shareStatus.textContent = data.share_status || "";

                updatedTime.textContent =
                    "최근 확인: " +
                    new Date().toLocaleTimeString("ko-KR");

            } catch (error) {
                updatedTime.textContent =
                    "서버 연결 확인 중...";
            }
        }

        updateLatestPhoto();

        /*
         * photos 폴더를 2초마다 확인합니다.
         * 새로운 사진이 추가되면 자동으로 최신 사진으로 변경됩니다.
         */
        setInterval(updateLatestPhoto, 2000);
    </script>
</body>
</html>
"""


VIEW_HTML = """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">

    <meta
        name="viewport"
        content="width=device-width, initial-scale=1.0"
    >

    <title>내 사진 다운로드</title>

    <style>
        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            min-height: 100vh;
            background:
                radial-gradient(
                    circle at top,
                    #264e7d,
                    #0a1424 60%
                );
            color: white;
            font-family:
                "Segoe UI",
                "Malgun Gothic",
                Arial,
                sans-serif;
            padding: 20px;
        }

        .container {
            width: min(100%, 720px);
            margin: 0 auto;
            text-align: center;
        }

        h1 {
            margin: 8px 0 8px;
            font-size: clamp(28px, 8vw, 42px);
            letter-spacing: -1px;
        }

        .description {
            margin: 0 0 20px;
            color: #bdcadc;
            line-height: 1.6;
        }

        .photo-box {
            background: rgba(255, 255, 255, 0.09);
            border: 1px solid rgba(255, 255, 255, 0.13);
            border-radius: 24px;
            padding: 13px;
            box-shadow: 0 20px 55px rgba(0, 0, 0, 0.35);
        }

        .photo-box img {
            display: block;
            width: 100%;
            max-height: 65vh;
            object-fit: contain;
            border-radius: 16px;
        }

        .download-button {
            display: flex;
            align-items: center;
            justify-content: center;
            width: 100%;
            min-height: 58px;
            margin-top: 18px;
            padding: 14px 18px;
            border-radius: 16px;
            background: #2979ff;
            color: white;
            text-decoration: none;
            font-size: 20px;
            font-weight: 800;
            box-shadow: 0 12px 30px rgba(41, 121, 255, 0.35);
        }

        .file-name {
            margin-top: 13px;
            color: #93a4ba;
            font-size: 13px;
            word-break: break-all;
        }

        .tip {
            margin-top: 17px;
            color: #9fb0c4;
            line-height: 1.6;
            font-size: 13px;
        }
    </style>
</head>

<body>
    <main class="container">
        <h1>내 미래 직업 사진</h1>

        <p class="description">
            아래 버튼을 눌러 사진을 저장하세요.
        </p>

        <div class="photo-box">
            <img
                src="{photo_url}"
                alt="내 미래 직업 사진"
            >
        </div>

        <a
            class="download-button"
            href="{download_url}"
            download
        >
            사진 다운로드
        </a>

        <div class="file-name">
            {display_filename}
        </div>

        <div class="tip">
            휴대폰 설정이나 브라우저 종류에 따라 사진이 바로 열릴 수 있습니다.<br>
            그런 경우 사진을 길게 눌러 저장할 수도 있습니다.
        </div>
    </main>
</body>
</html>
"""


# ============================================================
# HTTP 요청 처리
# ============================================================

class PhotoBoothHandler(BaseHTTPRequestHandler):

    def log_message(self, format_string, *args):
        """
        QR 스캔과 다운로드 요청은 콘솔에 간단히 표시합니다.
        API 자동 새로고침 로그는 표시하지 않습니다.
        """

        if not self.path.startswith("/api/latest"):
            client_ip = self.client_address[0]
            message = format_string % args
            print(f"[접속 {client_ip}] {message}")

    def send_bytes(
        self,
        content,
        content_type,
        status=200,
        extra_headers=None,
    ):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))

        self.send_header(
            "Cache-Control",
            "no-store, no-cache, must-revalidate",
        )

        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)

        self.end_headers()
        self.wfile.write(content)

    def send_text(
        self,
        text,
        content_type="text/html; charset=utf-8",
        status=200,
    ):
        self.send_bytes(
            text.encode("utf-8"),
            content_type,
            status=status,
        )

    def send_json(self, data, status=200):
        content = json.dumps(
            data,
            ensure_ascii=False,
        ).encode("utf-8")

        self.send_bytes(
            content,
            "application/json; charset=utf-8",
            status=status,
        )

    def send_not_found(self, message="파일을 찾을 수 없습니다."):
        html = f"""
        <!DOCTYPE html>
        <html lang="ko">
        <head>
            <meta charset="UTF-8">
            <meta
                name="viewport"
                content="width=device-width, initial-scale=1.0"
            >
            <title>파일 없음</title>
        </head>
        <body style="
            margin:0;
            min-height:100vh;
            display:flex;
            align-items:center;
            justify-content:center;
            background:#0b1627;
            color:white;
            font-family:Arial, sans-serif;
            text-align:center;
            padding:25px;
        ">
            <div>
                <div style="font-size:65px;">📷</div>
                <h1>{message}</h1>
                <p style="color:#a9b8cc;">
                    운영자에게 새로운 QR 코드를 요청해 주세요.
                </p>
            </div>
        </body>
        </html>
        """

        self.send_text(html, status=404)

    def serve_photo(self, filename, download=False):
        photo = get_photo_by_name(filename)

        if photo is None:
            self.send_not_found()
            return

        mime_type = (
            mimetypes.guess_type(photo.name)[0]
            or "application/octet-stream"
        )

        content = photo.read_bytes()

        headers = {}

        if download:
            encoded_name = urllib.parse.quote(photo.name)
            headers["Content-Disposition"] = (
                f"attachment; filename*=UTF-8''{encoded_name}"
            )

        self.send_bytes(
            content,
            mime_type,
            extra_headers=headers,
        )

    def serve_qr(self, filename):
        photo = get_photo_by_name(filename)

        if photo is None:
            self.send_not_found()
            return

        download_url = create_download_url(photo.name)

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=12,
            border=4,
        )

        qr.add_data(download_url)
        qr.make(fit=True)

        qr_image = qr.make_image(
            fill_color="black",
            back_color="white",
        )

        buffer = io.BytesIO()
        qr_image.save(buffer, format="PNG")

        self.send_bytes(
            buffer.getvalue(),
            "image/png",
        )

    def handle_latest_api(self):
        photos = get_photos()

        if not photos:
            self.send_json(
                {
                    "has_photo": False,
                    "photo_folder": str(PHOTO_DIR),
                    "share_status": build_share_status(""),
                }
            )
            return

        latest = photos[0]
        encoded = encoded_filename(latest.name)
        download_url = create_download_url(latest.name)

        self.send_json(
            {
                "has_photo": True,
                "filename": latest.name,
                "modified_ns": latest.stat().st_mtime_ns,
                "photo_url": f"/photo/{encoded}",
                "qr_url": f"/qr/{encoded}",
                "download_url": download_url,
                "photo_folder": str(PHOTO_DIR),
                "share_status": build_share_status(download_url),
            }
        )

    def handle_view_page(self, query):
        query_values = urllib.parse.parse_qs(query)
        filename = query_values.get("file", [""])[0]

        photo = get_photo_by_name(filename)

        if photo is None:
            self.send_not_found(
                "사진이 삭제되었거나 존재하지 않습니다."
            )
            return

        encoded = encoded_filename(photo.name)

        photo_url = f"/photo/{encoded}"
        download_url = f"/download/{encoded}"

        safe_display_filename = (
            photo.name
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

        html = VIEW_HTML.format(
            photo_url=photo_url,
            download_url=download_url,
            display_filename=safe_display_filename,
        )

        self.send_text(html)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/":
            self.send_text(INDEX_HTML)
            return

        if path == "/api/latest":
            self.handle_latest_api()
            return

        if path == "/view":
            self.handle_view_page(parsed.query)
            return

        if path.startswith("/photo/"):
            filename = urllib.parse.unquote(
                path[len("/photo/"):]
            )

            self.serve_photo(
                filename,
                download=False,
            )
            return

        if path.startswith("/download/"):
            filename = urllib.parse.unquote(
                path[len("/download/"):]
            )

            self.serve_photo(
                filename,
                download=True,
            )
            return

        if path.startswith("/qr/"):
            filename = urllib.parse.unquote(
                path[len("/qr/"):]
            )

            self.serve_qr(filename)
            return

        self.send_not_found("페이지를 찾을 수 없습니다.")


# ============================================================
# 서버 실행
# ============================================================

def main():
    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        PhotoBoothHandler,
    )

    operator_url = f"http://localhost:{PORT}"
    mobile_test_url = f"http://{LOCAL_IP}:{PORT}"

    print()
    print("=" * 66)
    print("  AI 미래 직업 포토부스 서버")
    print("=" * 66)
    print(f"  사진 폴더       : {PHOTO_DIR}")
    print(f"  운영자 화면     : {operator_url}")
    print(f"  휴대폰 테스트   : {mobile_test_url}")

    if GIT_PUBLISH_ENABLED:
        print(f"  공개 사진 경로   : {PUBLIC_BASE_URL}")
    elif QR_GIT_AUTO_PUBLISH:
        print("  공개 사진 경로   : GitHub origin 저장소 연결 필요")
    else:
        print("  공개 사진 경로   : 비활성화 (로컬 네트워크 QR 사용)")

    print("=" * 66)
    print()
    print("photos 폴더에 사진을 넣으면 최신 사진이 자동 표시됩니다.")
    print("서버를 종료하려면 Ctrl + C를 누르세요.")
    print()

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        print()
        print("서버를 종료합니다.")

    finally:
        server.server_close()


if __name__ == "__main__":
    try:
        main()

    except ModuleNotFoundError as error:
        if error.name == "qrcode":
            print()
            print("qrcode 라이브러리가 설치되어 있지 않습니다.")
            print()
            print("아래 명령으로 설치하세요:")
            print(f'"{sys.executable}" -m pip install "qrcode[pil]"')
            print()
        else:
            raise