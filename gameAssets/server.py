import base64
import binascii
import io
import ipaddress
import json
import logging
import math
import mimetypes
import os
import re
import ssl
import tempfile
import threading
import time
import zipfile
from contextlib import nullcontext
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from PIL import Image, ImageDraw, ImageOps
except ImportError:  # pragma: no cover - exercised only on installations without Pillow
    Image = None
    ImageDraw = None
    ImageOps = None


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
LOCAL_CONFIG_FILE = os.path.join(BASE_DIR, "config.local.json")
REQUESTED_STORAGE_ROOT = (
    "/Volumes/CM1/行銷處/市場營銷部/0共用/04_Design/05_其它專案/"
    "icon整形專案/所有遊戲圖/(0)更新紀錄"
)

DEFAULT_WORKFLOW_ID = "2084930502464638978"
EDIT_WORKFLOW_ID = "2085215811236577281"
SHRINK_WORKFLOW_ID = "2061658624203771906"
COMBINED_CROP_WORKFLOW_ID = "2085291529685544962"
DEFAULT_NODE_ID = "144"
DEFAULT_FIELD_NAME = "image"
DEFAULT_INSTANCE_TYPE = "plus"

IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})
MAX_JSON_BODY_BYTES = 1 * 1024 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_PREVIEW_BYTES = 25 * 1024 * 1024
# 塗鴉遮罩是跟 prompt、路徑等欄位一起塞進同一個 JSON 請求（上限 MAX_JSON_BODY_BYTES），
# 這裡另外設一個明確的欄位上限，塗鴉圖太大時給清楚的錯誤，而不是整個請求被攔在更底層。
MAX_EDIT_MASK_IMAGE_BASE64_CHARS = 700_000
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
MAX_SCAN_FILES = 5000
MAX_UPSTREAM_RESPONSE_BYTES = 2 * 1024 * 1024
MIN_RESIZE_PERCENT = 1.0
MAX_RESIZE_PERCENT = 300.0
DEFAULT_RESIZE_PERCENT = 100.0
CROP_OUTPUT_WIDTH = 1344
CROP_OUTPUT_HEIGHT = 1024
REQUEST_TIMEOUT_SECONDS = 30
UPSTREAM_TIMEOUT_SECONDS = 60
UPLOAD_TIMEOUT_SECONDS = 120
UPLOAD_RETRIES = 3
DOWNLOAD_TIMEOUT_SECONDS = 120
RETRYABLE_HTTP_STATUS = frozenset({408, 429, 500, 502, 503, 504})
RUNNINGHUB_CDN_HOST_PATTERN = re.compile(
    r"^rh-[a-z0-9-]+-images-[0-9]+\.cos\.[a-z0-9-]+\.myqcloud\.com$"
)
IMAGE_URL_KEYS = frozenset(
    {
        "url",
        "imageurl",
        "image_url",
        "fileurl",
        "file_url",
        "outputurl",
        "output_url",
        "downloadurl",
        "download_url",
    }
)
IMAGE_OUTPUT_TYPES = frozenset(
    {
        "bmp",
        "gif",
        "jpeg",
        "jpg",
        "png",
        "webp",
        "image/bmp",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)

LOGGER = logging.getLogger("gameassets")
if not LOGGER.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _create_tls_context():
    configured_bundle = os.environ.get("GAME_ASSETS_CA_BUNDLE") or os.environ.get(
        "SSL_CERT_FILE"
    )
    if configured_bundle:
        if not os.path.isfile(configured_bundle):
            raise RuntimeError(f"CA bundle does not exist: {configured_bundle}")
        ca_bundle = configured_bundle
    else:
        ca_bundle = next(
            (
                path
                for path in (
                    "/private/etc/ssl/cert.pem",
                    "/etc/ssl/cert.pem",
                    "/usr/local/etc/ca-certificates/cert.pem",
                    "/opt/homebrew/etc/ca-certificates/cert.pem",
                )
                if os.path.isfile(path)
            ),
            None,
        )

    if ca_bundle:
        context = ssl.create_default_context(cafile=ca_bundle)
        LOGGER.info("Using TLS CA bundle: %s", ca_bundle)
        return context

    context = ssl.create_default_context()
    if not context.get_ca_certs():
        LOGGER.warning(
            "No trusted TLS CA certificates found; set GAME_ASSETS_CA_BUNDLE before "
            "starting the server"
        )
    return context


TLS_CONTEXT = _create_tls_context()


class RequestError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class UpstreamError(RequestError):
    def __init__(self, message="RunningHub request failed", upstream_status=None):
        super().__init__(502, message)
        self.upstream_status = upstream_status


class ConfigError(RequestError):
    def __init__(self, message="Unable to save configuration"):
        super().__init__(500, message)


def _bounded_env_int(name, default, minimum, maximum):
    raw_value = os.environ.get(name, "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        LOGGER.warning("Ignoring invalid %s value", name)
        return default
    if value < minimum or value > maximum:
        LOGGER.warning("Ignoring out-of-range %s value", name)
        return default
    return value


def _default_allowed_roots():
    roots = [os.path.expanduser("~"), BASE_DIR, REQUESTED_STORAGE_ROOT]
    # Apache's macOS document root is commonly used as the project storage
    # volume. Only enable it when this server itself is running inside it.
    for candidate in ("/Library/WebServer/Documents", "/var/www", "/var/www/html"):
        resolved_candidate = os.path.realpath(os.path.abspath(candidate))
        if not os.path.isdir(resolved_candidate):
            continue
        try:
            if os.path.commonpath((BASE_DIR, resolved_candidate)) == resolved_candidate:
                roots.append(resolved_candidate)
        except ValueError:
            continue
    return roots


def _allowed_roots():
    raw_value = os.environ.get("GAME_ASSETS_ALLOWED_ROOTS", "")
    configured_roots = [
        os.path.expanduser(item.strip())
        for item in raw_value.split(os.pathsep)
        if item.strip()
    ]
    if not configured_roots:
        configured_roots = _default_allowed_roots()

    roots = []
    for root in configured_roots:
        resolved_root = os.path.realpath(os.path.abspath(root))
        if resolved_root not in roots:
            roots.append(resolved_root)
    return tuple(roots)


ALLOWED_ROOTS = _allowed_roots()
MAX_REQUEST_BODY_BYTES = _bounded_env_int(
    "GAME_ASSETS_MAX_REQUEST_BYTES", MAX_UPLOAD_BYTES, 1024, MAX_UPLOAD_BYTES
)
RUNNINGHUB_RETAIN_SECONDS = _bounded_env_int(
    "RUNNINGHUB_RETAIN_SECONDS", 3600, 60, 86400
)
# RunningHub 帳號層級對同時執行中的任務數有上限（依方案而定）。當本機同時送出多個
# 任務（例如並行設為 3）而其中幾個超過上限時，run/workflow 不會回傳 HTTP 錯誤，而是
# 在 200 回應裡帶著 errorCode（例如 APIKEY_TASK_IS_RUNNING / APIKEY_TASK_IS_QUEUED）
# 且沒有 taskId。這代表「線上目前有任務正在跑」，不是真正失敗，等其中一個跑完釋放
# 名額即可送出，因此在 run_workflow() 內自動等待重試，而不是直接把錯誤丟回前端。
RUNNINGHUB_BUSY_RETRY_ATTEMPTS = _bounded_env_int(
    "RUNNINGHUB_BUSY_RETRY_ATTEMPTS", 24, 1, 200
)
RUNNINGHUB_BUSY_RETRY_INTERVAL_SECONDS = _bounded_env_int(
    "RUNNINGHUB_BUSY_RETRY_INTERVAL_SECONDS", 5, 1, 60
)


def _is_within_allowed_root(path):
    resolved_path = os.path.realpath(os.path.abspath(path))
    for root in ALLOWED_ROOTS:
        try:
            if os.path.commonpath((resolved_path, root)) == root:
                return True
        except ValueError:
            continue
    return False


def _require_text(value, field_name, max_length=4096, allow_empty=False):
    if not isinstance(value, str):
        raise RequestError(400, f"{field_name} must be a string")
    value = value.strip()
    if not value and not allow_empty:
        raise RequestError(400, f"{field_name} is required")
    if len(value) > max_length:
        raise RequestError(413, f"{field_name} is too long")
    if "\x00" in value or any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        raise RequestError(400, f"{field_name} contains invalid characters")
    return value


def _optional_text(data, field_name, default="", max_length=4096):
    if field_name not in data or data[field_name] is None:
        return default
    return _require_text(
        data[field_name], field_name, max_length=max_length, allow_empty=True
    )


def _validated_identifier(value, field_name, default, pattern=r"[A-Za-z0-9_.-]{1,80}"):
    value = default if value in (None, "") else value
    value = _require_text(value, field_name, max_length=80)
    if not re.fullmatch(pattern, value):
        raise RequestError(400, f"Invalid {field_name}")
    return value


def _validated_path(value, field_name, *, directory=False, image=False, allow_missing=False):
    value = _require_text(value, field_name, max_length=4096)
    expanded_path = os.path.expanduser(value)
    if not os.path.isabs(expanded_path):
        raise RequestError(400, f"{field_name} must be an absolute path")

    resolved_path = os.path.realpath(os.path.abspath(expanded_path))
    if not _is_within_allowed_root(resolved_path):
        raise RequestError(403, f"{field_name} is outside the allowed folders")

    if allow_missing:
        probe_path = resolved_path
        while not os.path.exists(probe_path):
            parent_path = os.path.dirname(probe_path)
            if parent_path == probe_path:
                break
            probe_path = parent_path
        if not _is_within_allowed_root(probe_path):
            raise RequestError(403, f"{field_name} is outside the allowed folders")
    elif not os.path.exists(resolved_path):
        raise RequestError(404, f"{field_name} does not exist")

    if directory:
        if os.path.exists(resolved_path) and not os.path.isdir(resolved_path):
            raise RequestError(400, f"{field_name} must be a directory")
        if not allow_missing and not os.path.isdir(resolved_path):
            raise RequestError(400, f"{field_name} must be a directory")
    elif not allow_missing and not os.path.isfile(resolved_path):
        raise RequestError(400, f"{field_name} must be a file")

    if image and os.path.splitext(resolved_path)[1].lower() not in IMAGE_EXTENSIONS:
        raise RequestError(415, f"{field_name} must be an image file")
    return resolved_path


def _validated_output_directory(value):
    if not value:
        return ""
    return _validated_path(value, "output_dir", directory=True, allow_missing=True)


def _safe_upload_filename(value):
    value = _require_text(value or "dragged_image.png", "file name", max_length=255)
    value = os.path.basename(value.replace("\\", "/"))
    if (
        value in ("", ".", "..")
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or '"' in value
    ):
        raise RequestError(415, "Only supported image file types can be uploaded")
    extension = os.path.splitext(value)[1].lower()
    if not extension:
        value = f"{value}.png"
    elif extension not in IMAGE_EXTENSIONS:
        raise RequestError(415, "Only supported image file types can be uploaded")
    return value


def _validated_resize_percent(value, field_name="resizePercent"):
    if value in (None, ""):
        return DEFAULT_RESIZE_PERCENT
    if isinstance(value, bool):
        raise RequestError(400, f"{field_name} must be a number")
    try:
        percent = float(value)
    except (TypeError, ValueError) as exc:
        raise RequestError(400, f"{field_name} must be a number") from exc
    if (
        not math.isfinite(percent)
        or percent < MIN_RESIZE_PERCENT
        or percent > MAX_RESIZE_PERCENT
    ):
        raise RequestError(
            400,
            f"{field_name} must be between {MIN_RESIZE_PERCENT:g} and {MAX_RESIZE_PERCENT:g}",
        )
    return round(percent, 2)


def _resize_image_for_workflow(file_path, resize_percent, temp_dir):
    if resize_percent == 100:
        return file_path
    if Image is None or ImageOps is None:
        raise RequestError(
            503,
            "Pillow is required for Python image resizing; install requirements.txt",
        )

    extension = os.path.splitext(file_path)[1].lower()
    format_by_extension = {
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".png": "PNG",
        ".webp": "WEBP",
        ".bmp": "BMP",
    }
    output_path = os.path.join(temp_dir, f"resized{extension}")
    try:
        with Image.open(file_path) as source:
            image = ImageOps.exif_transpose(source)
            try:
                target_size = (
                    max(1, math.floor(image.width * resize_percent / 100 + 0.5)),
                    max(1, math.floor(image.height * resize_percent / 100 + 0.5)),
                )
                if image.size == target_size:
                    return file_path

                resample = (
                    Image.Resampling.LANCZOS
                    if hasattr(Image, "Resampling")
                    else Image.LANCZOS
                )
                resized = image.resize(target_size, resample)
                save_image = resized
                try:
                    image_format = format_by_extension.get(extension, "PNG")
                    save_options = {}
                    if image_format == "JPEG":
                        save_image = resized.convert("RGB")
                        save_options = {"quality": 95, "optimize": True}
                    elif image_format not in {"PNG", "WEBP", "BMP"}:
                        image_format = "PNG"
                    save_image.save(output_path, format=image_format, **save_options)
                finally:
                    if save_image is not resized:
                        save_image.close()
                    resized.close()
            finally:
                if image is not source:
                    image.close()
    except (OSError, ValueError) as exc:
        raise RequestError(415, "Unable to decode image for resizing") from exc
    return output_path


def _prepare_crop_image_for_workflow(
    file_path, resize_percent, temp_dir, crop_parameters=None
):
    """Prepare a transparent 1344x1024 canvas for local Step4 outpainting."""
    if Image is None or ImageOps is None:
        raise RequestError(
            503,
            "Pillow is required for Step4 crop normalization; install requirements.txt",
        )

    output_path = os.path.join(temp_dir, "crop-prepared.png")
    try:
        with Image.open(file_path) as source:
            image = ImageOps.exif_transpose(source)
            try:
                resample = (
                    Image.Resampling.LANCZOS
                    if hasattr(Image, "Resampling")
                    else Image.LANCZOS
                )
                base_image = ImageOps.fit(
                    image.convert("RGB"),
                    (CROP_OUTPUT_WIDTH, CROP_OUTPUT_HEIGHT),
                    method=resample,
                    centering=(0.5, 0.5),
                )
                try:
                    target_size = (
                        max(1, math.floor(CROP_OUTPUT_WIDTH * resize_percent / 100 + 0.5)),
                        max(1, math.floor(CROP_OUTPUT_HEIGHT * resize_percent / 100 + 0.5)),
                    )
                    scaled = (
                        base_image
                        if base_image.size == target_size
                        else base_image.resize(target_size, resample)
                    )
                    try:
                        parameters = crop_parameters or {}
                        crop_left = int(parameters.get("crop_left", 0))
                        crop_right = int(parameters.get("crop_right", 0))
                        crop_top = int(parameters.get("crop_top", 0))
                        crop_bottom = int(parameters.get("crop_bottom", 0))
                        extension_left = int(parameters.get("extension_left", 0))
                        extension_right = int(parameters.get("extension_right", 0))
                        extension_top = int(parameters.get("extension_top", 0))
                        extension_bottom = int(parameters.get("extension_bottom", 0))
                        if (
                            crop_left + crop_right >= scaled.width
                            or crop_top + crop_bottom >= scaled.height
                        ):
                            raise RequestError(400, "Step4 crop parameters exceed image bounds")
                        cropped = scaled.crop(
                            (
                                crop_left,
                                crop_top,
                                scaled.width - crop_right,
                                scaled.height - crop_bottom,
                            )
                        )
                        try:
                            if (
                                extension_left + cropped.width + extension_right
                                != CROP_OUTPUT_WIDTH
                                or extension_top + cropped.height + extension_bottom
                                != CROP_OUTPUT_HEIGHT
                            ):
                                raise RequestError(
                                    400, "Step4 crop and extension parameters do not fill output"
                                )
                            canvas = Image.new(
                                "RGBA",
                                (CROP_OUTPUT_WIDTH, CROP_OUTPUT_HEIGHT),
                                (0, 0, 0, 0),
                            )
                            try:
                                cropped_rgba = cropped.convert("RGBA")
                                try:
                                    canvas.paste(
                                        cropped_rgba,
                                        (extension_left, extension_top),
                                    )
                                finally:
                                    cropped_rgba.close()
                                canvas.save(output_path, format="PNG", optimize=True)
                            finally:
                                canvas.close()
                        finally:
                            cropped.close()
                    finally:
                        if scaled is not base_image:
                            scaled.close()
                finally:
                    base_image.close()
            finally:
                if image is not source:
                    image.close()
    except (OSError, ValueError) as exc:
        raise RequestError(415, "Unable to decode image for Step4 crop normalization") from exc
    return output_path


def _normalize_crop_download(path):
    """Protect the final Step4 file from upstream aspect-ratio differences."""
    if Image is None or ImageOps is None:
        raise RequestError(
            503,
            "Pillow is required to normalize Step4 output; install requirements.txt",
        )

    temp_path = f"{path}.normalize"
    try:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source)
            try:
                if image.size == (CROP_OUTPUT_WIDTH, CROP_OUTPUT_HEIGHT):
                    return
                resample = (
                    Image.Resampling.LANCZOS
                    if hasattr(Image, "Resampling")
                    else Image.LANCZOS
                )
                normalized = ImageOps.fit(
                    image.convert("RGB"),
                    (CROP_OUTPUT_WIDTH, CROP_OUTPUT_HEIGHT),
                    method=resample,
                    centering=(0.5, 0.5),
                )
                try:
                    normalized.save(temp_path, format="PNG", optimize=True)
                finally:
                    normalized.close()
            finally:
                if image is not source:
                    image.close()
        os.replace(temp_path, path)
    except (OSError, ValueError) as exc:
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass
        except OSError:
            LOGGER.debug("Unable to remove temporary normalized output")
        raise RequestError(500, "Unable to normalize Step4 output to 1344x1024") from exc


def _safe_download_hosts():
    raw_value = os.environ.get("RUNNINGHUB_DOWNLOAD_HOSTS", "runninghub.ai")
    hosts = []
    for item in raw_value.split(","):
        host = item.strip().lower().lstrip(".")
        if host.startswith("*."):
            host = host[2:]
        if host and host not in hosts:
            hosts.append(host)
    return tuple(hosts) or ("runninghub.ai",)


def _validate_remote_url(url, *, require_image=False):
    if not isinstance(url, str) or len(url) > 4096:
        raise RequestError(400, "Invalid download URL")
    try:
        parsed = urllib.parse.urlparse(url)
        port = parsed.port
    except ValueError as exc:
        raise RequestError(400, "Invalid download URL") from exc

    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or not host:
        raise RequestError(400, "Download URLs must use HTTPS")
    if parsed.username or parsed.password or port not in (None, 443):
        raise RequestError(400, "Invalid download URL")

    try:
        host_ip = ipaddress.ip_address(host)
    except ValueError:
        host_ip = None
    if host_ip and (
        host_ip.is_private
        or host_ip.is_loopback
        or host_ip.is_link_local
        or host_ip.is_reserved
        or host_ip.is_multicast
        or host_ip.is_unspecified
    ):
        raise RequestError(403, "Download URL points to a private address")

    allowed = _safe_download_hosts()
    is_configured_host = any(host == item or host.endswith(f".{item}") for item in allowed)
    is_runninghub_cdn_host = bool(RUNNINGHUB_CDN_HOST_PATTERN.fullmatch(host))
    if not is_configured_host and not is_runninghub_cdn_host:
        raise RequestError(403, "Download host is not allowed")
    if require_image:
        clean_path = urllib.parse.unquote(parsed.path).lower()
        if not clean_path.endswith(tuple(IMAGE_EXTENSIONS)):
            raise RequestError(415, "Download URL does not point to an image")
    return url


def _is_safe_image_url(value):
    try:
        _validate_remote_url(value, require_image=True)
    except RequestError:
        return False
    return True


def _response_message(response, default="RunningHub request failed"):
    if not isinstance(response, dict):
        return default
    for key in ("errorMessage", "message", "msg"):
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            clean_value = " ".join(value.split())
            return clean_value[:500]
    return default


# RunningHub 已知代表「帳號同時執行任務數已達上限」的錯誤代碼與關鍵字。
# 804 = APIKEY_TASK_IS_RUNNING，813 = APIKEY_TASK_IS_QUEUED。
_RUNNINGHUB_BUSY_ERROR_CODES = frozenset({"804", "813"})
_RUNNINGHUB_BUSY_KEYWORDS = (
    "task_is_running",
    "task_is_queued",
    "is running",
    "already running",
    "in queue",
    "queue is full",
    "queue_maxed",
    "concurrent",
    "並發",
    "并发",
    "排隊",
    "排队",
    "佔用",
    "占用",
    "運行中",
    "运行中",
    "執行中",
    "执行中",
)
# 一律不重試的永久性錯誤關鍵字（內容審核、餘額不足、參數錯誤等），
# 即使訊息裡也出現了上面的忙碌字眼，只要命中這裡就視為真正失敗。
_RUNNINGHUB_PERMANENT_ERROR_KEYWORDS = (
    "violation",
    "illegal",
    "forbidden",
    "nsfw",
    "content policy",
    "moderation",
    "unauthorized",
    "bad request",
    "invalid parameter",
    "parameter error",
    "balance",
    "insufficient",
    "quota",
    "not found",
    "違規",
    "违规",
    "餘額",
    "余额",
    "不足",
    "審核",
    "审核",
    "參數",
    "参数",
    "欠費",
    "欠费",
)


def _is_runninghub_busy_response(response):
    """判斷 run/workflow 的回應是否代表「帳號同時執行任務數已達上限，稍後重試即可」。"""
    if not isinstance(response, dict):
        return False
    task_id = response.get("taskId")
    if isinstance(task_id, str) and task_id.strip():
        return False
    error_code = str(response.get("errorCode") or "").strip().lower()
    message = _response_message(response, "").lower()
    if not error_code and not message:
        return False
    if any(keyword in message for keyword in _RUNNINGHUB_PERMANENT_ERROR_KEYWORDS):
        return False
    if error_code in _RUNNINGHUB_BUSY_ERROR_CODES:
        return True
    return any(keyword in message for keyword in _RUNNINGHUB_BUSY_KEYWORDS)


def _read_limited(stream, limit):
    chunks = []
    total = 0
    while True:
        chunk = stream.read(min(64 * 1024, limit - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise UpstreamError("RunningHub response is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_json_response(stream, *, allow_non_object=False):
    try:
        raw_body = _read_limited(stream, MAX_UPSTREAM_RESPONSE_BYTES)
        response = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpstreamError("RunningHub returned invalid JSON") from exc
    if allow_non_object:
        valid_response = isinstance(response, (dict, list, str))
    else:
        valid_response = isinstance(response, dict)
    if not valid_response:
        raise UpstreamError("RunningHub returned an invalid response")
    return response


def _http_error_message(error, status_code):
    try:
        raw_body = error.read(MAX_UPSTREAM_RESPONSE_BYTES)
    except OSError:
        raw_body = b""
    if raw_body:
        try:
            response = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            response = None
        message = _response_message(response, "")
        if message:
            return f"RunningHub returned HTTP {status_code}: {message}"
    return f"RunningHub returned HTTP {status_code}"


def _upstream_network_error_message(operation, error):
    reason = getattr(error, "reason", error)
    if isinstance(reason, TimeoutError):
        return f"{operation} timed out"
    detail = " ".join(str(reason).split())[:300]
    return f"{operation} failed: {detail}" if detail else f"{operation} failed"


class ConfigStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._loaded = False
        self._config = {}
        self._local_api_key = ""

    @staticmethod
    def _read_file(path):
        try:
            with open(path, "r", encoding="utf-8") as config_file:
                value = json.load(config_file)
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            LOGGER.warning("Unable to read %s: %s", os.path.basename(path), exc)
            return {}
        if not isinstance(value, dict):
            LOGGER.warning("Ignoring %s because it is not a JSON object", os.path.basename(path))
            return {}
        return value

    def _ensure_loaded(self):
        if self._loaded:
            return
        legacy_config = self._read_file(CONFIG_FILE)
        local_config = self._read_file(LOCAL_CONFIG_FILE)

        # config.json remains compatible for non-secret preferences. API keys
        # are accepted only from the ignored local file or the environment.
        self._config = {
            key: value for key, value in legacy_config.items() if key != "api_key"
        }
        self._config.update(
            {key: value for key, value in local_config.items() if key != "api_key"}
        )
        local_key = local_config.get("api_key", "")
        self._local_api_key = local_key.strip() if isinstance(local_key, str) else ""
        self._loaded = True

    def snapshot(self):
        with self._lock:
            self._ensure_loaded()
            return dict(self._config)

    def api_key(self):
        environment_key = os.environ.get("RUNNINGHUB_API_KEY", "").strip()
        if environment_key:
            return environment_key
        with self._lock:
            self._ensure_loaded()
            return self._local_api_key

    def public_config(self):
        config = self.snapshot()
        return {
            "workflow_id": str(config.get("workflow_id", DEFAULT_WORKFLOW_ID)),
            "node_id": str(config.get("node_id", DEFAULT_NODE_ID)),
            "field_name": str(config.get("field_name", DEFAULT_FIELD_NAME)),
            "output_dir": str(config.get("output_dir", "")),
            "instance_type": str(
                config.get("instance_type", DEFAULT_INSTANCE_TYPE)
            ),
            "apiConfigured": bool(self.api_key()),
        }

    @staticmethod
    def _write_local_file(value):
        temp_path = None
        file_descriptor = None
        try:
            file_descriptor, temp_path = tempfile.mkstemp(
                prefix=".config-", suffix=".tmp", dir=BASE_DIR
            )
            os.fchmod(file_descriptor, 0o600)
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as config_file:
                file_descriptor = None
                json.dump(value, config_file, indent=4, ensure_ascii=False)
                config_file.write("\n")
                config_file.flush()
                os.fsync(config_file.fileno())
            os.replace(temp_path, LOCAL_CONFIG_FILE)
            temp_path = None
        except OSError as exc:
            raise ConfigError() from exc
        finally:
            if file_descriptor is not None:
                try:
                    os.close(file_descriptor)
                except OSError:
                    LOGGER.debug("Unable to close temporary configuration file")
            if temp_path:
                try:
                    os.remove(temp_path)
                except FileNotFoundError:
                    pass
                except OSError:
                    LOGGER.debug("Unable to remove temporary configuration file")

    def update(self, values):
        with self._lock:
            self._ensure_loaded()
            updated_config = dict(self._config)
            updated_config.update(
                {key: value for key, value in values.items() if key != "api_key"}
            )

            local_key = self._local_api_key
            if local_key:
                updated_config["api_key"] = local_key

            self._write_local_file(updated_config)
            self._config = {
                key: value for key, value in updated_config.items() if key != "api_key"
            }
            self._local_api_key = local_key


CONFIG_STORE = ConfigStore()


def load_config():
    """Return cached non-secret configuration for compatibility with callers."""
    return CONFIG_STORE.snapshot()


def save_config(config):
    CONFIG_STORE.update(config)


def _request_workflow_values(data, config):
    workflow_id = data.get("workflowId") or data.get(
        "workflow_id", config.get("workflow_id", DEFAULT_WORKFLOW_ID)
    )
    instance_type = config.get(
        "instance_type", DEFAULT_INSTANCE_TYPE
    )
    workflow_id = _validated_identifier(
        workflow_id,
        "workflowId",
        DEFAULT_WORKFLOW_ID,
        pattern=r"[0-9]{1,32}",
    )
    if workflow_id == COMBINED_CROP_WORKFLOW_ID:
        node_id = DEFAULT_NODE_ID
        field_name = DEFAULT_FIELD_NAME
    else:
        node_id = data.get("nodeId") or data.get(
            "node_id", config.get("node_id", DEFAULT_NODE_ID)
        )
        field_name = config.get("field_name", DEFAULT_FIELD_NAME)
        node_id = _validated_identifier(
            node_id, "nodeId", DEFAULT_NODE_ID, pattern=r"[0-9]{1,32}"
        )
        field_name = _validated_identifier(
            field_name,
            "field_name",
            DEFAULT_FIELD_NAME,
            pattern=r"[A-Za-z][A-Za-z0-9_.-]{0,63}",
        )
    instance_type = _validated_identifier(
        instance_type,
        "instance_type",
        DEFAULT_INSTANCE_TYPE,
        pattern=r"[A-Za-z0-9_.-]{1,32}",
    )
    return workflow_id, node_id, field_name, instance_type


def _request_node_overrides(data):
    raw_overrides = data.get("nodeOverrides", [])
    if raw_overrides is None:
        return []
    if not isinstance(raw_overrides, list) or len(raw_overrides) > 8:
        raise RequestError(400, "nodeOverrides must contain 0 to 8 items")

    overrides = []
    seen = set()
    for item in raw_overrides:
        if not isinstance(item, dict):
            raise RequestError(400, "Each node override must be an object")
        node_id = _validated_identifier(
            item.get("nodeId"),
            "nodeOverrides.nodeId",
            "",
            pattern=r"[0-9]{1,32}",
        )
        field_name = _validated_identifier(
            item.get("fieldName"),
            "nodeOverrides.fieldName",
            "",
            pattern=r"[A-Za-z][A-Za-z0-9_.-]{0,63}",
        )
        field_value = item.get("fieldValue")
        if isinstance(field_value, bool) or not isinstance(
            field_value, (str, int, float)
        ):
            raise RequestError(400, "nodeOverrides.fieldValue must be text or a number")
        field_value = _require_text(
            str(field_value),
            "nodeOverrides.fieldValue",
            max_length=256,
        )
        override_key = (node_id, field_name)
        if override_key in seen:
            raise RequestError(400, "Duplicate node override")
        seen.add(override_key)
        overrides.append(
            {
                "nodeId": node_id,
                "fieldName": field_name,
                "fieldValue": field_value,
            }
        )
    return overrides


def _header_node_overrides(raw_value):
    if not raw_value:
        return []
    if len(raw_value) > 4096:
        raise RequestError(413, "Node override header is too large")
    try:
        decoded_value = json.loads(urllib.parse.unquote(raw_value))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestError(400, "Invalid node override header") from exc
    return _request_node_overrides({"nodeOverrides": decoded_value})


def _header_edit_mask_rect(raw_value):
    if not raw_value:
        return None
    try:
        value = json.loads(urllib.parse.unquote(raw_value))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestError(400, "Invalid edit mask rectangle") from exc
    return _validated_edit_mask_rect(value)


def _extract_crop_parameters(node_overrides):
    parameter_keys = {
        ("229", "value"): "crop_left",
        ("230", "value"): "crop_right",
        ("231", "value"): "crop_top",
        ("232", "value"): "crop_bottom",
        ("184", "left"): "extension_left",
        ("184", "right"): "extension_right",
        ("184", "top"): "extension_top",
        ("184", "bottom"): "extension_bottom",
    }
    parameters = {name: 0 for name in parameter_keys.values()}
    for override in node_overrides:
        parameter_name = parameter_keys.get(
            (override["nodeId"], override["fieldName"])
        )
        if parameter_name is None:
            continue
        raw_value = override["fieldValue"]
        if not re.fullmatch(r"(?:0|[1-9][0-9]*)", raw_value):
            raise RequestError(400, "Step4 crop parameters must be non-negative integers")
        value = int(raw_value)
        if value > CROP_OUTPUT_WIDTH:
            raise RequestError(400, "Step4 crop parameter is too large")
        parameters[parameter_name] = value
    return parameters


def run_runninghub_api(
    url,
    payload,
    api_key,
    method="POST",
    retries=3,
    timeout=None,
    allow_non_object=False,
):
    if not api_key:
        raise RequestError(503, "API Key not configured")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise UpstreamError("Invalid RunningHub endpoint")
    if method not in {"GET", "POST"}:
        raise RequestError(500, "Invalid upstream method")
    if not isinstance(retries, int) or retries < 1 or retries > 3:
        raise RequestError(500, "Invalid retry configuration")

    request_body = (
        json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    )
    request_timeout = timeout or UPSTREAM_TIMEOUT_SECONDS
    for attempt in range(retries):
        request = urllib.request.Request(
            url,
            data=request_body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request, timeout=request_timeout, context=TLS_CONTEXT
            ) as response:
                return _decode_json_response(
                    response, allow_non_object=allow_non_object
                )
        except urllib.error.HTTPError as exc:
            if exc.code in RETRYABLE_HTTP_STATUS and attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise UpstreamError(
                _http_error_message(exc, exc.code), upstream_status=exc.code
            ) from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise UpstreamError(
                _upstream_network_error_message("RunningHub workflow request", exc)
            ) from exc
        except OSError as exc:
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise UpstreamError(
                _upstream_network_error_message("RunningHub workflow request", exc)
            ) from exc
    raise UpstreamError("Unable to reach RunningHub")


def upload_media_to_runninghub(file_path, api_key):
    file_path = _validated_path(file_path, "file", image=True)
    try:
        file_size = os.path.getsize(file_path)
        if file_size > MAX_UPLOAD_BYTES:
            raise RequestError(413, "Image file is too large")
        with open(file_path, "rb") as image_file:
            file_content = image_file.read()
    except OSError as exc:
        raise RequestError(404, "Unable to read image file") from exc

    file_name = _safe_upload_filename(os.path.basename(file_path))
    mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    boundary = "----GameAssetsBoundary"
    quoted_name = urllib.parse.quote(file_name, safe="")
    part_header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{quoted_name}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("utf-8")
    part_footer = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = part_header + file_content + part_footer
    upload_url = "https://www.runninghub.ai/openapi/v2/media/upload/binary"
    for attempt in range(UPLOAD_RETRIES):
        request = urllib.request.Request(
            upload_url,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=UPLOAD_TIMEOUT_SECONDS, context=TLS_CONTEXT
            ) as response:
                return _decode_json_response(response)
        except urllib.error.HTTPError as exc:
            if exc.code in RETRYABLE_HTTP_STATUS and attempt < UPLOAD_RETRIES - 1:
                LOGGER.warning(
                    "RunningHub image upload attempt %d/%d returned HTTP %s; retrying",
                    attempt + 1,
                    UPLOAD_RETRIES,
                    exc.code,
                )
                time.sleep(1.5 * (attempt + 1))
                continue
            raise UpstreamError(
                _http_error_message(exc, exc.code), upstream_status=exc.code
            ) from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            if attempt < UPLOAD_RETRIES - 1:
                LOGGER.warning(
                    "RunningHub image upload attempt %d/%d failed: %s; retrying",
                    attempt + 1,
                    UPLOAD_RETRIES,
                    _upstream_network_error_message("RunningHub image upload", exc),
                )
                time.sleep(1.5 * (attempt + 1))
                continue
            raise UpstreamError(
                _upstream_network_error_message("RunningHub image upload", exc)
            ) from exc
        except OSError as exc:
            if attempt < UPLOAD_RETRIES - 1:
                LOGGER.warning(
                    "RunningHub image upload attempt %d/%d failed: %s; retrying",
                    attempt + 1,
                    UPLOAD_RETRIES,
                    _upstream_network_error_message("RunningHub image upload", exc),
                )
                time.sleep(1.5 * (attempt + 1))
                continue
            raise UpstreamError(
                _upstream_network_error_message("RunningHub image upload", exc)
            ) from exc
    raise UpstreamError("Unable to upload image to RunningHub")


def _validated_edit_mask_rect(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RequestError(400, "editMaskRect must be an object")
    try:
        rect = {
            key: float(value[key])
            for key in ("x", "y", "width", "height")
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise RequestError(400, "editMaskRect is invalid") from exc
    if any(not math.isfinite(item) for item in rect.values()):
        raise RequestError(400, "editMaskRect is invalid")
    if (
        rect["x"] < 0
        or rect["y"] < 0
        or rect["width"] <= 0
        or rect["height"] <= 0
        or rect["x"] + rect["width"] > 1
        or rect["y"] + rect["height"] > 1
    ):
        raise RequestError(400, "editMaskRect must stay within the image")
    return rect


def _prepare_edit_mask_image(file_path, mask_rect, output_dir):
    if Image is None:
        raise RequestError(500, "Pillow is required for rectangular editing")
    rect = _validated_edit_mask_rect(mask_rect)
    if rect is None:
        return file_path
    try:
        with Image.open(file_path) as source:
            image = ImageOps.exif_transpose(source).convert("RGBA")
            width, height = image.size
            alpha = Image.new("L", (width, height), 0)
            left = max(0, min(width - 1, round(rect["x"] * width)))
            top = max(0, min(height - 1, round(rect["y"] * height)))
            right = max(left + 1, min(width, round((rect["x"] + rect["width"]) * width)))
            bottom = max(top + 1, min(height, round((rect["y"] + rect["height"]) * height)))
            alpha.paste(255, (left, top, right, bottom))
            output_path = os.path.join(output_dir, "edit-mask-input.png")
            alpha.save(output_path, format="PNG")
            return output_path
    except (OSError, ValueError) as exc:
        raise RequestError(400, "Unable to create edit mask") from exc


def _validated_edit_mask_image(value):
    """前端黑白 PNG（白色選取），也接受舊版透明背景筆跡。"""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RequestError(400, "editMaskImage must be a non-empty string")
    if len(value) > MAX_EDIT_MASK_IMAGE_BASE64_CHARS:
        raise RequestError(413, "editMaskImage is too large")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RequestError(400, "editMaskImage is not valid base64") from exc


def _prepare_edit_mask_image_from_doodle(file_path, mask_bytes, output_dir):
    if Image is None:
        raise RequestError(500, "Pillow is required for freehand editing")
    if mask_bytes is None:
        return file_path
    try:
        with Image.open(file_path) as source:
            image = ImageOps.exif_transpose(source).convert("RGBA")
            width, height = image.size
            with Image.open(io.BytesIO(mask_bytes)) as mask_source:
                mask_source.load()
                # 前端固定輸出「純黑白、不帶遮罩資訊的常數 alpha」的 PNG（有些瀏覽器就算
                # 畫布用 {alpha:false} 建立，toDataURL 匯出時還是會強制帶一個全部都是
                # 255 的 alpha 通道）。這種常數 alpha 沒有任何遮罩形狀資訊，必須忽略，
                # 改用亮度（黑=0／白=255）判斷；只有在 alpha 真的有變化時才代表它是
                # 有意義的透明度資料，才拿來當遮罩用。
                alpha_channel = None
                if "A" in mask_source.getbands():
                    candidate = mask_source.convert("RGBA").split()[3]
                    low, high = candidate.getextrema()
                    if low != high:
                        alpha_channel = candidate
                if alpha_channel is None:
                    alpha_channel = mask_source.convert("L")
                alpha = alpha_channel.resize((width, height), Image.LANCZOS)
            output_path = os.path.join(output_dir, "edit-mask-input.png")
            alpha.save(output_path, format="PNG")
            return output_path
    except (OSError, ValueError) as exc:
        raise RequestError(400, "Unable to create edit mask") from exc


def _prepare_edit_mask_input(file_path, mask_rect, mask_image_bytes, output_dir):
    """rect（框選）與 doodle（塗鴉）兩種遮罩擇一使用，讓呼叫端不用重複判斷。"""
    if mask_image_bytes is not None:
        return _prepare_edit_mask_image_from_doodle(file_path, mask_image_bytes, output_dir)
    if mask_rect is not None:
        return _prepare_edit_mask_image(file_path, mask_rect, output_dir)
    # 無選取時維持整圖編輯：以全白遮罩允許整張套用生成結果。
    if Image is None:
        raise RequestError(503, "Pillow is required for image editing")
    with Image.open(file_path) as source:
        output_path = os.path.join(output_dir, "edit-mask-input.png")
        Image.new("L", ImageOps.exif_transpose(source).size, 255).save(output_path, format="PNG")
    return output_path


def make_node_info_list(
    workflow_id,
    node_id,
    field_name,
    remote_file_name,
    prompt_text=None,
    prompt_node_id="25",
    node_overrides=None,
    masked_remote_file_name=None,
):
    """Build the node mapping expected by each supported RunningHub workflow."""
    if str(workflow_id) == EDIT_WORKFLOW_ID:
        nodes = [
            {
                "nodeId": "318",
                "fieldName": "image",
                "fieldValue": masked_remote_file_name or remote_file_name,
            },
            {"nodeId": "306", "fieldName": "image", "fieldValue": remote_file_name},
        ]
    elif str(workflow_id) == SHRINK_WORKFLOW_ID:
        nodes = [
            {"nodeId": "257", "fieldName": "image", "fieldValue": remote_file_name}
        ]
    elif str(workflow_id) == COMBINED_CROP_WORKFLOW_ID:
        nodes = [
            {
                "nodeId": DEFAULT_NODE_ID,
                "fieldName": DEFAULT_FIELD_NAME,
                "fieldValue": remote_file_name,
            }
        ]
    else:
        nodes = [
            {"nodeId": str(node_id), "fieldName": field_name, "fieldValue": remote_file_name}
        ]
    if prompt_text and str(workflow_id) != COMBINED_CROP_WORKFLOW_ID:
        nodes.append(
            {
                "nodeId": str(prompt_node_id),
                "fieldName": (
                    "value"
                    if str(workflow_id) == COMBINED_CROP_WORKFLOW_ID
                    else "prompt"
                ),
                "fieldValue": prompt_text,
            }
        )
    if node_overrides and str(workflow_id) != COMBINED_CROP_WORKFLOW_ID:
        nodes.extend(node_overrides)
    return nodes


def run_workflow(workflow_id, node_info_list, instance_type, api_key):
    LOGGER.info(
        "RunningHub workflow %s nodes=%s",
        workflow_id,
        [(item["nodeId"], item["fieldName"]) for item in node_info_list],
    )
    payload = {
        "randomSeed": True,
        "instanceType": instance_type,
        "nodeInfoList": node_info_list,
        "retainSeconds": RUNNINGHUB_RETAIN_SECONDS,
        "usePersonalQueue": False,
    }
    run_url = f"https://www.runninghub.ai/openapi/v2/run/workflow/{workflow_id}"

    response = run_runninghub_api(run_url, payload, api_key)
    attempts = max(RUNNINGHUB_BUSY_RETRY_ATTEMPTS, 1)
    for attempt in range(1, attempts):
        if not _is_runninghub_busy_response(response):
            break
        LOGGER.info(
            "RunningHub workflow %s busy (attempt %d/%d): %s; retrying in %ds",
            workflow_id,
            attempt,
            attempts,
            _response_message(response, ""),
            RUNNINGHUB_BUSY_RETRY_INTERVAL_SECONDS,
        )
        time.sleep(RUNNINGHUB_BUSY_RETRY_INTERVAL_SECONDS)
        response = run_runninghub_api(run_url, payload, api_key)
    if _is_runninghub_busy_response(response):
        waited_seconds = (attempts - 1) * RUNNINGHUB_BUSY_RETRY_INTERVAL_SECONDS
        raise UpstreamError(
            f"RunningHub 目前同時執行的任務已達上限，已等待 {waited_seconds} 秒仍未釋放名額，"
            "請稍後再試，或將「並行」數量調低。"
        )
    return response


def _is_image_output_type(value):
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return normalized in IMAGE_OUTPUT_TYPES or normalized.rsplit("/", 1)[-1] in {
        "bmp",
        "jpeg",
        "jpg",
        "png",
        "webp",
    }


def _safe_result_image_url(value, output_type=None):
    if not isinstance(value, str):
        return None
    try:
        parsed = urllib.parse.urlparse(value)
        clean_path = urllib.parse.unquote(parsed.path).lower()
        _validate_remote_url(
            value,
            require_image=not _is_image_output_type(output_type),
        )
    except RequestError:
        return None
    if _is_image_output_type(output_type) or clean_path.endswith(tuple(IMAGE_EXTENSIONS)):
        return value
    return None


def extract_image_urls(value):
    """Find safe image URLs in direct, wrapped, and typed output responses."""
    urls = []

    def walk(item, output_type=None):
        if isinstance(item, str):
            candidate = _safe_result_image_url(item, output_type)
            if candidate:
                urls.append(candidate)
            stripped = item.lstrip()
            if stripped.startswith(("{", "[")):
                try:
                    walk(json.loads(item))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
        elif isinstance(item, list):
            for child in item:
                walk(child, output_type)
        elif isinstance(item, dict):
            type_hint = (
                item.get("outputType")
                or item.get("output_type")
                or item.get("mimeType")
                or item.get("mime_type")
                or item.get("contentType")
                or item.get("content_type")
                or item.get("fileType")
                or item.get("file_type")
            )
            for key, child in item.items():
                if (
                    isinstance(child, str)
                    and str(key).lower() in IMAGE_URL_KEYS
                ):
                    candidate = _safe_result_image_url(child, type_hint)
                    if candidate:
                        urls.append(candidate)
                    continue
                walk(child, type_hint)

    walk(value)
    return list(dict.fromkeys(urls))


def extract_preferred_image_urls(value, preferred_node_id=None):
    """Prefer output images emitted by one node when a workflow has preview branches."""
    if not preferred_node_id:
        return extract_image_urls(value)

    preferred_urls = []

    def walk(item):
        if isinstance(item, dict):
            node_id = item.get("nodeId", item.get("node_id"))
            if node_id is not None and str(node_id) == str(preferred_node_id):
                preferred_urls.extend(extract_image_urls(item))
                return
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    if preferred_urls:
        return list(dict.fromkeys(preferred_urls))

    all_urls = extract_image_urls(value)
    return all_urls[-1:] if all_urls else []


def extract_runninghub_status(value):
    """Find a terminal task status in either direct or wrapped API responses."""
    statuses = []
    status_keys = ("status", "taskStatus", "task_status", "state")
    known_statuses = {
        "QUEUED",
        "RUNNING",
        "SUCCESS",
        "SUCCEEDED",
        "COMPLETED",
        "DONE",
        "FAILED",
        "FAILURE",
        "ERROR",
        "PENDING",
        "PROCESSING",
        "IN_PROGRESS",
        "WAITING",
    }

    if isinstance(value, dict):
        for key in status_keys:
            status = value.get(key)
            if isinstance(status, str) and status.strip():
                normalized = status.strip().upper()
                if normalized in known_statuses:
                    if normalized in {"SUCCESS", "SUCCEEDED", "COMPLETED", "DONE"}:
                        return "SUCCESS"
                    if normalized in {"FAILED", "FAILURE", "ERROR"}:
                        return "FAILED"
                    return normalized

    def walk(item):
        if isinstance(item, dict):
            for key in status_keys:
                status = item.get(key)
                if isinstance(status, str) and status.strip():
                    statuses.append(status.strip().upper())
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
        elif isinstance(item, str):
            status = item.strip().upper()
            if status in known_statuses:
                statuses.append(status)

    walk(value)
    for status in statuses:
        if status in {"SUCCESS", "SUCCEEDED", "COMPLETED", "DONE"}:
            return "SUCCESS"
    for status in statuses:
        if status in {"FAILED", "FAILURE", "ERROR"}:
            return "FAILED"
    return statuses[0] if statuses else ""


def derive_output_dir(original_path, workflow_mode, configured_output_dir):
    target_leaf = {
        "shrink": "step0___shrink",
        "upscale": "step1___upscale",
        "edit": "step2___edit",
        "outpaint": "step3___outpaint",
        "crop-top": "step4___crop_top",
        "crop-bottom": "step4___crop_bottom",
        "crop-left": "step4___crop_left",
        "crop-right": "step4___crop_right",
        "crop-combined": "step4___crop",
    }.get(workflow_mode, "step3___outpaint")
    if configured_output_dir:
        return _validated_output_directory(configured_output_dir)

    if not os.path.isabs(original_path):
        return _validated_output_directory(os.path.join(BASE_DIR, "outputs", target_leaf))

    source_dir = os.path.dirname(os.path.realpath(original_path))
    parts = source_dir.split(os.sep)
    for index, part in enumerate(parts):
        if re.fullmatch(r"step\d+.*", part, flags=re.IGNORECASE):
            target_dir = os.sep.join(parts[:index] + [target_leaf]) or os.sep
            return _validated_output_directory(target_dir)
    return _validated_output_directory(os.path.join(os.path.dirname(source_dir), target_leaf))


def reserve_output_path(target_dir, filename):
    """Atomically reserve a non-overwriting filename for parallel downloads."""
    stem, suffix = os.path.splitext(filename)
    for counter in range(10000):
        candidate = f"{stem}({counter}){suffix}" if counter else filename
        path = os.path.join(target_dir, candidate)
        try:
            file_descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            return path, file_descriptor
        except FileExistsError:
            continue
        except OSError as exc:
            raise RequestError(500, "Unable to reserve output file") from exc
    raise RequestError(500, "Unable to reserve a unique output filename")


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_handle, code, message, headers, new_url):
        _validate_remote_url(new_url)
        return super().redirect_request(
            request, file_handle, code, message, headers, new_url
        )


def _open_download_url(url):
    opener = urllib.request.build_opener(
        SafeRedirectHandler(),
        urllib.request.HTTPSHandler(context=TLS_CONTEXT),
    )
    request = urllib.request.Request(
        _validate_remote_url(url, require_image=True),
        headers={"Accept": "image/*"},
        method="GET",
    )
    return opener.open(request, timeout=DOWNLOAD_TIMEOUT_SECONDS)


def _download_remote_file(url, target_path, target_fd):
    file_descriptor = target_fd
    try:
        with _open_download_url(url) as source, os.fdopen(file_descriptor, "wb") as destination:
            file_descriptor = None
            content_length = source.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > MAX_DOWNLOAD_BYTES:
                        raise RequestError(413, "Downloaded image is too large")
                except ValueError:
                    LOGGER.debug("Ignoring invalid upstream Content-Length")

            total_bytes = 0
            while True:
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_DOWNLOAD_BYTES:
                    raise RequestError(413, "Downloaded image is too large")
                destination.write(chunk)
    except urllib.error.HTTPError as exc:
        raise UpstreamError(
            f"RunningHub returned HTTP {exc.code}", upstream_status=exc.code
        ) from exc
    except (urllib.error.URLError, ssl.SSLError, ConnectionError, TimeoutError) as exc:
        raise UpstreamError("Unable to download image from RunningHub") from exc
    except OSError as exc:
        raise RequestError(500, "Unable to save downloaded image") from exc
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                LOGGER.debug("Unable to close reserved output file")


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "GameAssets/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)

    def handle(self):
        """Silence normal browser disconnects while reading a request."""
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            LOGGER.debug("Client disconnected before request completed")

    def _allowed_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return None
        configured_origins = {
            item.strip()
            for item in os.environ.get("GAME_ASSETS_ALLOWED_ORIGINS", "").split(",")
            if item.strip()
        }
        configured_origins.update(
            {
                f"http://localhost:{self.server.server_port}",
                f"http://127.0.0.1:{self.server.server_port}",
            }
        )
        if self._is_local_origin(origin):
            return origin
        return origin if origin in configured_origins else None

    @staticmethod
    def _is_local_origin(origin):
        try:
            parsed = urllib.parse.urlparse(origin)
        except ValueError:
            return False
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            return False
        hostname = (parsed.hostname or "").lower().rstrip(".")
        return hostname in {"localhost", "127.0.0.1", "::1"}

    def end_headers(self):
        allowed_origin = self._allowed_origin()
        if allowed_origin:
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, X-File-Name, X-Workflow-Id, X-Node-Id, "
                "X-Prompt-Text, X-Prompt-Node-Id, X-Node-Overrides",
            )
            self.send_header("Vary", "Origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _write_body(self, body):
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            LOGGER.debug("Client disconnected before response completed")

    def _send_json(self, status_code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._write_body(body)

    def _send_text(self, status_code, message):
        body = message.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write_body(body)

    def _read_content_length(self, max_bytes):
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise RequestError(411, "Content-Length is required")
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise RequestError(400, "Invalid Content-Length") from exc
        if content_length < 0:
            raise RequestError(400, "Invalid Content-Length")
        if content_length > max_bytes:
            raise RequestError(413, "Request body is too large")
        return content_length

    def _read_json_body(self):
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise RequestError(415, "Content-Type must be application/json")
        content_length = self._read_content_length(MAX_JSON_BODY_BYTES)
        try:
            raw_body = self.rfile.read(content_length)
        except TimeoutError as exc:
            raise RequestError(408, "Request body read timed out") from exc
        if len(raw_body) != content_length:
            raise RequestError(400, "Incomplete request body")
        try:
            data = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestError(400, "Invalid JSON") from exc
        if not isinstance(data, dict):
            raise RequestError(400, "JSON body must be an object")
        return data

    def _read_raw_upload(self):
        content_length = self._read_content_length(MAX_REQUEST_BODY_BYTES)
        if content_length == 0:
            raise RequestError(400, "Uploaded file is empty")
        try:
            raw_body = self.rfile.read(content_length)
        except TimeoutError as exc:
            raise RequestError(408, "Upload read timed out") from exc
        if len(raw_body) != content_length:
            raise RequestError(400, "Incomplete upload body")
        return raw_body

    def do_OPTIONS(self):
        if self.headers.get("Origin") and not self._allowed_origin():
            self._send_json(403, {"error": "Origin is not allowed"})
            return
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.headers.get("Origin") and not self._allowed_origin():
            self._send_json(403, {"error": "Origin is not allowed"})
            return
        parsed_path = urllib.parse.urlparse(self.path)
        try:
            if parsed_path.path == "/api/config":
                self._send_json(200, CONFIG_STORE.public_config())
                return

            if parsed_path.path == "/api/preview":
                query = urllib.parse.parse_qs(parsed_path.query, keep_blank_values=True)
                file_values = query.get("path", [])
                if len(file_values) != 1:
                    raise RequestError(400, "path is required")
                file_path = _validated_path(file_values[0], "path", image=True)
                try:
                    file_size = os.path.getsize(file_path)
                    if file_size > MAX_PREVIEW_BYTES:
                        raise RequestError(413, "Preview image is too large")
                    with open(file_path, "rb") as image_file:
                        body = image_file.read(MAX_PREVIEW_BYTES + 1)
                except OSError as exc:
                    raise RequestError(404, "Unable to read preview image") from exc
                if len(body) > MAX_PREVIEW_BYTES:
                    raise RequestError(413, "Preview image is too large")
                mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", mime_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self._write_body(body)
                return

            if parsed_path.path in {"/", "/index.html"}:
                index_path = os.path.join(BASE_DIR, "index.html")
                try:
                    with open(index_path, "rb") as index_file:
                        body = index_file.read()
                except OSError as exc:
                    raise RequestError(404, "Frontend not found") from exc
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self._write_body(body)
                return

            self._send_json(404, {"error": "Not Found"})
        except RequestError as exc:
            self._send_json(exc.status_code, {"error": exc.message})

    def _handle_config(self, request_data):
        if "api_key" in request_data:
            raise RequestError(
                400,
                "API key must be configured via RUNNINGHUB_API_KEY or config.local.json",
            )
        config = CONFIG_STORE.snapshot()

        workflow_id = _validated_identifier(
            _optional_text(
                request_data,
                "workflow_id",
                str(config.get("workflow_id", DEFAULT_WORKFLOW_ID)),
            ),
            "workflow_id",
            DEFAULT_WORKFLOW_ID,
            pattern=r"[0-9]{1,32}",
        )
        node_id = _validated_identifier(
            _optional_text(
                request_data, "node_id", str(config.get("node_id", DEFAULT_NODE_ID))
            ),
            "node_id",
            DEFAULT_NODE_ID,
            pattern=r"[0-9]{1,32}",
        )
        field_name = _validated_identifier(
            _optional_text(
                request_data,
                "field_name",
                str(config.get("field_name", DEFAULT_FIELD_NAME)),
            ),
            "field_name",
            DEFAULT_FIELD_NAME,
            pattern=r"[A-Za-z][A-Za-z0-9_.-]{0,63}",
        )
        output_dir = _optional_text(
            request_data, "output_dir", str(config.get("output_dir", "")), max_length=4096
        )
        output_dir = _validated_output_directory(output_dir)
        instance_type = _validated_identifier(
            _optional_text(
                request_data,
                "instance_type",
                str(config.get("instance_type", DEFAULT_INSTANCE_TYPE)),
            ),
            "instance_type",
            DEFAULT_INSTANCE_TYPE,
            pattern=r"[A-Za-z0-9_.-]{1,32}",
        )

        CONFIG_STORE.update(
            {
                "workflow_id": workflow_id,
                "node_id": node_id,
                "field_name": field_name,
                "output_dir": output_dir,
                "instance_type": instance_type,
            }
        )
        self._send_json(
            200,
            {"success": True, "apiConfigured": bool(CONFIG_STORE.api_key())},
        )

    def _handle_scan(self, request_data):
        folder_path = _validated_path(
            _require_text(request_data.get("path"), "path", max_length=4096),
            "path",
            directory=True,
        )
        files = []
        file_metadata = []
        try:
            with os.scandir(folder_path) as entries:
                for entry in entries:
                    if len(files) >= MAX_SCAN_FILES:
                        raise RequestError(413, "Directory contains too many images")
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if os.path.splitext(entry.name)[1].lower() not in IMAGE_EXTENSIONS:
                        continue
                    entry_path = os.path.realpath(entry.path)
                    if _is_within_allowed_root(entry_path):
                        file_stat = entry.stat(follow_symlinks=False)
                        files.append(entry_path)
                        file_metadata.append(
                            {
                                "path": entry_path,
                                "modifiedTime": str(file_stat.st_mtime_ns),
                                "size": file_stat.st_size,
                            }
                        )
        except OSError as exc:
            raise RequestError(500, "Unable to scan directory") from exc
        files.sort()
        file_metadata.sort(key=lambda item: item["path"])
        self._send_json(200, {"files": files, "fileMetadata": file_metadata})

    @staticmethod
    def _workflow_response(upload_response, run_response, preferred_node_id=None):
        if upload_response.get("code") not in (0, "0"):
            raise UpstreamError(_response_message(upload_response, "Image upload failed"))
        data = upload_response.get("data")
        remote_file_name = data.get("fileName") if isinstance(data, dict) else None
        if not isinstance(remote_file_name, str) or not remote_file_name.strip():
            raise UpstreamError("RunningHub did not return an uploaded file name")

        if not isinstance(run_response, dict):
            raise UpstreamError("RunningHub returned an invalid workflow response")
        task_id = run_response.get("taskId")
        error_code = run_response.get("errorCode")
        if not isinstance(task_id, str) or not task_id.strip():
            raise UpstreamError(_response_message(run_response, "Workflow start failed"))
        if error_code not in (None, "", 0, "0"):
            raise UpstreamError(_response_message(run_response, "Workflow start failed"))
        return {
            "success": True,
            "taskId": task_id.strip()[:256],
            "fileName": remote_file_name.strip()[:512],
            "outputUrls": extract_preferred_image_urls(
                run_response, preferred_node_id
            ),
        }

    def _handle_upload_and_run(self, request_data):
        file_path = _validated_path(
            _require_text(request_data.get("filePath"), "filePath", max_length=4096),
            "filePath",
            image=True,
        )
        resize_percent = _validated_resize_percent(request_data.get("resizePercent"))
        config = CONFIG_STORE.snapshot()
        api_key = CONFIG_STORE.api_key()
        if not api_key:
            raise RequestError(503, "API Key not configured")
        workflow_id, node_id, field_name, instance_type = _request_workflow_values(
            request_data, config
        )
        node_overrides = _request_node_overrides(request_data)
        crop_parameters = (
            _extract_crop_parameters(node_overrides)
            if workflow_id == COMBINED_CROP_WORKFLOW_ID
            else None
        )
        prompt = _optional_text(request_data, "prompt", max_length=10000)
        edit_mask_rect = (
            _validated_edit_mask_rect(request_data.get("editMaskRect"))
            if workflow_id == EDIT_WORKFLOW_ID
            else None
        )
        # 「塗鴉」模式：跟框選擇一使用，兩者都有的話塗鴉優先（前端本來就只會傳其中一種）。
        edit_mask_image = (
            _validated_edit_mask_image(request_data.get("editMaskImage"))
            if workflow_id == EDIT_WORKFLOW_ID
            else None
        )
        prompt_node_id = _validated_identifier(
            request_data.get("promptNodeId", "25"),
            "promptNodeId",
            "25",
            pattern=r"[0-9]{1,32}",
        )

        try:
            resize_context = (
                tempfile.TemporaryDirectory(prefix="gameassets-resize-", dir=BASE_DIR)
                if workflow_id in (COMBINED_CROP_WORKFLOW_ID, EDIT_WORKFLOW_ID) or resize_percent != 100
                or edit_mask_rect is not None or edit_mask_image is not None
                else nullcontext()
            )
        except OSError as exc:
            raise RequestError(500, "Unable to stage resized image") from exc
        with resize_context as resize_dir:
            if workflow_id == COMBINED_CROP_WORKFLOW_ID:
                upload_path = _prepare_crop_image_for_workflow(
                    file_path, resize_percent, resize_dir, crop_parameters
                )
            else:
                upload_path = (
                    _resize_image_for_workflow(file_path, resize_percent, resize_dir)
                    if resize_dir
                    else file_path
                )
            upload_response = upload_media_to_runninghub(upload_path, api_key)
            masked_remote_file_name = None
            if workflow_id == EDIT_WORKFLOW_ID:
                masked_path = _prepare_edit_mask_input(
                    upload_path, edit_mask_rect, edit_mask_image, resize_dir
                )
                masked_response = upload_media_to_runninghub(masked_path, api_key)
                if masked_response.get("code") not in (0, "0"):
                    raise UpstreamError(_response_message(masked_response, "Edit mask upload failed"))
                masked_data = masked_response.get("data")
                masked_remote_file_name = (
                    masked_data.get("fileName") if isinstance(masked_data, dict) else None
                )
                if not isinstance(masked_remote_file_name, str) or not masked_remote_file_name.strip():
                    raise UpstreamError("RunningHub did not return an edit mask file name")
            remote_file_name = (
                upload_response.get("data", {}).get("fileName")
                if isinstance(upload_response.get("data"), dict)
                else None
            )
            if upload_response.get("code") not in (0, "0"):
                raise UpstreamError(_response_message(upload_response, "Image upload failed"))
            if not isinstance(remote_file_name, str) or not remote_file_name.strip():
                raise UpstreamError("RunningHub did not return an uploaded file name")

            node_info_list = make_node_info_list(
                workflow_id,
                node_id,
                field_name,
                remote_file_name.strip(),
                prompt or None,
                prompt_node_id,
                node_overrides,
                masked_remote_file_name,
            )
            run_response = run_workflow(workflow_id, node_info_list, instance_type, api_key)
        self._send_json(
            200,
            self._workflow_response(
                upload_response,
                run_response,
                "199" if workflow_id == COMBINED_CROP_WORKFLOW_ID else None,
            ),
        )

    def _handle_debug_export_edit_mask(self, request_data):
        """本機測試用：完全不呼叫 RunningHub，只是把「如果真的送出去」會用到的
        兩張圖片（節點 306 的原圖、節點 318 的遮罩圖）跟對應的請求欄位打包成
        zip 讓使用者下載，方便直接匯入 ComfyUI 手動測試工作流程，不用先跑一次
        真正的批次處理、也不用花 RunningHub 的額度。"""
        file_path = _validated_path(
            _require_text(request_data.get("filePath"), "filePath", max_length=4096),
            "filePath",
            image=True,
        )
        resize_percent = _validated_resize_percent(request_data.get("resizePercent"))
        prompt = _optional_text(request_data, "prompt", max_length=10000)
        edit_mask_rect = _validated_edit_mask_rect(request_data.get("editMaskRect"))
        edit_mask_image = _validated_edit_mask_image(request_data.get("editMaskImage"))
        prompt_node_id = _validated_identifier(
            request_data.get("promptNodeId", "25"),
            "promptNodeId",
            "25",
            pattern=r"[0-9]{1,32}",
        )

        with tempfile.TemporaryDirectory(prefix="gameassets-debug-export-", dir=BASE_DIR) as work_dir:
            main_path = (
                _resize_image_for_workflow(file_path, resize_percent, work_dir)
                if resize_percent != 100
                else file_path
            )
            masked_path = _prepare_edit_mask_input(
                main_path, edit_mask_rect, edit_mask_image, work_dir
            )

            payload_preview = {
                "note": (
                    "僅供本機對照測試，不是真正送給 RunningHub 的請求。"
                    "main.* 對應節點 306；mask.png 對應節點 318，白色編輯、黑色保留。"
                ),
                "workflowId": EDIT_WORKFLOW_ID,
                "resizePercent": resize_percent,
                "editMaskRect": edit_mask_rect,
                "usedDoodleMask": edit_mask_image is not None,
                "promptNodeId": prompt_node_id,
                "prompt": prompt,
            }

            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
                main_ext = os.path.splitext(main_path)[1] or ".png"
                zip_file.write(main_path, arcname=f"main{main_ext}")
                zip_file.write(masked_path, arcname="mask.png")
                zip_file.writestr("prompt.txt", prompt or "")
                with open(os.path.join(BASE_DIR, "api", "2___edit_api.json"), encoding="utf-8") as workflow_file:
                    workflow = json.load(workflow_file)
                workflow["306"]["inputs"]["image"] = f"main{main_ext}"
                workflow["318"]["inputs"]["image"] = "mask.png"
                workflow[prompt_node_id]["inputs"]["prompt"] = prompt or ""
                zip_file.writestr("2___edit_api.json", json.dumps(workflow, ensure_ascii=False, indent=2))
                zip_file.writestr("README.txt", "匯入 2___edit_api.json，將 main 圖上傳至節點 306，mask.png 上傳至節點 318，prompt.txt 貼至節點 25。\n遮罩白色編輯、黑色保留；未選取時為全白。\n網站正式執行前，請更新 RH 雲端工作流並保留相同 workflow ID，重啟 server.py。\n")
                zip_file.writestr(
                    "payload.json",
                    json.dumps(payload_preview, ensure_ascii=False, indent=2),
                )
            body = buffer.getvalue()

        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition", 'attachment; filename="edit-mask-test.zip"'
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._write_body(body)

    def _handle_upload_dragged(self, request_data):
        file_name = _safe_upload_filename(self.headers.get("X-File-Name", "dragged_image.png"))
        resize_percent = _validated_resize_percent(request_data.get("resizePercent"))
        config = CONFIG_STORE.snapshot()
        api_key = CONFIG_STORE.api_key()
        if not api_key:
            raise RequestError(503, "API Key not configured")
        workflow_id, node_id, field_name, instance_type = _request_workflow_values(
            request_data, config
        )
        node_overrides = _request_node_overrides(request_data)
        crop_parameters = (
            _extract_crop_parameters(node_overrides)
            if workflow_id == COMBINED_CROP_WORKFLOW_ID
            else None
        )
        prompt = request_data.get("prompt")
        if prompt is not None:
            prompt = _require_text(prompt, "prompt", max_length=10000, allow_empty=True)
        prompt_node_id = _validated_identifier(
            request_data.get("promptNodeId", "25"),
            "promptNodeId",
            "25",
            pattern=r"[0-9]{1,32}",
        )
        edit_mask_rect = (
            _validated_edit_mask_rect(request_data.get("editMaskRect"))
            if workflow_id == EDIT_WORKFLOW_ID
            else None
        )
        raw_body = self._read_raw_upload()

        try:
            with tempfile.TemporaryDirectory(prefix="gameassets-", dir=BASE_DIR) as temp_dir:
                temp_file_path = os.path.join(temp_dir, file_name)
                with open(temp_file_path, "wb") as temp_file:
                    temp_file.write(raw_body)
                if workflow_id == COMBINED_CROP_WORKFLOW_ID:
                    upload_path = _prepare_crop_image_for_workflow(
                        temp_file_path, resize_percent, temp_dir, crop_parameters
                    )
                else:
                    upload_path = _resize_image_for_workflow(
                        temp_file_path,
                        resize_percent,
                        temp_dir,
                    )
                upload_response = upload_media_to_runninghub(upload_path, api_key)
                if upload_response.get("code") not in (0, "0"):
                    raise UpstreamError(
                        _response_message(upload_response, "Image upload failed")
                    )
                data = upload_response.get("data")
                remote_file_name = data.get("fileName") if isinstance(data, dict) else None
                if not isinstance(remote_file_name, str) or not remote_file_name.strip():
                    raise UpstreamError(
                        "RunningHub did not return an uploaded file name"
                    )
                masked_remote_file_name = None
                if workflow_id == EDIT_WORKFLOW_ID:
                    masked_path = _prepare_edit_mask_input(
                        upload_path, edit_mask_rect, None, temp_dir
                    )
                    masked_response = upload_media_to_runninghub(masked_path, api_key)
                    if masked_response.get("code") not in (0, "0"):
                        raise UpstreamError(
                            _response_message(masked_response, "Edit mask upload failed")
                        )
                    masked_data = masked_response.get("data")
                    masked_remote_file_name = (
                        masked_data.get("fileName")
                        if isinstance(masked_data, dict)
                        else None
                    )
                    if not isinstance(masked_remote_file_name, str) or not masked_remote_file_name.strip():
                        raise UpstreamError(
                            "RunningHub did not return an edit mask file name"
                        )
                node_info_list = make_node_info_list(
                    workflow_id,
                    node_id,
                    field_name,
                    remote_file_name.strip(),
                    prompt or None,
                    prompt_node_id,
                    node_overrides,
                    masked_remote_file_name,
                )
                run_response = run_workflow(
                    workflow_id, node_info_list, instance_type, api_key
                )
        except OSError as exc:
            raise RequestError(500, "Unable to stage uploaded image") from exc

        self._send_json(
            200,
            self._workflow_response(
                upload_response,
                run_response,
                "199" if workflow_id == COMBINED_CROP_WORKFLOW_ID else None,
            ),
        )

    def _handle_status(self, request_data):
        task_id = _require_text(request_data.get("taskId"), "taskId", max_length=256)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", task_id):
            raise RequestError(400, "Invalid taskId")
        api_key = CONFIG_STORE.api_key()
        task_payload = {"taskId": task_id, "apiKey": api_key, "userKey": api_key}
        preferred_node_id = (
            "199" if request_data.get("workflowMode") == "crop-combined" else None
        )

        # V2 workflow runs expose the same status/results payload shown by
        # RunningHub's task page. Keep the legacy status/output calls as a
        # fallback for older workflow endpoints.
        status_response = run_runninghub_api(
            "https://www.runninghub.ai/openapi/v2/query",
            task_payload,
            api_key,
            allow_non_object=True,
        )

        runninghub_status = extract_runninghub_status(status_response)
        status_code = (
            status_response.get("code")
            if isinstance(status_response, dict)
            else None
        )
        output_response = None
        urls = extract_preferred_image_urls(status_response, preferred_node_id)
        if not runninghub_status and not urls:
            status_response = run_runninghub_api(
                "https://www.runninghub.ai/task/openapi/status",
                task_payload,
                api_key,
                allow_non_object=True,
            )
            runninghub_status = extract_runninghub_status(status_response)
            status_code = (
                status_response.get("code")
                if isinstance(status_response, dict)
                else None
            )
            response_for_error = status_response

        if runninghub_status == "SUCCESS" and not urls:
            output_response = run_runninghub_api(
                "https://www.runninghub.ai/task/openapi/outputs",
                task_payload,
                api_key,
                allow_non_object=True,
            )
            urls = extract_preferred_image_urls(output_response, preferred_node_id)

        response_for_error = (
            output_response
            if isinstance(output_response, dict)
            else status_response
        )
        output_code = (
            output_response.get("code")
            if isinstance(output_response, dict)
            else None
        )
        code = output_code if output_code is not None else status_code
        message = (
            response_for_error.get("msg", "")
            if isinstance(response_for_error, dict)
            else ""
        )
        formatted_response = {
            "status": "RUNNING",
            "outputUrl": "",
            "error": "",
            "message": "",
            "upstreamStatus": runninghub_status,
            "upstreamCode": code,
        }

        if urls:
            formatted_response["status"] = "SUCCESS"
            formatted_response["outputUrls"] = urls
        elif runninghub_status == "SUCCESS":
            formatted_response["status"] = "WAITING_OUTPUT"
            formatted_response["message"] = (
                "RunningHub 已完成，但輸出圖片網址尚未回傳；正在等待下載資訊。"
            )
            LOGGER.warning(
                "RunningHub task %s reported SUCCESS without a downloadable image URL",
                task_id,
            )
        elif runninghub_status == "FAILED" or code in (805, "805"):
            formatted_response["status"] = "FAILED"
            failed_reason = (
                response_for_error.get("failedReason", {})
                if isinstance(response_for_error, dict)
                else {}
            )
            if isinstance(failed_reason, dict):
                exception_message = failed_reason.get("exception_message")
                node_name = failed_reason.get("node_name")
            else:
                exception_message = None
                node_name = None
            if isinstance(exception_message, str) and exception_message.strip():
                formatted_response["error"] = (
                    f"[{str(node_name)[:100] if node_name else 'Node'}] "
                    f"{' '.join(exception_message.split())[:500]}"
                )
            else:
                formatted_response["error"] = _response_message(
                    status_response, f"Task failed (code: {message or code})"
                )
        self._send_json(200, formatted_response)

    @staticmethod
    def _download_name(original_path, workflow_mode):
        if os.path.isabs(os.path.expanduser(original_path)):
            safe_original_path = _validated_path(
                original_path, "originalPath", image=True, allow_missing=True
            )
            base_name = os.path.basename(safe_original_path)
        else:
            if "/" in original_path or "\\" in original_path:
                raise RequestError(400, "originalPath must be a file name or allowed path")
            base_name = _safe_upload_filename(original_path)

        name_part, _ = os.path.splitext(base_name)
        if not name_part:
            name_part = "image"
        # Remove prefixes from earlier pipeline steps before applying the
        # current step prefix, including names produced by older versions.
        source_name_part = re.sub(
            r"^(?:(?:4_crop_combined|4_crop_(?:top|bottom|left|right)|4_crop|"
            r"3_outpaint|1_outpaint|2_edit|1_edit|1_upscale|0_shrink)_)+",
            "",
            name_part,
            flags=re.IGNORECASE,
        )
        source_name_part = re.sub(
            r"_(?:original|step0)_",
            "_",
            source_name_part,
            flags=re.IGNORECASE,
        ).strip("_") or "image"
        prefix = {
            "shrink": "0_shrink_",
            "upscale": "1_upscale_",
            "edit": "2_edit_",
            "outpaint": "3_outpaint_",
            "crop-top": "4_crop_",
            "crop-bottom": "4_crop_",
            "crop-left": "4_crop_",
            "crop-right": "4_crop_",
            "crop-combined": "4_crop_",
        }[workflow_mode]
        new_name_part = f"{prefix}{source_name_part}"
        new_name_part = new_name_part.replace("/", "_").replace("\\", "_")
        return new_name_part[:240] or "image"

    def _handle_download(self, request_data):
        image_urls = request_data.get("imageUrls", [])
        single_url = request_data.get("imageUrl", "")
        if single_url and not image_urls:
            image_urls = [single_url]
        if not isinstance(image_urls, list) or not image_urls or len(image_urls) > 8:
            raise RequestError(400, "imageUrls must contain 1 to 8 URLs")
        validated_urls = []
        for image_url in image_urls:
            validated_urls.append(_validate_remote_url(image_url, require_image=True))

        original_path = _require_text(
            request_data.get("originalPath"), "originalPath", max_length=4096
        )
        workflow_mode = _validated_identifier(
            request_data.get("workflowMode", "outpaint"),
            "workflowMode",
            "outpaint",
            pattern=r"(shrink|upscale|edit|outpaint|crop-(combined|top|bottom|left|right))",
        )
        config = CONFIG_STORE.snapshot()
        configured_output_dir = config.get("output_dir", "")
        if configured_output_dir:
            configured_output_dir = _validated_output_directory(configured_output_dir)
        target_dir = derive_output_dir(
            original_path, workflow_mode, configured_output_dir
        )
        target_dir = _validated_output_directory(target_dir)
        new_name_part = self._download_name(original_path, workflow_mode)
        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError as exc:
            raise RequestError(500, "Unable to create output directory") from exc
        if not _is_within_allowed_root(target_dir):
            raise RequestError(403, "Output directory is outside the allowed folders")

        saved_paths = []
        try:
            for index, image_url in enumerate(validated_urls):
                raw_filename = (
                    f"{new_name_part}_{index + 1}.png"
                    if len(validated_urls) > 1
                    else f"{new_name_part}.png"
                )
                target_path, target_fd = reserve_output_path(target_dir, raw_filename)
                try:
                    _download_remote_file(image_url, target_path, target_fd)
                    if workflow_mode == "crop-combined":
                        _normalize_crop_download(target_path)
                except (RequestError, OSError):
                    try:
                        os.remove(target_path)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        LOGGER.debug("Unable to remove failed output file")
                    raise
                saved_paths.append(target_path)
        except (RequestError, OSError):
            for saved_path in saved_paths:
                try:
                    os.remove(saved_path)
                except FileNotFoundError:
                    pass
                except OSError:
                    LOGGER.debug("Unable to remove partial output file")
            raise
        self._send_json(200, {"success": True, "savedPaths": saved_paths})

    def do_POST(self):
        if self.headers.get("Origin") and not self._allowed_origin():
            self._send_json(403, {"error": "Origin is not allowed"})
            return
        parsed_path = urllib.parse.urlparse(self.path)
        try:
            if parsed_path.path == "/api/upload-dragged":
                # Headers carry the workflow metadata for binary uploads.
                request_data = {
                    "workflowId": self.headers.get("X-Workflow-Id"),
                    "nodeId": self.headers.get("X-Node-Id"),
                    "prompt": (
                        urllib.parse.unquote(self.headers["X-Prompt-Text"])
                        if self.headers.get("X-Prompt-Text")
                        else None
                    ),
                    "promptNodeId": self.headers.get("X-Prompt-Node-Id", "25"),
                    "resizePercent": self.headers.get("X-Resize-Percent", "100"),
                    "nodeOverrides": _header_node_overrides(
                        self.headers.get("X-Node-Overrides", "")
                    ),
                    "editMaskRect": _header_edit_mask_rect(
                        self.headers.get("X-Edit-Mask-Rect", "")
                    ),
                }
                self._handle_upload_dragged(request_data)
                return

            json_routes = {
                "/api/config": self._handle_config,
                "/api/scan": self._handle_scan,
                "/api/upload-and-run": self._handle_upload_and_run,
                "/api/status": self._handle_status,
                "/api/download": self._handle_download,
                "/api/debug-export-edit-mask": self._handle_debug_export_edit_mask,
            }
            handler = json_routes.get(parsed_path.path)
            if handler is None:
                self._send_json(404, {"error": "Not Found"})
                return
            request_data = self._read_json_body()
            handler(request_data)
        except RequestError as exc:
            LOGGER.warning(
                "%s %s -> %s: %s",
                self.command,
                parsed_path.path,
                exc.status_code,
                exc.message,
            )
            self._send_json(exc.status_code, {"error": exc.message})


def run_server():
    host = os.environ.get("GAME_ASSETS_HOST", "127.0.0.1")
    port = _bounded_env_int("GAME_ASSETS_PORT", 8000, 1, 65535)
    httpd = ThreadingHTTPServer((host, port), RequestHandler)
    LOGGER.info("GameAssets server running on http://%s:%s", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Stopping GameAssets server")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run_server()
