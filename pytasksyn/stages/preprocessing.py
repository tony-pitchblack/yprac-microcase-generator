import os
import csv
import time
from pathlib import Path
from langchain.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser

from utils.logging_utils import get_logger


class PreprocessingStage:
    def __init__(self, config, session_dir, preprocessor_llm):
        self.config = config
        self.session_dir = session_dir
        self.preprocessor_llm = preprocessor_llm
        self.parser = StrOutputParser()
    
    def run(self):
        """Run the preprocessing stage to deduplicate comments"""
        logger = get_logger()
        logger.processing("Starting preprocessing stage")
        
        preprocess_dir = self.session_dir / "preprocess"
        preprocess_dir.mkdir(exist_ok=True)
        
        # Read input CSV
        input_file = Path(self.config['paths']['code_review_file'])
        with open(input_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            comments = list(reader)
        
        # Add comment_id column (enumerate from 0)
        for i, comment in enumerate(comments):
            comment['comment_id'] = str(i)
        
        logger.info(f"Loaded {len(comments)} comments from {input_file}")
        
        # Deduplicate comments per file using LLM
        deduplicated_comments = self._deduplicate_comments(comments)
        
        # Save deduplicated file
        output_file = preprocess_dir / "code_review_deduplicated.csv"
        with open(output_file, 'w', encoding='utf-8', newline='') as f:
            if deduplicated_comments:
                fieldnames = deduplicated_comments[0].keys()
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(deduplicated_comments)
        
        logger.stage_complete("preprocessing", {
            "original_comments": len(comments),
            "deduplicated_comments": len(deduplicated_comments)
        })
        logger.info(f"Deduplicated file saved to: {output_file}")
        
        return output_file
    
    def _deduplicate_comments(self, comments):
        """Deduplicate comments per file using LLM"""
        # Group comments by file_path
        files_comments = {}
        for comment in comments:
            file_path = comment['file_path']
            if file_path not in files_comments:
                files_comments[file_path] = []
            files_comments[file_path].append(comment)
        
        deduplicated = []
        
        for file_path, file_comments in files_comments.items():
            if len(file_comments) == 1:
                # Only one comment for this file, keep it
                deduplicated.extend(file_comments)
                print(f"  Single comment for {file_path}, keeping it")
                continue
            
            print(f"  Deduplicating {len(file_comments)} comments for {file_path}")
            
            # Create local mapping for this file's comments
            file_comment_mapping = {i: comment for i, comment in enumerate(file_comments)}
            
            # Use LLM to deduplicate comments for this file
            comments_text = "\n".join([
                f"Comment {i}: {comment['comment']}"
                for i, comment in enumerate(file_comments)
            ])
            
            prompt_template = """Given these code review comments for file {file_path}, identify which comments are unique and should be kept. 
If comments are similar but one is more comprehensive, prefer the comprehensive one.
If comments address different issues, keep them all.

Comments:
{comments_text}

Return only the comment IDs (0, 1, 2, etc.) that should be kept, separated by commas (e.g., "0,2").
Do not include any other text or explanation:"""
            
            try:
                prompt = PromptTemplate(
                    template=prompt_template,
                    input_variables=["file_path", "comments_text"]
                )
                
                chain = prompt | self.preprocessor_llm | self.parser
                response = chain.invoke({
                    "file_path": file_path,
                    "comments_text": comments_text
                })
                
                print(f"    LLM response: '{response.strip()}'")
                
                # Parse response to get local comment IDs
                kept_local_ids = []
                response_clean = response.strip()
                
                # Handle different response formats
                if ',' in response_clean:
                    id_parts = response_clean.split(',')
                else:
                    id_parts = response_clean.split()
                
                for id_str in id_parts:
                    id_str = id_str.strip().replace('.', '').replace(':', '')
                    if id_str.isdigit():
                        local_id = int(id_str)
                        if local_id < len(file_comments):
                            kept_local_ids.append(local_id)
                
                if not kept_local_ids:
                    print(f"    Warning: No valid IDs found in response, keeping all comments")
                    kept_local_ids = list(range(len(file_comments)))
                
                print(f"    LLM selected {len(kept_local_ids)} unique comments: {kept_local_ids}")
                
                # Add selected comments to deduplicated list
                for local_id in kept_local_ids:
                    if local_id in file_comment_mapping:
                        deduplicated.append(file_comment_mapping[local_id])
                
                print(f"    Deduplicated {len(file_comments)} -> {len(kept_local_ids)} comments for {file_path}")
                        
            except Exception as e:
                print(f"    Error deduplicating comments for {file_path}: {e}")
                print(f"    Falling back to keeping all {len(file_comments)} comments")
                # Fallback: keep all comments for this file
                deduplicated.extend(file_comments)
        
        return deduplicated