
import json
import os
import base64
from typing import Annotated

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

mcp = FastMCP("github-repo-agent")

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")  # optional, for higher rate limits

def _normalize_repo(repo: str) -> str:
    """Accept full GitHub URL or owner/repo format."""
    repo = repo.strip().rstrip("/").removesuffix(".git")
    if "github.com/" in repo:
        repo = repo.split("github.com/")[-1]
    return repo


def _gh(path: str, params: dict = None) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    try:
        resp = httpx.get(f"{GITHUB_API}{path}", headers=headers, params=params or {}, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
    except httpx.HTTPError as e:
        return {"error": str(e)}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
def get_repo_info(
    repo: Annotated[str, Field(description="GitHub repo in owner/repo format or full GitHub URL.")],
) -> str:
    """Get basic information about a GitHub repository."""
    repo = _normalize_repo(repo)
    data = _gh(f"/repos/{repo}")
    if "error" in data:
        return json.dumps(data)
    return json.dumps({
        "name": data.get("full_name"),
        "description": data.get("description"),
        "default_branch": data.get("default_branch"),
        "language": data.get("language"),
        "topics": data.get("topics", []),
        "last_push": data.get("pushed_at"),
        "open_issues": data.get("open_issues_count"),
    }, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
def list_repo_files(
    repo: Annotated[str, Field(description="GitHub repo in owner/repo format.")],
    path: Annotated[str, Field(description="Directory path to list (e.g. 'src', 'backend', ''  for root).")] = "",
    ref: Annotated[str, Field(description="Branch, tag, or commit SHA. Defaults to default branch.")] = "",
) -> str:
    """List files and directories in a GitHub repository path.

    Use this to explore the repo structure before reading specific files.
    Returns file names, types (file/dir), and sizes.
    
    SECURITY: Filters out sensitive files from listings to prevent accidental access.
    """
    # Security: Hide sensitive files from directory listings
    HIDDEN_FILES = {
        '.env', '.env.local', '.env.production', '.env.development', '.env.test',
        'credentials.json', 'credentials.yml', 'credentials.yaml',
        'secrets.json', 'secrets.yml', 'secrets.yaml',
        'private.key', 'private.pem', 'auth.json',
        '.npmrc', '.pypirc',
    }
    
    params = {}
    if ref:
        params["ref"] = ref
    data = _gh(f"/repos/{repo}/contents/{path}", params)
    if "error" in data:
        return json.dumps(data)
    if isinstance(data, list):
        # Filter out sensitive files
        filtered = []
        for f in data:
            file_name = f["name"].lower()
            # Skip if exact match or ends with sensitive extension
            if file_name in HIDDEN_FILES or file_name.endswith(('.key', '.pem')):
                continue
            filtered.append({
                "name": f["name"],
                "type": f["type"],
                "path": f["path"],
                "size": f.get("size", 0),
            })
        return json.dumps(filtered, indent=2)
    return json.dumps(data, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
def read_file(
    repo: Annotated[str, Field(description="GitHub repo in owner/repo format.")],
    path: Annotated[str, Field(description="File path in the repo (e.g. 'backend/server.js', 'Dockerfile').")],
    ref: Annotated[str, Field(description="Branch, tag, or commit SHA. Defaults to default branch.")] = "",
) -> str:
    """Read the contents of a file from a GitHub repository.

    Use this to read source code, Dockerfiles, docker-compose files, config files.
    For RCA: read the file at the commit that was deployed when the incident started.
    Returns decoded file content as plain text.
    
    SECURITY: Blocks reading of sensitive files containing secrets (.env, credentials, keys, etc.)
    """
    # Security: Block reading sensitive files that may contain secrets
    BLOCKED_FILES = {
        '.env', '.env.local', '.env.production', '.env.development', '.env.test',
        'credentials.json', 'credentials.yml', 'credentials.yaml',
        'secrets.json', 'secrets.yml', 'secrets.yaml',
        '.aws/credentials', '.ssh/id_rsa', '.ssh/id_ed25519',
        'private.key', 'private.pem', '*.key', '*.pem',
        '.npmrc', '.pypirc', 'auth.json',
    }
    
    # Check if the file path matches any blocked pattern
    file_name = path.split('/')[-1].lower()
    path_lower = path.lower()
    
    for blocked in BLOCKED_FILES:
        if blocked.startswith('*'):
            # Pattern match for extensions like *.key
            if file_name.endswith(blocked[1:]):
                return json.dumps({
                    "error": f"Access denied: Cannot read sensitive file '{path}'. This file may contain secrets or credentials.",
                    "blocked_pattern": blocked,
                    "security_note": "If you need configuration values, ask the user or check environment variables in the running container using inspect_container."
                })
        elif file_name == blocked or path_lower.endswith(blocked):
            return json.dumps({
                "error": f"Access denied: Cannot read sensitive file '{path}'. This file may contain secrets or credentials.",
                "blocked_file": blocked,
                "security_note": "If you need configuration values, ask the user or check environment variables in the running container using inspect_container."
            })
    
    params = {}
    if ref:
        params["ref"] = ref
    data = _gh(f"/repos/{repo}/contents/{path}", params)
    if "error" in data:
        return json.dumps(data)
    if data.get("encoding") == "base64":
        try:
            content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            return json.dumps({
                "path": path,
                "ref": ref or "default branch",
                "size": data.get("size"),
                "content": content,
            }, indent=2)
        except Exception as e:
            return json.dumps({"error": f"Failed to decode: {e}"})
    return json.dumps(data, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
def get_recent_commits(
    repo: Annotated[str, Field(description="GitHub repo in owner/repo format.")],
    branch: Annotated[str, Field(description="Branch name. Defaults to default branch.")] = "",
    count: Annotated[int, Field(description="Number of recent commits to return.", ge=1, le=20)] = 10,
    path: Annotated[str, Field(description="Filter commits that touched a specific file path. Leave empty for all.")] = "",
) -> str:
    """Get recent commits from a GitHub repository.

    Returns commit SHA, author, timestamp, and message for each commit.
    Use this to find what changed just before an incident started.
    Cross-reference commit timestamps with incident start time from docker events.
    """
    params = {"per_page": count}
    if branch:
        params["sha"] = branch
    if path:
        params["path"] = path
    data = _gh(f"/repos/{repo}/commits", params)
    if "error" in data:
        return json.dumps(data)
    if not isinstance(data, list):
        return json.dumps(data, indent=2)
    return json.dumps([{
        "sha": c["sha"][:12],
        "full_sha": c["sha"],
        "author": c["commit"]["author"]["name"],
        "email": c["commit"]["author"]["email"],
        "timestamp": c["commit"]["author"]["date"],
        "message": c["commit"]["message"].split("\n")[0],
    } for c in data], indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
def get_commit_diff(
    repo: Annotated[str, Field(description="GitHub repo in owner/repo format.")],
    sha: Annotated[str, Field(description="Full or short commit SHA to get the diff for.")],
) -> str:
    """Get the diff (changed files and lines) for a specific commit.

    Returns list of changed files with additions, deletions, and the patch diff.
    Use this when you've identified a suspicious commit from get_recent_commits
    and want to see exactly what code changed.
    Truncates large diffs to 300 lines per file.
    """
    data = _gh(f"/repos/{repo}/commits/{sha}")
    if "error" in data:
        return json.dumps(data)
    files = []
    for f in data.get("files", []):
        patch = f.get("patch", "")
        if len(patch.splitlines()) > 300:
            patch = "\n".join(patch.splitlines()[:300]) + "\n... (truncated)"
        files.append({
            "filename": f["filename"],
            "status": f["status"],
            "additions": f["additions"],
            "deletions": f["deletions"],
            "patch": patch,
        })
    return json.dumps({
        "sha": data["sha"][:12],
        "author": data["commit"]["author"]["name"],
        "timestamp": data["commit"]["author"]["date"],
        "message": data["commit"]["message"],
        "files_changed": len(files),
        "files": files,
    }, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
def search_code(
    repo: Annotated[str, Field(description="GitHub repo in owner/repo format.")],
    query: Annotated[str, Field(description="Search query (e.g. 'APP_SECRET', 'blockingHeavyComputation', 'MONGO_URI').")],
) -> str:
    """Search for a string or symbol across all files in a GitHub repository.

    Use this to find where a specific env var is used, where a function is defined,
    or where an error message originates. Helps trace a log error back to the source code.
    Returns file paths and line snippets containing the match.
    """
    data = _gh("/search/code", {"q": f"{query} repo:{repo}", "per_page": 10})
    if "error" in data:
        return json.dumps(data)
    items = data.get("items", [])
    return json.dumps({
        "query": query,
        "total_matches": data.get("total_count", 0),
        "results": [{
            "file": item["path"],
            "url": item["html_url"],
            "sha": item["sha"][:12],
        } for item in items],
    }, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
def get_file_commits(
    repo: Annotated[str, Field(description="GitHub repo in owner/repo format.")],
    path: Annotated[str, Field(description="File path to get commit history for (e.g. 'backend/server.js', 'docker-compose.yml').")],
    count: Annotated[int, Field(description="Number of recent commits to return.", ge=1, le=10)] = 5,
) -> str:
    """Get the commit history for a specific file in a GitHub repository.

    Use this to see who last changed a specific file and when.
    Especially useful for Dockerfile, docker-compose.yml, and config files —
    if the incident started after a config file was changed, this shows it.
    """
    params = {"path": path, "per_page": count}
    data = _gh(f"/repos/{repo}/commits", params)
    if "error" in data:
        return json.dumps(data)
    if not isinstance(data, list):
        return json.dumps(data, indent=2)
    return json.dumps({
        "file": path,
        "recent_changes": [{
            "sha": c["sha"][:12],
            "author": c["commit"]["author"]["name"],
            "timestamp": c["commit"]["author"]["date"],
            "message": c["commit"]["message"].split("\n")[0],
        } for c in data],
    }, indent=2)


if __name__ == "__main__":
    mcp.run()
