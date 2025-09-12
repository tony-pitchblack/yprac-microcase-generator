#!/usr/bin/env python3
import argparse
from fastapi import FastAPI, HTTPException
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
            # Only include comments that have file path and line number (review comments)
            if comment.get('path') and comment.get('line'):
                writer.writerow([
                    comment_id,
                    comment['path'],
                    comment['line'],
                    comment.get('body', ''),
                    comment.get('user', {}).get('login', 'Unknown')
                ])
                comment_id += 1
    
    return csv_path

@app.post("/gen-microcases/", status_code=200)
async def generate_microcases(request: GenerateMicrocaseRequest):
    # Initialize logger for console output
    logger = init_logger(console_output=True)
    logger.info(f"Received request - URL: {request.url}, User ID: {request.user_id}")
    
    # Parse GitHub PR URL
    pr_info = parse_github_pr_url(request.url)
    if not pr_info:
        raise HTTPException(status_code=400, detail="Invalid GitHub PR URL format")
    
    owner, repo, pr_number = pr_info
    logger.info(f"Parsed PR info - Owner: {owner}, Repo: {repo}, PR: {pr_number}")
    
    try:
        # Fetch all comments from the PR
        comments = await fetch_pr_comments(owner, repo, pr_number)
        
        logger.info(f"Found {len(comments)} comments in PR #{pr_number}")
        
        # Filter review comments (those with file path and line number)
        review_comments = [c for c in comments if c.get('path') and c.get('line')]
        logger.info(f"Found {len(review_comments)} review comments with file paths")
        
        if not review_comments:
            logger.warning("No review comments found with file paths - cannot generate microcases")
            return {
                "message": "No review comments with file paths found",
                "url": request.url,
                "user_id": request.user_id,
                "pr_info": {"owner": owner, "repo": repo, "pr_number": pr_number},
                "total_comments": len(comments),
                "review_comments": 0
            }
        
        # Setup session directory first
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_dir = Path("tmp") / "pytasksyn-backend" / f"session_{timestamp}"
        session_dir.mkdir(parents=True, exist_ok=True)
        
        # Create real project structure by fetching files from GitHub
        project_dir = session_dir / "source_project"
        project_dir.mkdir()
        
        # Fetch real files from GitHub repository
        await create_project_from_github(owner, repo, review_comments, project_dir)
        
        # Create temporary directory for CSV file
        with tempfile.TemporaryDirectory() as temp_dir_str:
            temp_dir = Path(temp_dir_str)
            
            # Create CSV from PR comments
            review_csv = await create_review_csv_from_comments(review_comments, temp_dir)
            
            # Load pytasksyn configuration with paths
            try:
                # Create minimal config instead of loading default (which has hardcoded paths)
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
                        'base_output_dir': 'data/pytasksyn'
                    }
                }
                
                # Re-initialize logger with session directory
                init_logger(session_dir, console_output=True)
                logger = get_logger()
                
                logger.info("Starting pytasksyn pipeline with PR data")
                
                # Run the pipeline
                results = run_pipeline(config, session_dir)
                
                # Return results
                return {
                    "message": "Microcase generation completed",
                    "url": request.url,
                    "user_id": request.user_id,
                    "pr_info": {"owner": owner, "repo": repo, "pr_number": pr_number},
                    "total_comments": len(comments),
                    "review_comments": len(review_comments),
                    "expert_results_count": len(results['expert_results']) if results['expert_results'] else 0,
                    "successful_microcases": sum(1 for r in results['expert_results'].values() if r['success']) if results['expert_results'] else 0,
                    "session_dir": str(results['session_dir'])
                }
                
            except Exception as e:
                logger.error(f"Pipeline execution failed: {str(e)}")
                raise HTTPException(status_code=500, detail=f"Pipeline execution failed: {str(e)}")
        
    except HTTPException:
        raise  # Re-raise HTTP exceptions
    except Exception as e:
        logger.error(f"Error processing request: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to process request: {str(e)}")

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
    args = parser.parse_args()
    
    if args.ngrok:
        start_server_with_ngrok()
    else:
        start_server()