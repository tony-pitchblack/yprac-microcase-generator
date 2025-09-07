#!/Users/a1111/micromamba/envs/ymg/bin/python
"""
estimate_context.py — Unicode-safe table for estimating context size using OpenAI tokenizer

Outputs a table: Directory | Lines | Characters | Chars/1000 | Tokens | Tokens/1000
- Hidden files/dirs are skipped.
- Tries to count only text-like files.
- Uses OpenAI's tiktoken for accurate token counting.
"""

import os
import sys
import argparse
import unicodedata
from pathlib import Path
import tiktoken

def get_display_width(text):
    """Calculate display width accounting for Unicode characters"""
    normalized = unicodedata.normalize("NFC", text)
    width = 0
    for char in normalized:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
    return width

def is_textlike(file_path):
    """Check if file is text-like based on extension and content"""
    text_extensions = {
        '.txt', '.md', '.rst', '.py', '.ipynb', '.sh', '.bash', '.zsh', '.fish',
        '.ts', '.tsx', '.js', '.jsx', '.json', '.yaml', '.yml', '.toml', '.ini',
        '.cfg', '.conf', '.env', '.properties', '.csv', '.tsv', '.html', '.htm',
        '.css', '.scss', '.less', '.vue', '.svelte', '.java', '.kt', '.gradle',
        '.groovy', '.go', '.rs', '.c', '.h', '.cpp', '.hpp', '.cc', '.mm', '.m',
        '.swift', '.rb', '.php', '.pl', '.lua', '.r', '.tex', '.bib', '.mk',
        '.make', '.cmake', '.dockerfile', '.sql', '.log'
    }
    
    binary_extensions = {
        '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.pdf', '.zip', '.tar',
        '.gz', '.bz2', '.7z', '.xz', '.rar', '.exe', '.dll', '.so', '.dylib',
        '.bin', '.o', '.obj', '.class', '.wasm'
    }
    
    suffix = file_path.suffix.lower()
    if suffix in text_extensions:
        return True
    if suffix in binary_extensions:
        return False
    
    # For unknown extensions, try to detect by content
    try:
        import subprocess
        result = subprocess.run(['file', '-b', '--mime', str(file_path)], 
                              capture_output=True, text=True, timeout=5)
        mime = result.stdout.strip()
        return any(pattern in mime for pattern in ['text/', 'json', 'xml', '+xml'])
    except:
        return False

def count_file_metrics(file_path):
    """Count lines, characters, and tokens for a single file"""
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        lines = content.count('\n') + (1 if content and not content.endswith('\n') else 0)
        chars = len(content)
        
        # Use OpenAI's GPT-4 tokenizer
        enc = tiktoken.get_encoding("cl100k_base")
        tokens = len(enc.encode(content))
        
        return lines, chars, tokens
    except Exception as e:
        print(f"Error reading {file_path}: {e}", file=sys.stderr)
        return 0, 0, 0

def find_directories(target_dir, recursive_depth):
    """Find all directories at the specified depth"""
    target_path = Path(target_dir).resolve()
    
    if recursive_depth == 0:
        # Only immediate subdirectories
        dirs = [d for d in target_path.iterdir() if d.is_dir() and not d.name.startswith('.')]
    else:
        dirs = []
        for root, dirnames, _ in os.walk(target_path):
            # Remove hidden directories
            dirnames[:] = [d for d in dirnames if not d.startswith('.')]
            
            root_path = Path(root)
            rel_path = root_path.relative_to(target_path)
            depth = len(rel_path.parts) if str(rel_path) != '.' else 0
            
            if depth <= recursive_depth:
                if root_path != target_path:  # Don't include the target directory itself
                    dirs.append(root_path)
    
    return sorted(dirs)

def is_leaf_directory(dir_path, target_dir, recursive_depth):
    """Check if directory is a leaf (has no subdirectories or is at max depth)"""
    if recursive_depth == 0:
        return True
    
    # Check current depth
    rel_path = dir_path.relative_to(Path(target_dir).resolve())
    current_depth = len(rel_path.parts)
    
    if current_depth >= recursive_depth:
        return True
    
    # Check if it has subdirectories
    try:
        subdirs = [d for d in dir_path.iterdir() if d.is_dir() and not d.name.startswith('.')]
        return len(subdirs) == 0
    except:
        return True

def process_directory(dir_path, target_dir):
    """Process a single directory and return metrics"""
    lines_sum = 0
    chars_sum = 0
    tokens_sum = 0
    
    # Only process files directly in this directory (not subdirectories)
    try:
        for file_path in dir_path.iterdir():
            if file_path.is_file() and not file_path.name.startswith('.'):
                if is_textlike(file_path):
                    lines, chars, tokens = count_file_metrics(file_path)
                    lines_sum += lines
                    chars_sum += chars
                    tokens_sum += tokens
    except Exception as e:
        print(f"Error processing directory {dir_path}: {e}", file=sys.stderr)
    
    # Calculate relative path for display
    target_path = Path(target_dir).resolve()
    try:
        rel_path = dir_path.relative_to(target_path)
        display_name = str(rel_path)
    except ValueError:
        display_name = str(dir_path)
    
    return display_name, lines_sum, chars_sum, tokens_sum

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--depth', type=int, default=0,
                       help='How many directory levels to list (0 = only immediate subdirectories)')
    parser.add_argument('directory', nargs='?', default='.',
                       help='Root directory to scan (default: current directory)')
    
    args = parser.parse_args()
    
    target_dir = args.directory
    recursive_depth = args.depth
    
    if not os.path.isdir(target_dir):
        print(f"ERROR: Not a directory: {target_dir}", file=sys.stderr)
        sys.exit(1)
    
    # Find all directories to process
    directories = find_directories(target_dir, recursive_depth)
    
    # Filter to only leaf directories
    leaf_directories = []
    for dir_path in directories:
        if is_leaf_directory(dir_path, target_dir, recursive_depth):
            leaf_directories.append(dir_path)
    
    # Process each directory
    results = []
    max_dir_width = 0
    
    for dir_path in leaf_directories:
        display_name, lines, chars, tokens = process_directory(dir_path, target_dir)
        results.append((display_name, lines, chars, tokens))
        max_dir_width = max(max_dir_width, get_display_width(display_name))
    
    # Sort results by directory name
    results.sort(key=lambda x: x[0])
    
    # Print table
    print("Directory")
    rule_len = max_dir_width + 72
    print("-" * rule_len)
    
    header_fmt = f"%-{max_dir_width}s %10s %12s %12s %10s %12s"
    print(header_fmt % ("", "Lines", "Characters", "Chars/1000", "Tokens", "Tokens/1000"))
    
    for display_name, lines, chars, tokens in results:
        width = get_display_width(display_name)
        padding = max_dir_width - width
        chars_k = chars / 1000.0
        tokens_k = tokens / 1000.0
        
        row_fmt = f"%s%{padding}s %10d %12d %12.1f %10d %12.1f"
        print(row_fmt % (display_name, "", lines, chars, chars_k, tokens, tokens_k))

if __name__ == "__main__":
    main()