from flask import Flask, request, jsonify
from flask_cors import CORS
import subprocess
import tempfile
import os
import glob
import base64
import threading

app = Flask(__name__)
CORS(app)

FIRMWARE_URLS = {
    "official":    "https://update.flipperzero.one/firmware/directory.json",
    "unleashed":   "https://up.unleashedflip.com/directory.json",
    "roguemaster": "https://up.roguemaster.net/directory.json",
    "momentum":    "https://up.momentum-fw.dev/firmware/directory.json",
}

# Cache which SDKs are already downloaded this server session
_sdk_ready = set()
_sdk_lock  = threading.Lock()

def ensure_sdk(firmware):
    """Download SDK only once per firmware per server lifetime."""
    with _sdk_lock:
        if firmware in _sdk_ready:
            return None
        sdk_url = FIRMWARE_URLS.get(firmware, FIRMWARE_URLS["official"])
        result = subprocess.run(
            ["python3", "-m", "ufbt", "update", f"--index-url={sdk_url}"],
            capture_output=True, text=True, timeout=180
        )
        if result.returncode != 0:
            return f"SDK update failed:\n{result.stderr}"
        _sdk_ready.add(firmware)
        return None

def prewarm():
    """Pre-download Unleashed SDK on startup so first compile is instant."""
    print("Pre-warming Unleashed SDK...", flush=True)
    err = ensure_sdk("unleashed")
    if err:
        print(f"Prewarm failed: {err}", flush=True)
    else:
        print("Unleashed SDK ready ✓", flush=True)

threading.Thread(target=prewarm, daemon=True).start()

@app.route("/keep-alive", methods=["GET", "HEAD"])
def keep_alive():
    return jsonify({"status": "ok"})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "sdks_ready": list(_sdk_ready)})

def do_compile(tmp, firmware, app_name):
    err = ensure_sdk(firmware)
    if err:
        return None, err

    build = subprocess.run(
        ["python3", "-m", "ufbt"],
        cwd=tmp, capture_output=True, text=True, timeout=240
    )
    if build.returncode != 0:
        return None, f"Compile error:\n{build.stderr}"

    fap_files = (
        glob.glob(os.path.join(tmp, "dist", "*.fap")) +
        glob.glob(os.path.join(tmp, ".ufbt", "build", "*.fap")) +
        glob.glob(os.path.join(tmp, "build", "*.fap"))
    )

    if not fap_files:
        return None, "Build succeeded but no .fap found"

    with open(fap_files[0], "rb") as f:
        return f.read(), None

@app.route("/compile", methods=["POST"])
def compile():
    data = request.json
    c_content    = data.get("cFileContent", "")
    fam_content  = data.get("famFileContent", "")
    c_filename   = data.get("cFileName", "app.c")
    firmware     = data.get("firmware", "official")
    extra_files  = data.get("extraFiles", [])

    # Auto-fix swapped files
    if "App(" in c_content or "appid=" in c_content:
        c_content, fam_content = fam_content, c_content

    if not c_content or not fam_content:
        return jsonify({"success": False, "error": "Missing source files"}), 400

    app_name = c_filename.replace(".c", "").replace("-", "_")

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
            safe_name = name.lstrip("/").lstrip("./")
            dest_path = os.path.join(tmp, safe_name)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            if is_binary:
                with open(dest_path, "wb") as f:
                    f.write(base64.b64decode(content))
            else:
                with open(dest_path, "w") as f:
                    f.write(content)

        fap_bytes, err = do_compile(tmp, firmware, app_name)
        if err:
            return jsonify({"success": False, "error": err}), 500

    return app.response_class(
        response=fap_bytes,
        status=200,
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={app_name}.fap"}
    )

@app.route("/compile-git", methods=["POST"])
def compile_git():
    data     = request.json
    git_url  = data.get("gitUrl", "").strip()
    firmware = data.get("firmware", "official")

    if not git_url:
        return jsonify({"success": False, "error": "No GitHub URL provided"}), 400

    if "github.com" not in git_url:
        return jsonify({"success": False, "error": "Only GitHub URLs are supported"}), 400

    repo_name = git_url.rstrip("/").split("/")[-1].replace(".git", "")

    with tempfile.TemporaryDirectory() as tmp:
        clone = subprocess.run(
            ["git", "clone", "--depth=1", git_url, "repo"],
            cwd=tmp, capture_output=True, text=True, timeout=60
        )
        if clone.returncode != 0:
            return jsonify({"success": False, "error": f"Git clone failed:\n{clone.stderr}"}), 500

        repo_dir = os.path.join(tmp, "repo")
        fam_files = glob.glob(os.path.join(repo_dir, "**", "application.fam"), recursive=True)
        if not fam_files:
            return jsonify({"success": False, "error": "No application.fam found in repo"}), 500

        app_dir = os.path.dirname(fam_files[0])
        fap_bytes, err = do_compile(app_dir, firmware, repo_name)
        if err:
            return jsonify({"success": False, "error": err}), 500

    return app.response_class(
        response=fap_bytes,
        status=200,
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={repo_name}.fap"}
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
