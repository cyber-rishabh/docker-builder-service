param (
    [Parameter(Mandatory=$true)]
    [string]$RepoUrl,
    [int]$HostPort = 8080
)

function Generate-Dockerfile {
    param ([string]$RepoPath)

    $dockerfilePath = Join-Path $RepoPath "Dockerfile"

    # Dynamic Site Detection
    # Case 1: Next.js/TypeScript
    if ((Test-Path (Join-Path $RepoPath "next.config.js")) -or
        (Test-Path (Join-Path $RepoPath "tsconfig.json"))) {
        @"
FROM node:18-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
RUN npm run build
EXPOSE 80
CMD ["npm", "start"]
"@ | Out-File $dockerfilePath -Encoding utf8
        Write-Host "Generated Next.js/TypeScript Dockerfile"
    }
    # Case 2: Node.js
    elseif (Test-Path (Join-Path $RepoPath "package.json")) {
        $packageJson = Get-Content (Join-Path $RepoPath "package.json") | ConvertFrom-Json
        if ($packageJson.scripts.server -or $packageJson.scripts.start) {
            @"
FROM node:18-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
EXPOSE 80
CMD ["npm", "run", "server"]
"@ | Out-File $dockerfilePath -Encoding utf8
            Write-Host "Generated Node.js Dockerfile"
        }
    }
    # Static Site Detection
    # Case 3: HTML/CSS/JS
    elseif ((Get-ChildItem $RepoPath -Recurse -File |
            Where-Object { $_.Name -match '\.(html|css|js)$' }).Count -gt 3) {
        @"
FROM nginx:alpine
COPY . /usr/share/nginx/html
EXPOSE 80
"@ | Out-File $dockerfilePath -Encoding utf8
        Write-Host "Generated Static Site Dockerfile"
    }
    else {
        throw "Could not detect project type"
    }
}

# Main Execution
try {
    # Validate and prepare
    if (-not $RepoUrl.StartsWith("http")) {
        throw "Invalid repository URL"
    }

    $repoName = ($RepoUrl -split '/')[-1] -replace '\.git$','' -replace '[^a-z0-9]','-'
    $repoName = $repoName.ToLower()
    $clonePath = Join-Path $pwd $repoName

    # Clone fresh
    Write-Host "Cloning $RepoUrl..."
    Remove-Item $clonePath -Recurse -Force -ErrorAction SilentlyContinue
    git clone $RepoUrl $clonePath 2>&1 | Out-Null

    if (-not (Test-Path $clonePath)) {
        throw "Clone failed"
    }

    # Generate and build
    Generate-Dockerfile -RepoPath $clonePath

    Write-Host "Building Docker image..."
    docker build -t "$repoName-image" $clonePath

    Write-Host "Running container on port $HostPort..."
    docker run -d -p "${HostPort}:80" "$repoName-image"

    Write-Host "Success! Access at: http://localhost:$HostPort"
    Write-Host "To stop: docker stop $(docker ps -lq)"
}
catch {
    Write-Host "Error: $_" -ForegroundColor Red
    exit 1
}