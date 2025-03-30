import os
import subprocess
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

@app.route('/build', methods=['POST'])
def build_image():
    try:
        data = request.get_json()
        if not data or 'repo_url' not in data:
            return jsonify({"error": "Missing repo_url"}), 400

        repo_name = data['repo_url'].split('/')[-1].replace('.git', '')
        image_name = f"{os.getenv('DOCKERHUB_USERNAME')}/{repo_name}:latest"

        # Trigger build (in production, use a queue system)
        subprocess.run([
            "docker", "build",
            "-t", image_name,
            data['repo_url']
        ], check=True)
        subprocess.run(["docker", "push", image_name], check=True)

        return jsonify({
            "status": "success",
            "image": image_name,
            "pull_cmd": f"docker pull {image_name}"
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)