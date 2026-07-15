"""
J.A.R.V.I.S. — Just A Really Virtuous Intelligent System
Local backend for a Windows desktop-control AI assistant.

Run with:  python app.py
Then open: http://127.0.0.1:5678

This binds to 127.0.0.1 ONLY (not 0.0.0.0), so it is not reachable from your
network — just this machine, just your browser.
"""

import base64
import json
import mimetypes
import os
import re
import time
import uuid

import requests
from flask import Flask, jsonify, request, session, send_from_directory

import tools

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

try:
    import docx as docx_lib  # python-docx
except Exception:
    docx_lib = None

try:
    from faster_whisper import WhisperModel
except Exception:
    WhisperModel = None

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
TASKS_PATH = os.path.join(APP_DIR, "tasks.json")
CHATS_DIR = os.path.join(APP_DIR, "chats")
UPLOADS_DIR = os.path.join(APP_DIR, "uploads")
os.makedirs(CHATS_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("AI_ASSISTANT_SECRET", str(uuid.uuid4()))
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200MB per upload request

# In-memory conversation store:
# {conversation_id: {"id", "messages": [...], "pending": {...} or None,
#                     "pending_notification_id", "title", "created_at", "updated_at"}}
CONVERSATIONS = {}

# In-memory notification store (mirrors risky-action confirmations so the UI
# can show/approve them from a Notifications panel instead of only a modal).
# {notification_id: {"id", "conversation_id", "tool_call_id", "name", "args",
#                     "status": "pending"|"approved"|"denied",
#                     "created_at", "resolved_at"}}
NOTIFICATIONS = {}

# In-memory upload registry, rehydrated from disk at startup (see bottom of
# the "file uploads" section below): {file_id: {id, path, name, ext, category, size, uploaded_at}}
UPLOAD_REGISTRY = {}

SYSTEM_PROMPT = """You are J.A.R.V.I.S. (Just A Really Virtuous Intelligent System), a helpful AI \
assistant with the ability to control the user's Windows computer through a fixed set of tools \
(opening apps, changing settings, checking system info, managing files, and so on).

Rules:
- Only use the tools provided. Never claim to have done something you didn't actually call a tool for.
- Some tools are marked RISKY in their description. You may still call them when they're the right \
  way to help the user — the application itself will pause and ask the human to confirm before \
  anything risky actually happens, so you don't need to ask permission in words first, just call the tool.
- Prefer the most specific tool for the job over run_shell_command. Only use run_shell_command when \
  nothing else covers the task.
- After tools run, summarize what happened in plain, friendly language.
- If a request is ambiguous (e.g. "open my editor" and several are installed), ask a brief \
  clarifying question instead of guessing.
- You cannot see the screen unless you call take_screenshot.
- The user may attach files to a message. Text/PDF/Word contents appear inline as \
  "--- Attached file ---" blocks; images may appear as image content if your model supports vision. \
  Audio/video files are stored on disk but not automatically transcribed — you'll see a note with \
  the file's path instead of its contents.
"""


# ---------------------------------------------------------------------------
# Config: three providers, each with their own settings, persisted locally.
# All three speak the same OpenAI-style /chat/completions shape, so one call
# function (call_openai_compatible) serves all of them — only the base URL,
# auth header, and model id change.
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "provider": "openrouter",  # "openrouter" | "ollama" | "google"
    "openrouter": {"api_key": "", "model": ""},
    "google": {"api_key": "", "model": ""},
    "ollama": {"base_url": "http://localhost:11434", "model": ""},
    "whisper": {"model_size": "base"},
}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    with open(CONFIG_PATH, "r") as f:
        cfg = json.load(f)

    # Migrate the old flat {"api_key", "model"} shape (OpenRouter-only) if present.
    if "openrouter" not in cfg and ("api_key" in cfg or "model" in cfg):
        cfg = {
            "provider": "openrouter",
            "openrouter": {"api_key": cfg.get("api_key", ""), "model": cfg.get("model", "")},
            "google": {"api_key": "", "model": ""},
            "ollama": {"base_url": "http://localhost:11434", "model": ""},
        }

    # Fill in anything missing from a partially-saved config.
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    merged["provider"] = cfg.get("provider", merged["provider"])
    for key in ("openrouter", "google", "ollama", "whisper"):
        merged[key].update(cfg.get(key, {}))
    return merged


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f)


@app.route("/api/config", methods=["GET"])
def get_config():
    cfg = load_config()

    def mask(k):
        return ("•" * 8 + k[-4:]) if k else ""

    return jsonify({
        "provider": cfg["provider"],
        "openrouter": {"has_key": bool(cfg["openrouter"]["api_key"]),
                        "masked_key": mask(cfg["openrouter"]["api_key"]),
                        "model": cfg["openrouter"]["model"]},
        "google": {"has_key": bool(cfg["google"]["api_key"]),
                   "masked_key": mask(cfg["google"]["api_key"]),
                   "model": cfg["google"]["model"]},
        "ollama": {"base_url": cfg["ollama"]["base_url"], "model": cfg["ollama"]["model"]},
        "whisper": {"model_size": cfg["whisper"]["model_size"], "available": WhisperModel is not None},
    })


@app.route("/api/config", methods=["POST"])
def set_config():
    data = request.get_json(force=True)
    cfg = load_config()

    if "provider" in data and data["provider"] in ("openrouter", "google", "ollama"):
        cfg["provider"] = data["provider"]

    for provider in ("openrouter", "google"):
        incoming = data.get(provider)
        if incoming:
            if incoming.get("api_key"):
                cfg[provider]["api_key"] = incoming["api_key"].strip()
            if "model" in incoming:
                cfg[provider]["model"] = incoming["model"].strip()

    incoming_ollama = data.get("ollama")
    if incoming_ollama:
        if "base_url" in incoming_ollama and incoming_ollama["base_url"].strip():
            cfg["ollama"]["base_url"] = incoming_ollama["base_url"].strip().rstrip("/")
        if "model" in incoming_ollama:
            cfg["ollama"]["model"] = incoming_ollama["model"].strip()

    incoming_whisper = data.get("whisper")
    if incoming_whisper and incoming_whisper.get("model_size"):
        size = incoming_whisper["model_size"].strip()
        if size in ("tiny", "base", "small", "medium", "large-v3"):
            cfg["whisper"]["model_size"] = size

    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/ollama/models", methods=["GET"])
def ollama_models():
    """Convenience proxy so the UI can list models already pulled in Ollama."""
    cfg = load_config()
    base = cfg["ollama"]["base_url"].rstrip("/")
    try:
        resp = requests.get(f"{base}/api/tags", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        names = [m["name"] for m in data.get("models", [])]
        return jsonify({"ok": True, "models": names})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Couldn't reach Ollama at {base}: {e}"})


# ---------------------------------------------------------------------------
# Voice input: local Whisper transcription (faster-whisper). The model is
# loaded lazily on first use and cached in memory; switching model_size in
# Settings reloads it. Everything runs on this machine — no cloud STT.
# ---------------------------------------------------------------------------

_whisper_model = None
_whisper_model_size = None


def get_whisper_model(size):
    global _whisper_model, _whisper_model_size
    if WhisperModel is None:
        return None
    if _whisper_model is None or _whisper_model_size != size:
        _whisper_model = WhisperModel(size, device="cpu", compute_type="int8")
        _whisper_model_size = size
    return _whisper_model


@app.route("/api/transcribe", methods=["POST"])
def transcribe():
    if WhisperModel is None:
        return jsonify({"ok": False, "error": "faster-whisper isn't installed on the server. Run: pip install faster-whisper"}), 500
    if "audio" not in request.files:
        return jsonify({"ok": False, "error": "No audio in request."}), 400

    f = request.files["audio"]
    cfg = load_config()
    model_size = cfg["whisper"]["model_size"]

    ext = os.path.splitext(f.filename or "")[1] or mimetypes.guess_extension(f.mimetype or "") or ".webm"
    tmp_path = os.path.join(UPLOADS_DIR, f".tmp_{uuid.uuid4()}{ext}")
    f.save(tmp_path)

    try:
        model = get_whisper_model(model_size)
        if model is None:
            return jsonify({"ok": False, "error": "faster-whisper isn't installed on the server."}), 500
        segments, info = model.transcribe(tmp_path, beam_size=5, vad_filter=True)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return jsonify({"ok": True, "text": text, "language": info.language})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Transcription failed: {e}"}), 500
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# File uploads: text/PDF/Word contents get extracted and inlined into the
# chat message; images get base64-inlined for vision-capable models; audio
# and video are stored and referenced by path but not transcribed.
# ---------------------------------------------------------------------------

TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".html", ".css",
             ".log", ".yml", ".yaml", ".xml", ".ini", ".bat", ".ps1"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
IMAGE_INLINE_LIMIT = 6 * 1024 * 1024  # don't inline images larger than this as base64


def classify_ext(ext):
    ext = ext.lower()
    if ext == ".pdf":
        return "pdf"
    if ext == ".docx":
        return "docx"
    if ext == ".doc":
        return "doc_legacy"  # old binary Word format — not supported for extraction
    if ext in TEXT_EXTS:
        return "text"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    return "other"


def human_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def safe_filename(name):
    name = os.path.basename(name or "file")
    name = re.sub(r"[^A-Za-z0-9 ._()\-]", "_", name).strip()
    return name or "file"


def extract_plain_text(path, max_chars=8000):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(max_chars + 1)
        truncated = len(content) > max_chars
        return content[:max_chars], truncated
    except Exception as e:
        return f"[Could not read file: {e}]", False


def extract_pdf_text(path, max_chars=8000):
    if PdfReader is None:
        return "[PDF text extraction unavailable — 'pypdf' isn't installed on the server]", False
    try:
        reader = PdfReader(path)
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        truncated = len(text) > max_chars
        return text[:max_chars], truncated
    except Exception as e:
        return f"[Could not extract PDF text: {e}]", False


def extract_docx_text(path, max_chars=8000):
    if docx_lib is None:
        return "[.docx text extraction unavailable — 'python-docx' isn't installed on the server]", False
    try:
        d = docx_lib.Document(path)
        text = "\n".join(p.text for p in d.paragraphs)
        truncated = len(text) > max_chars
        return text[:max_chars], truncated
    except Exception as e:
        return f"[Could not extract .docx text: {e}]", False


def register_upload(path, original_name, file_id=None):
    file_id = file_id or str(uuid.uuid4())
    ext = os.path.splitext(original_name)[1].lower()
    UPLOAD_REGISTRY[file_id] = {
        "id": file_id,
        "path": path,
        "name": original_name,
        "ext": ext,
        "category": classify_ext(ext),
        "size": os.path.getsize(path) if os.path.exists(path) else 0,
        "uploaded_at": os.path.getmtime(path) if os.path.exists(path) else time.time(),
    }
    return UPLOAD_REGISTRY[file_id]


def rehydrate_uploads_registry():
    """Repopulates UPLOAD_REGISTRY from disk at startup, since the registry
    itself is in-memory but the files (and their ids, encoded in the
    filename) persist across restarts."""
    if not os.path.isdir(UPLOADS_DIR):
        return
    for fname in os.listdir(UPLOADS_DIR):
        if fname in ("README.md", ".gitkeep"):
            continue
        path = os.path.join(UPLOADS_DIR, fname)
        if not os.path.isfile(path):
            continue
        if len(fname) > 37 and fname[36] == "_":
            file_id, original_name = fname[:36], fname[37:]
        else:
            file_id, original_name = str(uuid.uuid4()), fname
        if file_id not in UPLOAD_REGISTRY:
            register_upload(path, original_name, file_id)


def build_attachment_blocks(file_ids, max_chars_per_file=8000):
    """Turns a list of previously-uploaded file ids into (extra_text, image_blocks)
    to merge into an outgoing chat message."""
    extra_text_parts = []
    image_blocks = []
    for fid in file_ids or []:
        info = UPLOAD_REGISTRY.get(fid)
        if not info or not os.path.exists(info["path"]):
            extra_text_parts.append(f"\n\n[Attachment {fid} could not be found on the server.]")
            continue

        name, category, path, size = info["name"], info["category"], info["path"], info["size"]

        if category == "text":
            text, truncated = extract_plain_text(path, max_chars_per_file)
            suffix = "\n[...truncated...]" if truncated else ""
            extra_text_parts.append(
                f"\n\n--- Attached file: {name} ---\n{text}{suffix}\n--- end of attachment ---"
            )
        elif category == "pdf":
            text, truncated = extract_pdf_text(path, max_chars_per_file)
            suffix = "\n[...truncated...]" if truncated else ""
            extra_text_parts.append(
                f"\n\n--- Attached file: {name} (PDF) ---\n{text}{suffix}\n--- end of attachment ---"
            )
        elif category == "docx":
            text, truncated = extract_docx_text(path, max_chars_per_file)
            suffix = "\n[...truncated...]" if truncated else ""
            extra_text_parts.append(
                f"\n\n--- Attached file: {name} (Word) ---\n{text}{suffix}\n--- end of attachment ---"
            )
        elif category == "doc_legacy":
            extra_text_parts.append(
                f"\n\n[Attached file: {name} ({human_size(size)}) — old .doc format isn't supported "
                f"for text extraction. Save it as .docx and re-upload if you want me to read it. "
                f"Stored at {path}.]"
            )
        elif category == "image" and size <= IMAGE_INLINE_LIMIT:
            try:
                with open(path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                mime = mimetypes.guess_type(name)[0] or "image/png"
                image_blocks.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
                extra_text_parts.append(f"\n\n[Attached image: {name}]")
            except Exception as e:
                extra_text_parts.append(f"\n\n[Could not read image {name}: {e}]")
        elif category == "image":
            extra_text_parts.append(
                f"\n\n[Attached image: {name} ({human_size(size)}) — too large to inline "
                f"(limit {human_size(IMAGE_INLINE_LIMIT)}). Stored at {path}.]"
            )
        elif category in ("audio", "video"):
            extra_text_parts.append(
                f"\n\n[Attached {category} file: {name} ({human_size(size)}) — stored at {path}. "
                f"I can't automatically listen to or watch this yet; describe what's in it if you "
                f"want me to act on its contents.]"
            )
        else:
            extra_text_parts.append(
                f"\n\n[Attached file: {name} ({human_size(size)}) — stored at {path}. "
                f"I don't have a way to read this file type's contents.]"
            )

    return "".join(extra_text_parts), image_blocks


@app.route("/api/upload", methods=["POST"])
def upload_files():
    incoming = request.files.getlist("files")
    if not incoming:
        return jsonify({"ok": False, "error": "No files in request."}), 400

    results = []
    for f in incoming:
        if not f.filename:
            continue
        original = safe_filename(f.filename)
        file_id = str(uuid.uuid4())
        stored_name = f"{file_id}_{original}"
        path = os.path.join(UPLOADS_DIR, stored_name)
        f.save(path)
        info = register_upload(path, original, file_id)
        results.append({k: info[k] for k in ("id", "name", "ext", "category", "size")})

    return jsonify({"ok": True, "files": results})


@app.route("/api/uploads", methods=["GET"])
def list_uploads():
    items = [
        {k: v[k] for k in ("id", "name", "ext", "category", "size", "uploaded_at")}
        for v in UPLOAD_REGISTRY.values()
    ]
    items.sort(key=lambda x: x["uploaded_at"], reverse=True)
    return jsonify({"files": items[:200]})


rehydrate_uploads_registry()


# ---------------------------------------------------------------------------
# Tasks: a simple persistent to-do list of things you want JARVIS to do,
# separate from the running chat transcript.
# ---------------------------------------------------------------------------

def load_tasks():
    if not os.path.exists(TASKS_PATH):
        return []
    try:
        with open(TASKS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_tasks(tasks):
    with open(TASKS_PATH, "w", encoding="utf-8") as f:
        json.dump(tasks, f, indent=2)


@app.route("/api/tasks", methods=["GET"])
def get_tasks():
    return jsonify({"tasks": load_tasks()})


@app.route("/api/tasks", methods=["POST"])
def add_task():
    data = request.get_json(force=True)
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Task text is required."}), 400
    tasks = load_tasks()
    task = {"id": str(uuid.uuid4()), "text": text, "done": False, "created_at": time.time()}
    tasks.insert(0, task)
    save_tasks(tasks)
    return jsonify({"ok": True, "task": task})


@app.route("/api/tasks/<task_id>", methods=["POST"])
def update_task(task_id):
    data = request.get_json(force=True)
    tasks = load_tasks()
    for t in tasks:
        if t["id"] == task_id:
            if "done" in data:
                t["done"] = bool(data["done"])
            if "text" in data and data["text"].strip():
                t["text"] = data["text"].strip()
            save_tasks(tasks)
            return jsonify({"ok": True, "task": t})
    return jsonify({"ok": False, "error": "Task not found."}), 404


@app.route("/api/tasks/<task_id>", methods=["DELETE"])
def delete_task(task_id):
    tasks = load_tasks()
    remaining = [t for t in tasks if t["id"] != task_id]
    if len(remaining) == len(tasks):
        return jsonify({"ok": False, "error": "Task not found."}), 404
    save_tasks(remaining)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory("templates", "index.html")


# ---------------------------------------------------------------------------
# Provider call — OpenRouter, Google AI Studio (Gemini), and Ollama all speak
# the same OpenAI-style chat/completions shape, including "tools".
# ---------------------------------------------------------------------------

PROVIDER_ENDPOINTS = {
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
}


def resolve_provider(cfg):
    """Returns (provider, endpoint_url, api_key, model, error_message_or_None)."""
    provider = cfg.get("provider", "openrouter")

    if provider in ("openrouter", "google"):
        section = cfg[provider]
        label = "OpenRouter" if provider == "openrouter" else "Google AI Studio"
        if not section["api_key"]:
            return provider, None, None, None, f"No {label} API key configured yet. Add one in Settings."
        if not section["model"]:
            return provider, None, None, None, f"No model selected for {label} yet. Add one in Settings."
        return provider, PROVIDER_ENDPOINTS[provider], section["api_key"], section["model"], None

    if provider == "ollama":
        section = cfg["ollama"]
        if not section["model"]:
            return provider, None, None, None, "No Ollama model selected. Pull one (e.g. 'ollama pull llama3.1') then set it in Settings."
        base = section["base_url"].rstrip("/")
        return provider, f"{base}/v1/chat/completions", "ollama", section["model"], None

    return provider, None, None, None, "Unknown provider configured."


def call_openai_compatible(endpoint, api_key, model, messages, provider):
    headers = {"Content-Type": "application/json"}
    if provider != "ollama":
        headers["Authorization"] = f"Bearer {api_key}"
    if provider == "openrouter":
        headers["HTTP-Referer"] = "http://127.0.0.1:5678"
        headers["X-Title"] = "AI Desktop Assistant"

    payload = {"model": model, "messages": messages, "tools": tools.TOOL_SPECS}
    resp = requests.post(endpoint, headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    return resp.json()


def run_agent_loop(conv):
    """Advance the conversation until the model produces a plain text reply,
    it wants to call a risky tool (pause for confirmation), or we hit a safety cap."""
    cfg = load_config()
    provider, endpoint, api_key, model, err = resolve_provider(cfg)
    if err:
        return {"type": "error", "message": err}

    for _ in range(8):  # hard cap on tool-call round-trips per turn
        try:
            data = call_openai_compatible(endpoint, api_key, model, conv["messages"], provider)
        except requests.HTTPError as e:
            detail = ""
            try:
                detail = e.response.json().get("error", {}).get("message", "")
            except Exception:
                pass
            return {"type": "error", "message": f"{provider} error: {detail or e}"}
        except requests.ConnectionError as e:
            hint = " Is 'ollama serve' running?" if provider == "ollama" else ""
            return {"type": "error", "message": f"Couldn't reach {provider}.{hint} ({e})"}
        except Exception as e:
            return {"type": "error", "message": f"Request to {provider} failed: {e}"}

        if "error" in data:
            return {"type": "error", "message": data["error"].get("message", str(data["error"]))}

        choice = data["choices"][0]
        msg = choice["message"]
        conv["messages"].append(msg)

        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            return {"type": "final", "message": msg.get("content") or ""}

        for tc in tool_calls:
            name = tc["function"]["name"]
            raw_args = tc["function"]["arguments"]
            # Most OpenAI-compatible servers send arguments as a JSON string;
            # be tolerant of servers that already hand back a parsed dict.
            if isinstance(raw_args, dict):
                args = raw_args
            else:
                try:
                    args = json.loads(raw_args or "{}")
                except json.JSONDecodeError:
                    args = {}

            if tools.is_risky(name):
                # Pause the whole loop and hand control back to the browser.
                conv["pending"] = {"tool_call_id": tc["id"], "name": name, "args": args}
                notif_id = create_notification(conv, name, args, tc["id"])
                return {
                    "type": "confirm",
                    "notification_id": notif_id,
                    "tool_call_id": tc["id"],
                    "name": name,
                    "args": args,
                    "assistant_note": msg.get("content") or "",
                }

            result = tools.run_tool(name, args)
            conv["messages"].append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(result),
            })
        # loop again so the model can see the tool results

    return {"type": "final", "message": "(Stopped after several tool calls in a row — let me know if you'd like me to continue.)"}


def get_conv(conv_id):
    if conv_id in CONVERSATIONS:
        return CONVERSATIONS[conv_id]

    # Not in memory — maybe it's a chat saved before a server restart. Load it
    # back in rather than silently starting fresh and overwriting the file.
    path = chat_file_path(conv_id)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            CONVERSATIONS[conv_id] = {
                "id": conv_id,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + saved.get("messages", []),
                "pending": None,
                "pending_notification_id": None,
                "title": saved.get("title"),
                "created_at": saved.get("created_at", time.time()),
                "updated_at": saved.get("updated_at", time.time()),
            }
            return CONVERSATIONS[conv_id]
        except Exception:
            pass  # fall through and start fresh if the file is unreadable

    now = time.time()
    CONVERSATIONS[conv_id] = {
        "id": conv_id,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}],
        "pending": None,
        "pending_notification_id": None,
        "title": None,
        "created_at": now,
        "updated_at": now,
    }
    return CONVERSATIONS[conv_id]


def create_notification(conv, name, args, tool_call_id):
    notif_id = str(uuid.uuid4())
    NOTIFICATIONS[notif_id] = {
        "id": notif_id,
        "conversation_id": conv["id"],
        "tool_call_id": tool_call_id,
        "name": name,
        "args": args,
        "status": "pending",
        "created_at": time.time(),
        "resolved_at": None,
    }
    conv["pending_notification_id"] = notif_id
    return notif_id


def chat_file_path(conv_id):
    return os.path.join(CHATS_DIR, f"{conv_id}.json")


def save_conversation_to_disk(conv):
    """Writes the full conversation (minus the system prompt) to chats/<id>.json
    so chat history survives server restarts and is easy to browse as plain files."""
    conv["updated_at"] = time.time()
    payload = {
        "id": conv["id"],
        "title": conv["title"],
        "created_at": conv["created_at"],
        "updated_at": conv["updated_at"],
        "messages": [m for m in conv["messages"] if m.get("role") != "system"],
    }
    try:
        with open(chat_file_path(conv["id"]), "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
    except Exception as e:
        # Saving to disk is a nice-to-have; never let it break the chat itself.
        print(f"Warning: couldn't save chat {conv['id']} to disk: {e}")


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(force=True)
    conv_id = data.get("conversation_id") or str(uuid.uuid4())
    user_message = data.get("message", "")
    attachment_ids = data.get("attachment_ids") or []

    conv = get_conv(conv_id)
    if conv["pending"] is not None:
        return jsonify({"conversation_id": conv_id, "type": "error",
                         "message": "There's a pending confirmation — check Notifications to approve or deny it first."})

    if conv["title"] is None:
        title_source = user_message or "(file attachment)"
        conv["title"] = (title_source[:60] + "…") if len(title_source) > 60 else title_source

    extra_text, image_blocks = build_attachment_blocks(attachment_ids)
    full_text = user_message + extra_text
    if image_blocks:
        content = [{"type": "text", "text": full_text}] + image_blocks
    else:
        content = full_text

    conv["messages"].append({"role": "user", "content": content})
    result = run_agent_loop(conv)
    result["conversation_id"] = conv_id
    save_conversation_to_disk(conv)
    return jsonify(result)


@app.route("/api/confirm", methods=["POST"])
def confirm():
    data = request.get_json(force=True)
    conv_id = data.get("conversation_id")
    approved = bool(data.get("approved"))
    conv = CONVERSATIONS.get(conv_id)
    if not conv or not conv.get("pending"):
        return jsonify({"type": "error", "message": "No pending action for this conversation."})

    pending = conv["pending"]
    conv["pending"] = None

    notif_id = conv.get("pending_notification_id")
    conv["pending_notification_id"] = None
    if notif_id and notif_id in NOTIFICATIONS:
        NOTIFICATIONS[notif_id]["status"] = "approved" if approved else "denied"
        NOTIFICATIONS[notif_id]["resolved_at"] = time.time()

    if approved:
        result = tools.run_tool(pending["name"], pending["args"])
    else:
        result = {"ok": False, "cancelled_by_user": True}

    conv["messages"].append({
        "role": "tool",
        "tool_call_id": pending["tool_call_id"],
        "content": json.dumps(result),
    })

    out = run_agent_loop(conv)
    out["conversation_id"] = conv_id
    save_conversation_to_disk(conv)
    return jsonify(out)


@app.route("/api/notifications", methods=["GET"])
def list_notifications():
    items = sorted(NOTIFICATIONS.values(), key=lambda n: n["created_at"], reverse=True)
    return jsonify({"notifications": items[:50]})


@app.route("/api/chats", methods=["GET"])
def list_chats():
    """Lists saved chats straight from chats/*.json on disk, so history
    survives a server restart even though CONVERSATIONS is in-memory."""
    items = []
    for fname in os.listdir(CHATS_DIR):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(CHATS_DIR, fname), "r", encoding="utf-8") as f:
                data = json.load(f)
            items.append({
                "id": data.get("id"),
                "title": data.get("title") or "(untitled)",
                "updated_at": data.get("updated_at", 0),
            })
        except Exception:
            continue
    items.sort(key=lambda c: c["updated_at"], reverse=True)
    return jsonify({"chats": items[:100]})


@app.route("/api/chats/<conv_id>", methods=["GET"])
def get_chat(conv_id):
    path = chat_file_path(conv_id)
    if not os.path.exists(path):
        return jsonify({"ok": False, "error": "No saved chat with that id."}), 404
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return jsonify({"ok": True, "chat": data})


@app.route("/api/system-info", methods=["GET"])
def system_info():
    return jsonify(tools.get_system_info())


if __name__ == "__main__":
    print("AI Desktop Assistant running at http://127.0.0.1:5678")
    app.run(host="127.0.0.1", port=5678, debug=False)
