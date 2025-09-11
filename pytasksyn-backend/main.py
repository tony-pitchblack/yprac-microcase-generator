#!/usr/bin/env python3
import argparse
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
import os
import sys
import csv
import tempfile
from datetime import datetime
from dotenv import load_dotenv
from pathlib import Path
import httpx
import re
from typing import Optional
import asyncio
import json
import uuid

# Load .env from root folder
root_dir = Path(__file__).parent.parent
env_path = root_dir / ".env"
load_dotenv(env_path)

# Add root directory to path for pytasksyn imports
sys.path.insert(0, str(root_dir))

# Import pytasksyn modules
from pytasksyn.main import load_config, run_pipeline
from pytasksyn.utils.logging_utils import init_logger, get_logger

app = FastAPI()

# Simple in-memory session storage for SSE
SESSIONS: dict[str, asyncio.Queue] = {}
DEV_MODE: bool = False

def sse_format(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")

class GenerateMicrocaseRequest(BaseModel):
    url: str
    user_id: str

def parse_github_pr_url(url: str) -> Optional[tuple[str, str, str]]:
    """Parse GitHub PR URL to extract owner, repo, and PR number."""
    pattern = r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)"
    match = re.match(pattern, url)
    if match:
        return match.groups()
    return None

async def fetch_pr_comments(owner: str, repo: str, pr_number: str) -> list:
    """Fetch all comments from a GitHub PR."""
    github_token = os.getenv("GITHUB_TOKEN")
    headers = {}
    if github_token:
        headers["Authorization"] = f"token {github_token}"
    
    comments = []
    
    async with httpx.AsyncClient() as client:
        # Fetch PR review comments
        review_comments_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/comments"
        response = await client.get(review_comments_url, headers=headers)
        if response.status_code == 200:
            comments.extend(response.json())
        
        # Fetch issue comments (general PR comments)
        issue_comments_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"
        response = await client.get(issue_comments_url, headers=headers)
        if response.status_code == 200:
            comments.extend(response.json())
    
    return comments

async def fetch_pr_details(owner: str, repo: str, pr_number: str) -> dict:
    """Fetch PR details to obtain head repo info and SHA (supports forks)."""
    github_token = os.getenv("GITHUB_TOKEN")
    headers = {}
    if github_token:
        headers["Authorization"] = f"token {github_token}"

    pr_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    async with httpx.AsyncClient() as client:
        response = await client.get(pr_url, headers=headers)
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Failed to fetch PR details: {response.status_code}")
        data = response.json()
        head = data.get("head", {})
        head_repo = head.get("repo") or {}
        return {
            "head_owner": (head_repo.get("owner") or {}).get("login", owner),
            "head_repo": head_repo.get("name", repo),
            "head_sha": head.get("sha"),
        }

async def fetch_github_file_content(owner: str, repo: str, file_path: str, ref: str = "HEAD") -> str:
    """Fetch file content from GitHub repository"""
    github_token = os.getenv("GITHUB_TOKEN")
    headers = {}
    if github_token:
        headers["Authorization"] = f"token {github_token}"
    
    # Use Contents API to fetch file
    contents_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}"
    params = {"ref": ref}
    
    async with httpx.AsyncClient() as client:
        response = await client.get(contents_url, headers=headers, params=params)
        if response.status_code == 200:
            file_data = response.json()
            if file_data.get('encoding') == 'base64':
                import base64
                content = base64.b64decode(file_data['content']).decode('utf-8')
                return content
        
        # If Contents API fails, try Raw API
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{file_path}"
        response = await client.get(raw_url, headers=headers)
        if response.status_code == 200:
            return response.text
    
    raise Exception(f"Could not fetch file {file_path} from {owner}/{repo}")

async def create_project_from_github(owner: str, repo: str, review_comments: list, project_dir: Path, ref: str = "HEAD"):
    """Create project structure by fetching real files from GitHub"""
    # Get unique file paths from comments
    file_paths = list(set(comment['path'] for comment in review_comments))
    
    logger = get_logger()
    logger.info(f"Fetching {len(file_paths)} files from GitHub repo {owner}/{repo}")
    
    for file_path in file_paths:
        try:
            logger.info(f"Fetching file: {file_path}")
            content = await fetch_github_file_content(owner, repo, file_path, ref)
            
            # Create local file
            local_file = project_dir / file_path
            local_file.parent.mkdir(parents=True, exist_ok=True)
            local_file.write_text(content, encoding='utf-8')
            
            logger.info(f"Successfully saved: {file_path}")
            
        except Exception as e:
            logger.warning(f"Could not fetch {file_path}: {e}")
            # Create a minimal placeholder file for missing files
            local_file = project_dir / file_path
            local_file.parent.mkdir(parents=True, exist_ok=True)
            local_file.write_text(f"# Could not fetch original file: {e}\n# File: {file_path}\n", encoding='utf-8')

async def create_review_csv_from_comments(comments: list, temp_dir: Path) -> Path:
    """Create a CSV file from PR comments in the expected format for pytasksyn"""
    csv_path = temp_dir / "code_review.csv"
    
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        # Write header matching expected format
        writer.writerow(['comment_id', 'file_path', 'line_number', 'comment', 'author'])
        
        comment_id = 1
        for comment in comments:
            # Prefer original line fields; fallback to current line or range starts
            if comment.get('path'):
                line_number = (
                    comment.get('original_line')
                    or comment.get('line')
                    or comment.get('original_start_line')
                    or comment.get('start_line')
                )
                if line_number is not None:
                    writer.writerow([
                        comment_id,
                        comment['path'],
                        int(line_number),
                        comment.get('body', ''),
                        comment.get('user', {}).get('login', 'Unknown')
                    ])
                    comment_id += 1
    
    return csv_path

@app.post("/gen-microcases/", status_code=202)
async def generate_microcases(request: GenerateMicrocaseRequest):
    # Initialize logger for console output
    init_logger(console_output=True)
    logger = get_logger()
    logger.info(f"Received request - URL: {request.url}, User ID: {request.user_id}")
    
    # Parse GitHub PR URL
    pr_info = parse_github_pr_url(request.url)
    if not pr_info:
        raise HTTPException(status_code=400, detail="Invalid GitHub PR URL format")
    
    owner, repo, pr_number = pr_info
    logger.info(f"Parsed PR info - Owner: {owner}, Repo: {repo}, PR: {pr_number}")
    
    try:
        # Fetch PR details for head repo/sha (supports forks)
        pr_details = await fetch_pr_details(owner, repo, pr_number)
        head_owner = pr_details["head_owner"]
        head_repo = pr_details["head_repo"]
        head_sha = pr_details["head_sha"]

        # Fetch all comments from the PR
        comments = await fetch_pr_comments(owner, repo, pr_number)
        
        logger.info(f"Found {len(comments)} comments in PR #{pr_number}")
        
        # Filter review comments with usable line information
        review_comments = [
            c for c in comments
            if c.get('path') and (
                c.get('original_line') is not None
                or c.get('line') is not None
                or c.get('original_start_line') is not None
                or c.get('start_line') is not None
            )
        ]
        logger.info(f"Found {len(review_comments)} review comments with file paths")
        
        # Dev mode: keep at most 1 comment to speed up iteration
        if DEV_MODE and len(review_comments) > 1:
            review_comments = review_comments[:1]
            logger.info("DEV mode enabled: limiting review comments to 1")
        
        if not review_comments:
            logger.warning("No review comments found with file paths - cannot generate microcases")
            return JSONResponse({
                "message": "No review comments with file paths found",
                "url": request.url,
                "user_id": request.user_id,
                "pr_info": {"owner": owner, "repo": repo, "pr_number": pr_number},
                "total_comments": len(comments),
                "review_comments": 0
            }, status_code=202)
        
        # Setup session directory first
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_dir = Path("tmp") / "pytasksyn-backend" / f"session_{timestamp}"
        session_dir.mkdir(parents=True, exist_ok=True)
        
        # Create real project structure by fetching files from GitHub (at PR head SHA)
        project_dir = session_dir / "source_project"
        project_dir.mkdir()
        
        # Fetch real files from the PR head repository and commit
        await create_project_from_github(head_owner, head_repo, review_comments, project_dir, ref=head_sha or "HEAD")
        
        # Create temporary directory for CSV file
        temp_dir_obj = tempfile.TemporaryDirectory()
        temp_dir = Path(temp_dir_obj.name)
        
        # Create CSV from PR comments
        review_csv = await create_review_csv_from_comments(review_comments, temp_dir)
        
        # Minimal config
        config = {
            'paths': {
                'student_project': str(project_dir),
                'code_review_file': str(review_csv)
            },
            'stages': {
                'enable_tutor': False,
                'enable_student': False
            },
            'models': {
                'preprocessor': {'provider': 'yandex', 'model_name': 'yandexgpt-lite'},
                'expert': {'provider': 'yandex', 'model_name': 'yandexgpt'}
            },
            'expert': {
                'max_attempts': 2,
                'context_max_symbols': 5000,
                'context_comment_margin': 50,
                'context_add_rest': False
            },
            'output': {
                'session_prefix': 'session',
                'base_output_dir': 'tmp/pytasksyn-backend'
            }
        }

        # Create SSE session
        session_id = uuid.uuid4().hex
        queue: asyncio.Queue = asyncio.Queue()
        SESSIONS[session_id] = queue

        async def _producer():
            init_logger(session_dir, console_output=True)
            prod_logger = get_logger()
            try:
                await queue.put(("progress", {"message": "🚀 Запуск конвейера"}))
                # Run pipeline in a thread to avoid blocking event loop
                results = await asyncio.to_thread(run_pipeline, config, session_dir)

                # Build mapping from comment_id to original text/line
                dedup_file = session_dir / "preprocess" / "code_review_deduplicated.csv"
                id_to_row = {}
                try:
                    import csv as _csv
                    with open(dedup_file, 'r', encoding='utf-8') as f:
                        reader = _csv.DictReader(f)
                        for row in reader:
                            try:
                                cid = int(row.get('comment_id', '0'))
                            except Exception:
                                continue
                            id_to_row[cid] = row
                except Exception:
                    pass

                expert_results = results.get('expert_results') or {}
                total_sent = 0
                for cid, er in expert_results.items():
                    if not er.get('success'):
                        continue
                    attempt_dir = Path(er['successful_attempt_dir'])
                    mc_path = attempt_dir / "microcase.txt"
                    try:
                        mc_text = mc_path.read_text(encoding='utf-8')
                    except Exception:
                        mc_text = ""
                    src_path = er.get('source_file_path')
                    src_line = er.get('source_line_number')
                    row = id_to_row.get(cid, {})
                    review_comment = row.get('comment', '')
                    await queue.put(("microcase", {
                        "microcase_id": cid,
                        "file_path": src_path,
                        "line_number": src_line,
                        "comment": mc_text or review_comment,
                        "review_comment": review_comment,
                        "solution": ""
                    }))
                    total_sent += 1

                await queue.put(("complete", {"message": "Генерация завершена", "total_accepted": total_sent}))
            except Exception as e:
                prod_logger.error(f"SSE producer failed: {e}")
                await queue.put(("error", {"message": str(e)}))
                await queue.put(("complete", {"message": "Завершено с ошибкой", "total_accepted": 0}))
            finally:
                try:
                    temp_dir_obj.cleanup()
                except Exception:
                    pass

        asyncio.create_task(_producer())
        return JSONResponse({"session_id": session_id}, status_code=202)
        
    except HTTPException:
        raise  # Re-raise HTTP exceptions
    except Exception as e:
        logger = get_logger()
        logger.error(f"Error processing request: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to process request: {str(e)}")

@app.get("/stream-microcases/{session_id}")
async def stream_microcases(session_id: str):
    queue = SESSIONS.get(session_id)
    if not queue:
        raise HTTPException(status_code=404, detail="Invalid session_id")

    async def event_generator():
        try:
            while True:
                event, data = await queue.get()
                yield sse_format(event, data)
                if event == "complete":
                    break
        finally:
            SESSIONS.pop(session_id, None)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

def start_server_with_ngrok():
    from pyngrok import ngrok
    
    # Set ngrok auth token from environment variable
    ngrok_api_key = os.getenv("NGROK_AUTHTOKEN")
    if ngrok_api_key:
        ngrok.set_auth_token(ngrok_api_key)
    else:
        print("Warning: NGROK_AUTHTOKEN not found in .env file")
    
    # Start ngrok tunnel
    public_url = ngrok.connect(8000)
    print(f"✅ ngrok tunnel established successfully!")
    print(f"🌐 Public URL: {public_url}")
    print(f"📝 API endpoint: {public_url}/gen-microcases/")
    
    # Start FastAPI server
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

def start_server():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FastAPI microcase generator backend")
    parser.add_argument("--ngrok", action="store_true", help="Start server with ngrok tunnel")
    parser.add_argument("--dev", action="store_true", help="Dev mode: limit to at most 1 comment")
    args = parser.parse_args()
    
    # Set global DEV flag
    DEV_MODE = bool(getattr(args, "dev", False))
    
    if args.ngrok:
        start_server_with_ngrok()
    else:
        start_server()