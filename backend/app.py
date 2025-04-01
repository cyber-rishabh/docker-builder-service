import os
import subprocess
import shutil
import logging
import time
import re
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
    client.ping()
    logger.info("Docker connection established")
except Exception as e:
    logger.error(f"Docker connection failed: {str(e)}")
    client = None

# Load environment variables
try:
    dotenv_path = Path(__file__).parent / '.env'
    if dotenv_path.exists():
        with open(dotenv_path, encoding='utf-8') as f:
            load_dotenv(stream=f)
        logger.info("Environment variables loaded")
except Exception as e:
    logger.warning(f"Couldn't load .env: {str(e)}")

# Enhanced project templates with priority order
PROJECT_CONFIGS = [
    {
        'type': 'nextjs',
        'detect': ['next.config.js'],
        'dockerfile': """FROM node:16
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
RUN npm run build
EXPOSE 3000
CMD ["npm", "run", "start"]""",
        'port': 3000
    },
    {
        'type': 'react',
        'detect': ['package.json', 'src/App.js'],
        'dockerfile': """FROM node:16
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
RUN npm run build
EXPOSE 3000
CMD ["npm", "start"]""",
        'port': 3000
    },
    {
        'type': 'node',
        'detect': ['package.json'],
        'dockerfile': """FROM node:16
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
EXPOSE 3000
CMD ["npm", "start"]""",
        'port': 3000
    },
    {
        'type': 'python',
        'detect': ['requirements.txt'],
        'dockerfile': """FROM python:3.9
WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY . .
EXPOSE 5000
CMD ["python", "app.py"]""",
        'port': 5000
    },
    {
        'type': 'static',
        'detect': ['index.html'],
        'dockerfile': """FROM nginx:alpine
COPY . /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]""",
        'port': 80
    }
]


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
    raise RuntimeError(f"Failed to clean build directory: {build_dir}")


def clone_repository(repo_url: str, build_dir: Path):
    """Clone repository with comprehensive error handling"""
    try:
        subprocess.run(
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
    for config in PROJECT_CONFIGS:
        if all((build_dir / file).exists() for file in config['detect']):
            return config
    return PROJECT_CONFIGS[-1]  # Default to static


@app.route('/')
def home():
    """Root endpoint showing service status"""
    return jsonify({
        "status": "ready",
        "endpoints": {
            "build": "POST /build",
            "health": "GET /health"
        },
        "docker_available": bool(client),
        "supported_project_types": [c['type'] for c in PROJECT_CONFIGS]
    })


@app.route('/health')
def health_check():
    """Health check endpoint"""
    try:
        disk_usage = shutil.disk_usage('/')
        status = {
            "status": "healthy",
            "disk_space": f"{disk_usage.free / (1024 ** 3):.1f}GB free",
            "docker": "connected" if client else "disconnected",
            "timestamp": time.time()
        }
        return jsonify(status)
    except Exception as e:
        logger.error(f"Health check failed: {str(e)}")
        return jsonify({"status": "unhealthy", "error": str(e)}), 500


@app.route('/build', methods=['POST'])
def build_image():
    """Main build endpoint"""
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

        repo_name = re.sub(r'[^a-z0-9-]', '-', Path(parsed.path).stem.lower())
        build_dir = Path(f"/tmp/builds/{repo_name}")

        try:
            clean_build_dir(build_dir)
            clone_repository(repo_url, build_dir)

            config = detect_project_type(build_dir)

            with open(build_dir / 'Dockerfile', 'w') as f:
                f.write(config['dockerfile'])

            image, build_logs = client.images.build(
                path=str(build_dir),
                tag=f"builder/{repo_name}:latest",
                rm=True
            )

            return jsonify({
                "status": "success",
                "image": image.tags[0],
                "type": config['type'],
                "port": config['port'],
                "logs": [line.get('stream', '').strip()
                         for line in build_logs
                         if 'stream' in line and line['stream'].strip()][-20:],
                "run_command": f"docker run -p 8080:{config['port']} {image.tags[0]}"
            })

        except Exception as e:
            logger.error(f"Build process failed: {str(e)}")
            return jsonify({"error": str(e)}), 500

        finally:
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