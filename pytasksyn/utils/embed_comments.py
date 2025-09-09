#!/usr/bin/env python3
"""
Helper script to embed reviewer comments into corresponding source files.
Embeds comments using the template:

###### LINE {line_number} ################
{comment_body}
#####################################
"""

import os
import csv
import argparse
from pathlib import Path
from typing import Dict, List


def parse_args():
    parser = argparse.ArgumentParser(description='Embed code review comments into source files')
    parser.add_argument('--review-file', required=True, help='Path to code review CSV file')
    parser.add_argument('--project-root', required=True, help='Path to student project root directory')
    parser.add_argument('--output-dir', required=True, help='Output directory for embedded files')
    return parser.parse_args()


def load_review_comments(review_file: str) -> Dict[str, List[Dict]]:
    """Load review comments and group them by file_path"""
    comments_by_file = {}
    
    with open(review_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            file_path = row['file_path']
            if file_path not in comments_by_file:
                comments_by_file[file_path] = []
            
            comments_by_file[file_path].append({
                'line_number': int(row['line_number']),
                'comment': row['comment'],
                'comment_id': row.get('comment_id', '')
            })
    
    # Sort comments by line number for each file
    for file_path in comments_by_file:
        comments_by_file[file_path].sort(key=lambda x: x['line_number'])
    
    return comments_by_file


def embed_comments_in_file(source_file: str, comments: List[Dict], output_file: str):
    """Embed comments into a source file and save to output file"""
    # Read original file
    try:
        with open(source_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except (FileNotFoundError, UnicodeDecodeError) as e:
        print(f"Warning: Could not read {source_file}: {e}")
        # Create a placeholder file
        lines = [f"# Could not read original file: {e}\n"]
    
    # Create output with embedded comments
    output_lines = []
    line_index = 0
    
    for comment in comments:
        target_line = comment['line_number']
        
        # Add lines up to the comment line
        while line_index < target_line - 1 and line_index < len(lines):
            output_lines.append(lines[line_index])
            line_index += 1
        
        # Add the comment block before the target line
        comment_block = [
            f"###### LINE {target_line} ################\n",
            f"# {comment['comment']}\n",
            "#####################################\n"
        ]
        output_lines.extend(comment_block)
        
        # Add the target line if it exists
        if line_index < len(lines):
            output_lines.append(lines[line_index])
            line_index += 1
    
    # Add any remaining lines
    while line_index < len(lines):
        output_lines.append(lines[line_index])
        line_index += 1
    
    # Write output file
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.writelines(output_lines)


def main():
    args = parse_args()
    
    review_file = Path(args.review_file)
    project_root = Path(args.project_root)
    output_dir = Path(args.output_dir)
    
    if not review_file.exists():
        print(f"Error: Review file not found: {review_file}")
        return 1
    
    if not project_root.exists():
        print(f"Error: Project root not found: {project_root}")
        return 1
    
    # Load review comments
    print(f"Loading comments from: {review_file}")
    comments_by_file = load_review_comments(str(review_file))
    print(f"Found comments for {len(comments_by_file)} files")
    
    # Process each file with comments
    for file_path, comments in comments_by_file.items():
        print(f"Processing {file_path} ({len(comments)} comments)")
        
        # Resolve full source file path
        source_file = project_root / file_path
        
        # Create output file path (preserve directory structure)
        output_file = output_dir / file_path
        
        # Embed comments
        embed_comments_in_file(str(source_file), comments, str(output_file))
        print(f"  -> Embedded file saved to: {output_file}")
    
    print(f"\nAll embedded files saved to: {output_dir}")
    return 0


if __name__ == '__main__':
    exit(main())