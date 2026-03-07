from flask import Flask, request, jsonify
from flask_cors import CORS
import subprocess
import tempfile
import os
import glob
import base64
import re

app = Flask(__name__)

CORS(app, resources={r"/*": {
    "origins": [
        "https://fz-online-compiler.vercel.app",
        "http://localhost:5173",
        "http://localhost:4173",
    ],
    "methods": ["GET", "POST", "OPTIONS"],
    "allow_headers": ["Content-Type"],
}})

@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin", "")
    allowed_origins = [
        "https://fz-online-compiler.vercel.app",
        "http://localhost:5173",
        "http://localhost:4173",
    ]
    if origin in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

FIRMWARE_URLS = {
    "official":    "https://update.flipperzero.one/firmware/directory.json",
    "unleashed":   "https://up.unleashedflip.com/directory.json",
    "roguemaster": "https://up.roguemaster.net/directory.json",
    "momentum":    "https://up.momentum-fw.dev/firmware/directory.json",
}

UFBT_UPDATE_TIMEOUT = 120
UFBT_BUILD_TIMEOUT  = 300   # 5 min — handles large projects
GIT_CLONE_TIMEOUT   = 120   # 2 min — handles big repos


@app.route("/health", methods=["GET"])
@app.route("/keep-alive", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


def is_safe_git_url(url: str) -> bool:
    """Accept any public GitHub HTTPS URL (with or without .git, subpaths ok)."""
    return bool(re.match(
        r'^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(\.git)?(/.*)?$', url
    ))


def find_fap_output(build_dir: str):
    """Search all common ufbt output locations for a compiled .fap file."""
    patterns = [
        os.path.join(build_dir, "dist", "**", "*.fap"),
        os.path.join(build_dir, ".ufbt", "build", "**", "*.fap"),
        os.path.join(build_dir, "build", "**", "*.fap"),
        os.path.join(build_dir, "**", "*.fap"),   # fallback — search everywhere
    ]
    for pattern in patterns:
        hits = glob.glob(pattern, recursive=True)
        # Exclude any .fap that was already in the repo (pre-built)
        hits = [h for h in hits if ".ufbt" in h or "dist" in h or "build" in h]
        if hits:
            return hits[0]
    return None


def auto_generate_fam(c_content: str, app_name: str) -> str:
    """
    Generate a minimal application.fam by scanning the C source for common
    defines/strings, falling back to safe defaults if nothing is found.
    """
    # Try to extract appid
    appid_match = re.search(r'#define\s+APP_ID\s+"([^"]+)"', c_content)
    if not appid_match:
        appid_match = re.search(r'appid\s*=\s*"([^"]+)"', c_content)
    appid = appid_match.group(1) if appid_match else app_name

    # Try to extract app name string
    name_match = re.search(r'#define\s+APP_NAME\s+"([^"]+)"', c_content)
    if not name_match:
        name_match = re.search(r'name\s*=\s*"([^"]+)"', c_content)
    name = name_match.group(1) if name_match else app_name.replace("_", " ").title()

    # Try to extract version
    ver_match = re.search(r'#define\s+APP_VERSION\s+"([^"]+)"', c_content)
    version = ver_match.group(1) if ver_match else "1.0"

    # Try to detect entry point — common patterns
    entry_match = re.search(r'int32_t\s+(\w+)\s*\(\s*void\s*\)', c_content)
    if not entry_match:
        entry_match = re.search(r'int32_t\s+(\w+_app)\s*\(', c_content)
    entry = entry_match.group(1) if entry_match else f"{app_name}_app"

    return f"""App(
    appid="{appid}",
    name="{name}",
    apptype=FlipperAppType.EXTERNAL,
    entry_point="{entry}",
    requires=["gui"],
    stack_size=2 * 1024,
    fap_version="{version}",
)
"""


def do_compile(build_dir: str, firmware: str):
    """
    Run ufbt update + ufbt build inside build_dir.
    ufbt only compiles sources declared in application.fam — it ignores
    everything else in the directory, so we never need to filter files.
    Returns (fap_bytes, error_str).
    """
    sdk_url = FIRMWARE_URLS.get(firmware, FIRMWARE_URLS["official"])

    # Each firmware gets its own ufbt home to avoid SDK cache collisions
    ufbt_home = os.path.join(os.path.expanduser("~"), f".ufbt_{firmware}")
    env = {**os.environ, "UFBT_HOME": ufbt_home}

    # ── Step 1: pull/update the SDK ──────────────────────────────────────────
    try:
        update = subprocess.run(
            ["python3", "-m", "ufbt", "update", f"--index-url={sdk_url}"],
            cwd=build_dir, capture_output=True, text=True,
            timeout=UFBT_UPDATE_TIMEOUT, env=env
        )
    except subprocess.TimeoutExpired:
        return None, (
            "SDK update timed out (120s).\n"
            "The firmware index server may be slow — please try again."
        )

    if update.returncode != 0:
        return None, f"SDK update failed:\n{(update.stderr or update.stdout).strip()}"

    # ── Step 2: build ─────────────────────────────────────────────────────────
    try:
        build = subprocess.run(
            ["python3", "-m", "ufbt"],
            cwd=build_dir, capture_output=True, text=True,
            timeout=UFBT_BUILD_TIMEOUT, env=env
        )
    except subprocess.TimeoutExpired:
        return None, (
            "Compilation timed out (5 min).\n"
            "The project may be unusually large — please try again."
        )

    if build.returncode != 0:
        error_output = ((build.stderr or "") + "\n" + (build.stdout or "")).strip()
        return None, f"Compile error:\n{error_output}"

    # ── Step 3: locate the .fap ───────────────────────────────────────────────
    fap_path = find_fap_output(build_dir)
    if not fap_path:
        return None, (
            "Build reported success but no .fap file was found.\n\n"
            f"stdout:\n{build.stdout}\n\nstderr:\n{build.stderr}"
        )

    with open(fap_path, "rb") as f:
        return f.read(), None


# ── /compile  (file upload mode) ─────────────────────────────────────────────

@app.route("/compile", methods=["POST"])
def compile_files():
    data        = request.json
    c_content   = data.get("cFileContent", "")
    fam_content = data.get("famFileContent", "")
    c_filename  = data.get("cFileName", "app.c")
    firmware    = data.get("firmware", "official")
    extra_files = data.get("extraFiles", [])

    if firmware not in FIRMWARE_URLS:
        return jsonify({"success": False, "error": f"Unknown firmware: {firmware}"}), 400

    if not c_content:
        return jsonify({"success": False, "error": "Missing .c source file"}), 400

    app_name = re.sub(r"[^A-Za-z0-9_]", "_", c_filename.replace(".c", ""))

    # Auto-fix swapped .c / .fam — only when both files were actually uploaded
    if fam_content and ("App(" in c_content or "appid=" in c_content):
        c_content, fam_content = fam_content, c_content

    # Auto-generate application.fam if not provided
    if not fam_content:
        fam_content = auto_generate_fam(c_content, app_name)

    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, f"{app_name}.c"), "w") as f:
            f.write(c_content)
        with open(os.path.join(tmp, "application.fam"), "w") as f:
            f.write(fam_content)

        for ef in extra_files:
            name      = ef.get("name", "")
            content   = ef.get("content", "")
            is_binary = ef.get("isBinary", False)
            if not name or content is None:
                continue
            safe_name = os.path.normpath(name.lstrip("/").lstrip("./"))
            if safe_name.startswith(".."):
                continue
            dest_path = os.path.join(tmp, safe_name)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            if is_binary:
                with open(dest_path, "wb") as f:
                    f.write(base64.b64decode(content))
            else:
                with open(dest_path, "w") as f:
                    f.write(content)

        fap_bytes, err = do_compile(tmp, firmware)
        if err:
            return jsonify({"success": False, "error": err}), 500

    return app.response_class(
        response=fap_bytes, status=200,
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={app_name}.fap"}
    )


# ── /compile-git  (GitHub URL mode) ──────────────────────────────────────────

@app.route("/compile-git", methods=["POST"])
def compile_git():
    data     = request.json
    git_url  = data.get("gitUrl", "").strip()
    firmware = data.get("firmware", "official")

    if not git_url:
        return jsonify({"success": False, "error": "No GitHub URL provided"}), 400

    if firmware not in FIRMWARE_URLS:
        return jsonify({"success": False, "error": f"Unknown firmware: {firmware}"}), 400

    # Normalise URL — strip trailing slashes, tree/branch paths, .git suffix
    # so we always end up with a bare clone URL
    clean_url = re.sub(r'\.git$', '', git_url.rstrip("/"))
    clean_url = re.sub(r'/tree/[^/]+.*$', '', clean_url)   # strip /tree/branch/...
    clone_url = clean_url + ".git"

    if not is_safe_git_url(clean_url):
        return jsonify({"success": False, "error": (
            "Invalid URL. Please use a public GitHub repository URL, e.g.:\n"
            "  https://github.com/username/repo"
        )}), 400

    repo_name = re.sub(r"[^A-Za-z0-9_]", "_", clean_url.rstrip("/").split("/")[-1])

    with tempfile.TemporaryDirectory() as tmp:
        clone_dir = os.path.join(tmp, "repo")

        # ── 1. Clone ──────────────────────────────────────────────────────────
        try:
            clone = subprocess.run(
                ["git", "clone", "--depth=1", "--single-branch", clone_url, clone_dir],
                capture_output=True, text=True, timeout=GIT_CLONE_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            return jsonify({"success": False,
                            "error": "Git clone timed out (120s). Check the URL or try again."}), 500

        if clone.returncode != 0:
            return jsonify({"success": False, "error": (
                "Git clone failed. Make sure the repository is public.\n\n"
                f"Details:\n{(clone.stderr or clone.stdout).strip()}"
            )}), 500

        # ── 2. Find application.fam ───────────────────────────────────────────
        fam_files = glob.glob(os.path.join(clone_dir, "**", "application.fam"), recursive=True)
        if not fam_files:
            return jsonify({"success": False, "error": (
                "No application.fam found in this repository.\n"
                "This doesn't look like a Flipper Zero app — "
                "every FZ app needs an application.fam manifest."
            )}), 500

        # Pick the shallowest application.fam (the real app root, not a nested example)
        fam_files.sort(key=lambda p: p.count(os.sep))
        app_dir = os.path.dirname(fam_files[0])

        # ── 3. Build directly in the app directory ────────────────────────────
        # ufbt only compiles what application.fam declares — it ignores
        # docs/, images/, tests/, reference code, etc. automatically.
        fap_bytes, err = do_compile(app_dir, firmware)
        if err:
            return jsonify({"success": False, "error": err}), 500

    return app.response_class(
        response=fap_bytes, status=200,
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={repo_name}.fap"}
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
