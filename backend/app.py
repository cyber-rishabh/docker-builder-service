import os
import subprocess
import shutil
import logging
import time
from pathlib import Path
from urllib.parse import urlparse
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import docker

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Initialize Docker client with error handling
try:
    client = docker.from_env()
    client.ping()  # Test connection
    logger.info("Docker connection established")
except Exception as e:
    logger.error(f"Docker connection failed: {str(e)}")
    client = None
    # Consider sys.exit(1) if Docker is mandatory

# Load environment variables
try:
    dotenv_path = Path(__file__).parent / '.env'
    if dotenv_path.exists():
        with open(dotenv_path, encoding='utf-8') as f:
            load_dotenv(stream=f)
        logger.info("Environment variables loaded")
except Exception as e:
    logger.warning(f"Couldn't load .env: {str(e)}")

# Dockerfile templates
DOCKERFILE_TEMPLATES = {
    'static': """FROM nginx:alpine
COPY . /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]""",

    'node': """FROM node:16-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
CMD ["npm", "start"]""",

    'default': """FROM alpine
COPY . /app
WORKDIR /app
CMD ["ls", "-la"]"""
}


def clean_build_dir(build_dir: Path):
    """Robust directory cleaning with retries"""
    for attempt in range(3):
        try:
            if build_dir.exists():
                shutil.rmtree(build_dir, ignore_errors=True)
            build_dir.mkdir(parents=True, exist_ok=True)
            return
        except Exception as e:
            logger.warning(f"Clean failed (attempt {attempt + 1}): {str(e)}")
            time.sleep(1)
    raise RuntimeError(f"Failed to clean build directory after 3 attempts: {build_dir}")


def clone_repository(repo_url: str, build_dir: Path):
    """Clone repository with comprehensive error handling"""
    try:
        result = subprocess.run(
            ['git', 'clone', '--depth', '1', repo_url, str(build_dir)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        logger.info(f"Cloned repository: {repo_url}")
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.strip()
        if 'Repository not found' in error_msg:
            raise ValueError("Repository not found or private (needs access token)")
        if 'could not read Username' in error_msg:
            raise ValueError("Authentication failed - use HTTPS with token")
        raise RuntimeError(f"Git clone failed: {error_msg}")


def detect_project_type(build_dir: Path):
    """Detect project type based on files present"""
    if (build_dir / 'package.json').exists():
        return 'node'
    elif (build_dir / 'index.html').exists():
        return 'static'
    return 'default'


def build_docker_image(build_dir: Path, repo_name: str):
    """Build Docker image with comprehensive logging"""
    if not client:
        raise RuntimeError("Docker service unavailable")

    try:
        # Auto-generate Dockerfile if needed
        if not (build_dir / 'Dockerfile').exists():
            project_type = detect_project_type(build_dir)
            with open(build_dir / 'Dockerfile', 'w') as f:
                f.write(DOCKERFILE_TEMPLATES[project_type])
            logger.info(f"Generated {project_type} Dockerfile")

        image, build_logs = client.images.build(
            path=str(build_dir),
            tag=f"builder/{repo_name.replace('.', '-')}:latest",
            rm=True,
            forcerm=True
        )

        # Filter and limit logs
        logs = [
                   line.get('stream', '').strip()
                   for line in build_logs
                   if 'stream' in line and line['stream'].strip()
               ][-20:]  # Last 20 lines only

        logger.info(f"Successfully built image: {image.tags[0]}")
        return image, logs

    except docker.errors.BuildError as e:
        logger.error(f"Build failed: {str(e)}")
        raise RuntimeError(f"Docker build failed: {e.msg}")
    except docker.errors.APIError as e:
        logger.error(f"Docker API error: {str(e)}")
        raise RuntimeError("Docker service error")


@app.route('/')
def home():
    return jsonify({
        "status": "ready",
        "endpoints": {
            "build": "POST /build",
            "health": "GET /health"
        },
        "docker_available": bool(client)
    })


@app.route('/health')
def health_check():
    try:
        disk_usage = shutil.disk_usage('/')
        status = {
            "status": "healthy",
            "disk_space": f"{disk_usage.free / (1024 ** 3):.1f}GB free",
            "docker": "connected" if client else "disconnected"
        }
        return jsonify(status)
    except Exception as e:
        logger.error(f"Health check failed: {str(e)}")
        return jsonify({"status": "unhealthy", "error": str(e)}), 500


@app.route('/build', methods=['POST'])
def build_image():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400

    data = request.get_json()
    repo_url = data.get('repo_url', '').strip()

    if not repo_url:
        return jsonify({"error": "Missing repo_url"}), 400

    try:
        # Validate URL
        parsed = urlparse(repo_url)
        if not all([parsed.scheme, parsed.netloc]):
            return jsonify({"error": "Invalid repository URL"}), 400

        repo_name = Path(parsed.path).stem or "unnamed"
        build_dir = Path(f"/tmp/builds/{repo_name}")

        try:
            clean_build_dir(build_dir)
            clone_repository(repo_url, build_dir)

            # Special handling for GitHub Pages
            if 'github.io' in repo_url.lower():
                if not (build_dir / 'Dockerfile').exists():
                    with open(build_dir / 'Dockerfile', 'w') as f:
                        f.write(DOCKERFILE_TEMPLATES['static'])
                    logger.info("Auto-generated static site Dockerfile")

            image, logs = build_docker_image(build_dir, repo_name)

            return jsonify({
                "status": "success",
                "image": image.tags[0],
                "logs": logs,
                "run_command": f"docker run -p 8080:80 {image.tags[0]}"
            })

        except Exception as e:
            logger.error(f"Build process failed: {str(e)}")
            return jsonify({"error": str(e)}), 500

        finally:
            if build_dir.exists():
                shutil.rmtree(build_dir, ignore_errors=True)

    except Exception as e:
        logger.exception("Unexpected error in build endpoint")
        return jsonify({"error": "Internal server error"}), 500


if __name__ == '__main__':
    app.run(
        host='0.0.0.0',
        port=5000,
        debug=os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    )