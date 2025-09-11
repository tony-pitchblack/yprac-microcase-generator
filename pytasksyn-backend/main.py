#!/usr/bin/env python3
import argparse
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import os
from dotenv import load_dotenv
from pathlib import Path
import httpx
import re
from typing import Optional

# Load .env from root folder
root_dir = Path(__file__).parent.parent
env_path = root_dir / ".env"
load_dotenv(env_path)

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

@app.post("/gen-microcases/", status_code=200)
async def generate_microcases(request: GenerateMicrocaseRequest):
    print(f"Received request - URL: {request.url}, User ID: {request.user_id}")
    
    # Parse GitHub PR URL
    pr_info = parse_github_pr_url(request.url)
    if not pr_info:
        raise HTTPException(status_code=400, detail="Invalid GitHub PR URL format")
    
    owner, repo, pr_number = pr_info
    print(f"Parsed PR info - Owner: {owner}, Repo: {repo}, PR: {pr_number}")
    
    try:
        # Fetch all comments from the PR
        comments = await fetch_pr_comments(owner, repo, pr_number)
        
        print(f"\n=== Found {len(comments)} comments in PR #{pr_number} ===")
        for i, comment in enumerate(comments, 1):
            print(f"\n--- Comment {i} ---")
            print(f"Author: {comment.get('user', {}).get('login', 'Unknown')}")
            print(f"Created: {comment.get('created_at', 'Unknown')}")
            print(f"Body: {comment.get('body', 'No content')}")
            if comment.get('path'):
                print(f"File: {comment.get('path')}")
            if comment.get('line'):
                print(f"Line: {comment.get('line')}")
        
        return {
            "message": "PR comments fetched and printed",
            "url": request.url,
            "user_id": request.user_id,
            "pr_info": {"owner": owner, "repo": repo, "pr_number": pr_number},
            "comments_count": len(comments)
        }
        
    except Exception as e:
        print(f"Error fetching PR comments: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch PR comments: {str(e)}")

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