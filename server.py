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

# Belt-and-suspenders: manually set CORS headers on every response
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

# Timeout constants (seconds)
UFBT_UPDATE_TIMEOUT = 120   # SDK download can be slow
UFBT_BUILD_TIMEOUT  = 180   # Compilation timeout
GIT_CLONE_TIMEOUT   = 60    # Git clone timeout

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

def is_safe_git_url(url: str) -> bool:
    """Only allow well-formed public GitHub HTTPS URLs."""
    return bool(re.match(r'^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(\.git)?$', url))

def do_compile(tmp, firmware, app_name):
    sdk_url = FIRMWARE_URLS.get(firmware, FIRMWARE_URLS["official"])

    # Step 1: Update/download the SDK
    try:
        update = subprocess.run(
            ["python3", "-m", "ufbt", "update", f"--index-url={sdk_url}"],
            cwd=tmp, capture_output=True, text=True,
            timeout=UFBT_UPDATE_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return None, "SDK update timed out (120s). The firmware server may be slow — please retry."

    if update.returncode != 0:
        return None, f"SDK update failed:\n{update.stderr or update.stdout}"

    # Step 2: Build the app
    try:
        build = subprocess.run(
            ["python3", "-m", "ufbt"],
            cwd=tmp, capture_output=True, text=True,
            timeout=UFBT_BUILD_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return None, "Compilation timed out (180s). Try a simpler project or retry."

    if build.returncode != 0:
        # Return both stderr and stdout for better diagnostics
        error_output = (build.stderr or "") + "\n" + (build.stdout or "")
        return None, f"Compile error:\n{error_output.strip()}"

    # Step 3: Find the output .fap
    fap_files = (
        glob.glob(os.path.join(tmp, "dist", "*.fap")) +
        glob.glob(os.path.join(tmp, ".ufbt", "build", "*.fap")) +
        glob.glob(os.path.join(tmp, "build", "*.fap"))
    )

    if not fap_files:
        return None, (
            "Build succeeded but no .fap file was found.\n"
            f"Build output:\n{build.stdout}\n{build.stderr}"
        )

    with open(fap_files[0], "rb") as f:
        return f.read(), None


@app.route("/compile", methods=["POST"])
def compile_files():
    data = request.json
    c_content   = data.get("cFileContent", "")
    fam_content = data.get("famFileContent", "")
    c_filename  = data.get("cFileName", "app.c")
    firmware    = data.get("firmware", "official")
    extra_files = data.get("extraFiles", [])

    # Validate firmware choice
    if firmware not in FIRMWARE_URLS:
        return jsonify({"success": False, "error": f"Unknown firmware: {firmware}"}), 400

    # Auto-fix swapped files (same logic as before)
    if "App(" in c_content or "appid=" in c_content:
        c_content, fam_content = fam_content, c_content

    if not c_content or not fam_content:
        return jsonify({"success": False, "error": "Missing source files"}), 400

    app_name = re.sub(r"[^A-Za-z0-9_]", "_", c_filename.replace(".c", ""))

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
            # Sanitize path to prevent directory traversal
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

    # Validate firmware choice
    if firmware not in FIRMWARE_URLS:
        return jsonify({"success": False, "error": f"Unknown firmware: {firmware}"}), 400

    # Strict URL validation — only allow safe public GitHub HTTPS URLs
    if not is_safe_git_url(git_url):
        return jsonify({
            "success": False,
            "error": (
                "Invalid URL. Please use a full public GitHub HTTPS URL like:\n"
                "  https://github.com/username/repo\n"
                "  https://github.com/username/repo.git"
            )
        }), 400

    repo_name = re.sub(r"[^A-Za-z0-9_]", "_", git_url.rstrip("/").split("/")[-1].replace(".git", ""))

    with tempfile.TemporaryDirectory() as tmp:
        # Clone the repo (shallow, single branch for speed)
        try:
            clone = subprocess.run(
                ["git", "clone", "--depth=1", "--single-branch", git_url, "repo"],
                cwd=tmp, capture_output=True, text=True,
                timeout=GIT_CLONE_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            return jsonify({"success": False, "error": "Git clone timed out (60s). Check the URL or try again."}), 500

        if clone.returncode != 0:
            return jsonify({
                "success": False,
                "error": (
                    f"Git clone failed. Make sure the repository is public and the URL is correct.\n\n"
                    f"Details:\n{clone.stderr or clone.stdout}"
                )
            }), 500

        repo_dir = os.path.join(tmp, "repo")

        # Find application.fam to locate the app root directory
        fam_files = glob.glob(os.path.join(repo_dir, "**", "application.fam"), recursive=True)
        if not fam_files:
            return jsonify({
                "success": False,
                "error": (
                    "No application.fam found in the repository.\n"
                    "Make sure this is a Flipper Zero app repo with an application.fam manifest."
                )
            }), 500

        # Use the directory containing application.fam as the build root
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
