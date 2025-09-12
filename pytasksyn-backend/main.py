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
from typing import Optional, Union
import asyncio
import json
import uuid
import shutil
import subprocess
import hashlib
import yaml
import traceback

# Load .env from root folder
root_dir = Path(__file__).parent.parent
env_path = root_dir / ".env"
load_dotenv(env_path)

# Add root directory to path for pytasksyn imports
sys.path.insert(0, str(root_dir))

# Import pytasksyn modules
from pytasksyn.main import load_config, run_pipeline, create_llm
from pytasksyn.utils.logging_utils import init_logger, get_logger

app = FastAPI()

# Simple in-memory session storage for SSE
SESSIONS: dict[str, asyncio.Queue] = {}
LIMIT_CASES: Optional[int] = None
ENABLE_CACHE: bool = False

# In-memory mapping to track session context for solution checking
# Keyed by user_id → { session_id, session_dir, pr_url, microcase_attempt_dirs: {cid: attempt_dir} }
SESSION_CONTEXTS: dict[str, dict] = {}

def sse_format(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")

class GenerateMicrocaseRequest(BaseModel):
    url: str
    user_id: str

class CheckMicrocaseRequest(BaseModel):
    user_id: str
    microcase_id: Union[int, str]
    solution: str
    pr_url: Optional[str] = None

class EvaluateReviewRequest(BaseModel):
    user_id: str
    review: str
    pr_url: Optional[str] = None

def parse_github_pr_url(url: str) -> Optional[tuple[str, str, str]]:
    """Parse GitHub PR URL to extract owner, repo, and PR number."""
    pattern = r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)"
    match = re.match(pattern, url)
    if match:
        return match.groups()
    return None

def _hash_pull_request_url(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()

def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def _load_default_config() -> dict:
    """Load backend config. If pytasksyn-backend/config.yml is missing, copy from pytasksyn-backend/config_default.yml."""
    backend_cfg_dir = root_dir / "pytasksyn-backend"
    backend_cfg_path = backend_cfg_dir / "config.yml"
    backend_default_path = backend_cfg_dir / "config_default.yml"

    if not backend_cfg_path.exists():
        if not backend_default_path.exists():
            raise HTTPException(status_code=500, detail=f"Backend default config not found: {backend_default_path}")
        try:
            shutil.copy2(backend_default_path, backend_cfg_path)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to create backend config.yml from default: {e}")

    try:
        with open(backend_cfg_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read backend config: {e}")

def _apply_backend_overrides(base_config: dict, project_dir: Path, review_csv: Path) -> dict:
    """Merge dynamic backend paths and env-based model overrides into default config."""
    cfg = dict(base_config or {})

    # Paths (required)
    cfg.setdefault('paths', {})
    cfg['paths']['student_project'] = str(project_dir)
    cfg['paths']['code_review_file'] = str(review_csv)

    # Stages: by default keep tutor/student disabled unless default config explicitly enables
    cfg.setdefault('stages', {})
    cfg['stages'].setdefault('enable_tutor', False)
    cfg['stages'].setdefault('enable_student', False)

    # Models: optional env overrides to align with server deployment needs
    cfg.setdefault('models', {})
    cfg['models'].setdefault('preprocessor', {})
    cfg['models'].setdefault('expert', {})

    preprov = os.getenv("PREPROCESSOR_PROVIDER")
    premodel = os.getenv("PREPROCESSOR_MODEL")
    if preprov:
        cfg['models']['preprocessor']['provider'] = preprov
    if premodel:
        cfg['models']['preprocessor']['model_name'] = premodel

    expprov = os.getenv("EXPERT_PROVIDER")
    expmodel = os.getenv("EXPERT_MODEL")
    if expprov:
        cfg['models']['expert']['provider'] = expprov
    if expmodel:
        cfg['models']['expert']['model_name'] = expmodel

    # Output settings come from backend config files (config.yml/default)

    return cfg

def _resolve_limit_cases_from_config(cfg: dict) -> Optional[int]:
    """Resolve limit_cases from possible config locations."""
    try:
        gen = (cfg.get("generation") or {}) if isinstance(cfg, dict) else {}
        lim = (cfg.get("limits") or {}) if isinstance(cfg, dict) else {}
        for candidate in [gen.get("limit_cases"), lim.get("limit_cases"), (cfg.get("limit_cases") if isinstance(cfg, dict) else None)]:
            if candidate is None:
                continue
            if isinstance(candidate, int):
                return candidate
            try:
                return int(candidate)
            except Exception:
                continue
    except Exception:
        pass
    return None

def _cache_microcases(pr_url: str, session_dir: Path) -> None:
    if not ENABLE_CACHE:
        return
    pr_hash = _hash_pull_request_url(pr_url)
    storage_root = Path("tmp") / "pytasksyn-backend" / "microcase_storage" / pr_hash
    storage_root.mkdir(parents=True, exist_ok=True)

    report_path = Path(session_dir) / "script_report.json"
    if not report_path.exists():
        return

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return

    id_to_row: dict[int, dict] = {}
    dedup_file = Path(session_dir) / "preprocess" / "code_review_deduplicated.csv"
    try:
        with open(dedup_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    cid = int(row.get('comment_id', '0'))
                except Exception:
                    continue
                id_to_row[cid] = row
    except Exception:
        pass

    for entry in report:
        # Cache only accepted microcases (tutor must have accepted)
        if not entry.get("accepted"):
            continue
        try:
            cid = int(entry.get("comment_id"))
        except Exception:
            continue
        attempt_dir_str = entry.get("attempt_dir")
        if not attempt_dir_str:
            continue
        attempt_dir = Path(attempt_dir_str)
        tests_dir = attempt_dir / "tests"
        if not (attempt_dir.exists() and tests_dir.exists()):
            continue

        micro_dir = storage_root / f"microcase_{cid}"
        if micro_dir.exists():
            shutil.rmtree(micro_dir, ignore_errors=True)
        micro_dir.mkdir(parents=True, exist_ok=True)

        shutil.copytree(tests_dir, micro_dir / "tests", dirs_exist_ok=True)

        sol_path = attempt_dir / "solution_expert.py"
        if sol_path.exists():
            shutil.copy2(sol_path, micro_dir / "solution_expert.py")

        mc_text = ""
        try:
            mc_text = (attempt_dir / "microcase.txt").read_text(encoding='utf-8')
        except Exception:
            pass
        row = id_to_row.get(cid, {})
        if not mc_text:
            mc_text = row.get('comment') or ""
        file_path = row.get('file_path') or None
        line_number = None
        try:
            ln = row.get('line_number')
            line_number = int(ln) if ln is not None and ln != '' else None
        except Exception:
            line_number = None

        meta = {
            "microcase_id": cid,
            "pull_request_url": pr_url,
            "file_path": file_path,
            "line_number": line_number,
            "microcase_text": mc_text
        }
        _write_json(micro_dir / "microcase.json", meta)

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
            # Build structured diagnostics for better troubleshooting (403, rate limits, scopes, SSO)
            provider_message = None
            documentation_url = None
            provider_body_text = None
            try:
                err_json = response.json()
                provider_message = err_json.get("message")
                documentation_url = err_json.get("documentation_url")
                provider_body_text = json.dumps(err_json)
            except Exception:
                try:
                    provider_body_text = (response.text or "")
                except Exception:
                    provider_body_text = None

            headers_lower = {k.lower(): v for k, v in response.headers.items()}
            diagnostics = {
                "message": "Failed to fetch PR details",
                "provider_status": response.status_code,
                "provider_message": provider_message,
                "documentation_url": documentation_url,
                "request_url": pr_url,
                "token_present": bool(github_token),
                "rate_limit": {
                    "limit": headers_lower.get("x-ratelimit-limit"),
                    "remaining": headers_lower.get("x-ratelimit-remaining"),
                    "reset": headers_lower.get("x-ratelimit-reset"),
                },
                "oauth_scopes": headers_lower.get("x-oauth-scopes"),
                "accepted_oauth_scopes": headers_lower.get("x-accepted-oauth-scopes"),
                "sso": headers_lower.get("x-github-sso"),
                "provider_body": (provider_body_text or "")[:1000] or None,
            }
            logger = get_logger()
            try:
                # Avoid logging large bodies
                log_copy = {k: v for k, v in diagnostics.items() if k != "provider_body"}
                logger.warning(f"PR details fetch failed: {json.dumps(log_copy, ensure_ascii=False)}")
            except Exception:
                logger.warning("PR details fetch failed (could not serialize diagnostics)")
            raise HTTPException(status_code=502, detail=diagnostics)
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
    pr_url = request.url
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
        
        # Determine limit_cases from backend config (CLI can override)
        limit_cases = None
        try:
            base_cfg_for_limits = _load_default_config()
            limit_cases = _resolve_limit_cases_from_config(base_cfg_for_limits)
        except Exception:
            pass
        
        # CLI override if provided
        if LIMIT_CASES is not None:
            limit_cases = LIMIT_CASES
        
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
        
        # Limit number of review comments to process
        if limit_cases and limit_cases > 0 and len(review_comments) > int(limit_cases):
            review_comments = review_comments[:limit_cases]
            logger.info(f"Limiting review comments to {limit_cases} (from config or CLI)")
        
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

        # Load default config and apply backend overrides
        base_config = _load_default_config()
        config = _apply_backend_overrides(base_config, project_dir, review_csv)
        # Persist effective config for this session
        try:
            with open(session_dir / "config_used.yml", 'w', encoding='utf-8') as f:
                yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
        except Exception:
            pass

        # Create SSE session
        session_id = uuid.uuid4().hex
        queue: asyncio.Queue = asyncio.Queue()
        SESSIONS[session_id] = queue

        # Initialize session context for this user to enable solution checking later
        SESSION_CONTEXTS[request.user_id] = {
            "session_id": session_id,
            "session_dir": str(session_dir),
            "microcase_attempt_dirs": {},
            "pr_url": pr_url
        }

        async def _producer():
            init_logger(session_dir, console_output=True)
            prod_logger = get_logger()
            try:
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
                tutor_results = results.get('tutor_results') or {}
                total_sent = 0
                for cid, er in expert_results.items():
                    if not er.get('success'):
                        continue
                    # If tutor stage ran, only stream accepted by tutor
                    if tutor_results:
                        tr = tutor_results.get(cid)
                        if not tr or not tr.get('accepted'):
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
                    # Update session context mapping for solution checking (defer to report later)

                    await queue.put(("microcase", {
                        "microcase_id": cid,
                        "file_path": src_path,
                        "line_number": src_line,
                        "comment": mc_text or review_comment,
                        "review_comment": review_comment,
                        "solution": ""
                    }))
                    total_sent += 1

                # Persist latest successful microcases for this PR
                try:
                    _cache_microcases(pr_url, session_dir)
                except Exception:
                    pass

                await queue.put(("complete", {"message": "Генерация завершена", "total_accepted": total_sent}))
            except Exception as e:
                tb = traceback.format_exc()
                prod_logger.error(f"SSE producer failed: {e}\n{tb}")
                await queue.put(("error", {"message": str(e), "traceback": tb}))
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
        tb = traceback.format_exc()
        logger.error(f"Error processing request: {str(e)}\n{tb}")
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
    
    # Print configuration on startup
    try:
        config = _load_default_config()
        cfg_limit = _resolve_limit_cases_from_config(config)
        effective_limit = LIMIT_CASES if LIMIT_CASES is not None else cfg_limit
        print(f"Microcase limit: {effective_limit}")
    except Exception as e:
        print(f"⚠️  Could not load config for startup info: {e}")
    
    # Start ngrok tunnel
    public_url = ngrok.connect(8000)
    print(f"✅ ngrok tunnel established successfully!")
    print(f"🌐 Public URL: {public_url}")
    print(f"📝 API endpoint: {public_url}/gen-microcases/")
    
    # Start FastAPI server
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

def start_server():
    # Print configuration on startup
    try:
        config = _load_default_config()
        cfg_limit = _resolve_limit_cases_from_config(config)
        effective_limit = LIMIT_CASES if LIMIT_CASES is not None else cfg_limit
        print(f"Microcase limit: {effective_limit}")
    except Exception as e:
        print(f"⚠️  Could not load config for startup info: {e}")
    
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)


def _run_student_tests(attempt_dir: Path, solution_code_text: str) -> tuple[bool, str, str]:
    """Run pytest for student's solution against generated tests.

    Writes student's code as solution_expert.py so tests can import it.
    Returns (success, stdout, stderr).
    """
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            tests_dir = Path(attempt_dir) / "tests"
            if not tests_dir.exists():
                return False, "", "Tests directory not found"

            # Write student's solution with expected module name
            (tmp_path / "solution_expert.py").write_text(solution_code_text, encoding="utf-8")

            # Ensure pytest can import solution_expert from the temp dir
            env = os.environ.copy()
            env["PYTHONPATH"] = f"{str(tmp_path)}{os.pathsep}{env.get('PYTHONPATH', '')}"

            # Run pytest against the session tests directory
            result = subprocess.run([
                sys.executable, "-m", "pytest", "-q", "tests/"
            ], cwd=attempt_dir, env=env, capture_output=True, text=True)

            return result.returncode == 0, result.stdout, result.stderr
    except Exception as e:
        return False, "", f"Error running tests: {e}"


@app.post("/check-microcase/")
async def check_microcase(request: CheckMicrocaseRequest):
    # Resolve attempt dir for given microcase id (prefer cache if pr_url provided)
    try:
        mc_id_int = int(request.microcase_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid microcase_id")

    # 1) Try cache by pr_url if provided
    attempt_dir_path: Optional[Path] = None
    if ENABLE_CACHE and request.pr_url:
        try:
            pr_hash = _hash_pull_request_url(request.pr_url)
            storage_root = Path("tmp") / "pytasksyn-backend" / "microcase_storage" / pr_hash
            micro_dir = storage_root / f"microcase_{mc_id_int}"
            if (micro_dir / "tests").exists():
                attempt_dir_path = micro_dir
        except Exception:
            attempt_dir_path = None

    # 2) Fallback to active session mapping
    if attempt_dir_path is None:
        ctx = SESSION_CONTEXTS.get(request.user_id)
        if not ctx:
            raise HTTPException(status_code=404, detail="No active session for this user")
        session_dir = ctx.get("session_dir")
        if not session_dir:
            raise HTTPException(status_code=409, detail="Session directory not available")
        report_path = Path(session_dir) / "script_report.json"
        if not report_path.exists():
            raise HTTPException(status_code=409, detail="Report not available yet")
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            raise HTTPException(status_code=500, detail="Failed to read report")

        attempt_dir_str = None
        for entry in report:
            try:
                if int(entry.get("comment_id")) == mc_id_int:
                    attempt_dir_str = entry.get("attempt_dir")
                    break
            except Exception:
                continue
        if not attempt_dir_str:
            # Make message clearer if pr_url was provided but id not found in cache
            if request.pr_url:
                raise HTTPException(status_code=404, detail="Cached microcase not found for given pr_url and microcase_id")
            raise HTTPException(status_code=409, detail="Tests for this microcase are not available yet")
        attempt_dir_path = Path(attempt_dir_str)

    autotest_path = attempt_dir_path / "tests" / "test_microcase.py"
    success, out, err = _run_student_tests(attempt_dir_path, request.solution)
    if success:
        # Persist student's passing solution under PR cache for later review evaluation
        if ENABLE_CACHE:
            try:
                pr_url = request.pr_url or (SESSION_CONTEXTS.get(request.user_id) or {}).get("pr_url")
                if pr_url:
                    pr_hash = _hash_pull_request_url(pr_url)
                    storage_root = Path("tmp") / "pytasksyn-backend" / "microcase_storage" / pr_hash
                    micro_dir = storage_root / f"microcase_{mc_id_int}"
                    student_dir = micro_dir / "student_solutions"
                    student_dir.mkdir(parents=True, exist_ok=True)
                    (student_dir / f"{request.user_id}.py").write_text(request.solution, encoding="utf-8")
            except Exception:
                pass
        return {"status": "passed"}

    # On failure, provide brief explanation
    explanation = (out or err or "Tests failed").strip()
    if len(explanation) > 4000:
        explanation = explanation[-4000:]
    return {
        "status": "failed",
        "explanation": explanation,
        "attempt_dir": str(attempt_dir_path),
        "autotest_path": str(autotest_path)
    }

@app.post("/evaluate-review/")
async def evaluate_review(request: EvaluateReviewRequest):
    # Determine PR URL
    pr_url = request.pr_url or (SESSION_CONTEXTS.get(request.user_id) or {}).get("pr_url")
    if not pr_url:
        raise HTTPException(status_code=400, detail="pr_url is required (not found in session)")

    pr_hash = _hash_pull_request_url(pr_url)
    storage_root = Path("tmp") / "pytasksyn-backend" / "microcase_storage" / pr_hash
    if not storage_root.exists():
        raise HTTPException(status_code=404, detail="No cached microcases for this PR")

    # Load all microcases and expert solutions
    all_microcases: list[dict] = []
    expert_solutions: dict[int, str] = {}
    student_solutions: dict[int, str] = {}

    try:
        for mc_dir in sorted(storage_root.glob("microcase_*")):
            try:
                mc_json = mc_dir / "microcase.json"
                meta = json.loads(mc_json.read_text(encoding="utf-8")) if mc_json.exists() else {}
            except Exception:
                meta = {}
            try:
                mc_id = int(meta.get("microcase_id")) if meta.get("microcase_id") is not None else None
            except Exception:
                mc_id = None
            all_microcases.append({
                "microcase_id": mc_id,
                "file_path": meta.get("file_path"),
                "line_number": meta.get("line_number"),
                "microcase": meta.get("microcase_text") or ""
            })
            # Expert solution
            exp_path = mc_dir / "solution_expert.py"
            if mc_id is not None and exp_path.exists():
                try:
                    expert_solutions[mc_id] = exp_path.read_text(encoding="utf-8")
                except Exception:
                    pass
            # Student solution for this user (if exists)
            stu_path = mc_dir / "student_solutions" / f"{request.user_id}.py"
            if mc_id is not None and stu_path.exists():
                try:
                    student_solutions[mc_id] = stu_path.read_text(encoding="utf-8")
                except Exception:
                    pass
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load microcases: {e}")

    # Build prompt context
    solved_ids = sorted(k for k in student_solutions.keys())
    if not solved_ids:
        raise HTTPException(status_code=409, detail="No solved microcases found for this user")

    def _fmt_cases(cases: list[dict]) -> str:
        parts = []
        for c in cases:
            cid = c.get("microcase_id")
            body = c.get("microcase") or ""
            fp = c.get("file_path") or ""
            ln = c.get("line_number")
            loc = f"{fp}:{ln}" if fp else ""
            parts.append(f"- ID {cid} {('('+loc+')') if loc else ''}:\n{body}\n")
        return "\n".join(parts)

    def _fmt_code_map(code_map: dict[int, str], title: str) -> str:
        lines = [title]
        for cid in sorted(code_map.keys()):
            code = code_map[cid]
            lines.append(f"[ID {cid}]\n```python\n{code}\n```\n")
        return "\n".join(lines)

    all_cases_text = _fmt_cases(all_microcases)
    expert_code_text = _fmt_code_map({cid: expert_solutions.get(cid, '') for cid in sorted(expert_solutions.keys())}, "Эталонные решения (эксперт):")
    student_code_text = _fmt_code_map({cid: student_solutions[cid] for cid in solved_ids}, "Решения студента (только решённые):")

    prompt = (
        "Ты — преподаватель программирования. Оцени, насколько хорошо студент усвоил материал по решённым им микро-кейсам.\n"
        "Даны:\n"
        "1) Все микрокейсы (для контекста):\n" + all_cases_text + "\n\n"
        "2) Эталонные решения эксперта (могут отсутствовать для некоторых кейсов):\n" + expert_code_text + "\n\n"
        "3) Решения студента по тем кейсам, которые он прошёл тестами:\n" + student_code_text + "\n\n"
        "4) Текстовое ревью студента по своим решениям:\n" + (request.review or "") + "\n\n"
        "Оцени понимание по шкале 0..100, где 0 — не понял, 100 — отлично понял.\n"
        "Ответь строго JSON c двумя полями: {\"score\": <целое 0..100>, \"fedback\": <краткий комментарий>}"
    )

    # Create LLM instance for evaluation
    model_provider = os.getenv("REVIEW_PROVIDER", "yandex")
    model_name = os.getenv("REVIEW_MODEL", "yandexgpt-lite")
    try:
        llm = create_llm({"provider": model_provider, "model_name": model_name})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LLM init failed: {e}")

    # Invoke LLM
    try:
        response = None
        try:
            response = llm.invoke(prompt)  # type: ignore
        except Exception:
            # Fallback to callable interface
            response = llm(prompt)  # type: ignore
        if hasattr(response, "content"):
            text = getattr(response, "content")
        else:
            text = str(response)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}")

    # Parse JSON/score
    score: Optional[int] = None
    fedback: Optional[str] = None
    try:
        obj = json.loads(text)
        score = int(obj.get("score"))
        fedback = obj.get("fedback") or obj.get("feedback") or obj.get("comment")
    except Exception:
        # Try to extract first integer 0..100
        try:
            m = re.search(r"\b(100|\d{1,2})\b", text)
            if m:
                score = int(m.group(1))
            fedback = text.strip()
        except Exception:
            pass
    if score is None:
        raise HTTPException(status_code=500, detail="Failed to parse score from LLM response")
    score = max(0, min(100, score))
    return {"score": score, "fedback": (fedback or "").strip()}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FastAPI microcase generator backend")
    parser.add_argument("--ngrok", action="store_true", help="Start server with ngrok tunnel")
    parser.add_argument("--limit-cases", type=int, default=None, help="Limit number of review comments to process")
    args = parser.parse_args()
    
    # Set global limit
    LIMIT_CASES = getattr(args, "limit_cases", None)
    
    if args.ngrok:
        start_server_with_ngrok()
    else:
        start_server()