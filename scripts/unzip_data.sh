#!/bin/bash

set -e

SOURCE_DIR="data"
TARGET_DIR="tmp/data_unzipped"

echo "Starting data processing from $SOURCE_DIR to $TARGET_DIR"

# Create target directory if it doesn't exist
mkdir -p "$TARGET_DIR"

# Function to process files recursively
process_directory() {
    local src_dir="$1"
    local target_dir="$2"
    
    # Create the target directory structure
    mkdir -p "$target_dir"
    
    # Process all items in the source directory
    find "$src_dir" -mindepth 1 -maxdepth 1 -type f | while read -r file; do
        local filename=$(basename "$file")
        local target_path="$target_dir/$filename"
        
        if [[ "$file" == *.zip ]]; then
            echo "Unzipping: $file -> $target_dir/"
            # Use LC_ALL=C to handle character encoding issues and force overwrite
            LC_ALL=C unzip -o -q "$file" -d "$target_dir" 2>/dev/null || {
                echo "Warning: Failed to unzip $file, trying with different encoding options..."
                # Try with different unzip options for character encoding
                LC_ALL=C unzip -o -O cp866 -q "$file" -d "$target_dir" 2>/dev/null || \
                LC_ALL=C unzip -o -O utf-8 -q "$file" -d "$target_dir" 2>/dev/null || \
                LC_ALL=C unzip -o -j "$file" -d "$target_dir" 2>/dev/null || \
                echo "Error: Could not unzip $file with any encoding"
            }
        else
            echo "Copying: $file -> $target_path"
            cp "$file" "$target_path"
        fi
    done
    
    # Process subdirectories recursively
    find "$src_dir" -mindepth 1 -maxdepth 1 -type d | while read -r subdir; do
        local subdir_name=$(basename "$subdir")
        local target_subdir="$target_dir/$subdir_name"
        echo "Processing directory: $subdir -> $target_subdir"
        process_directory "$subdir" "$target_subdir"
    done
}

# Check if source directory exists
if [ ! -d "$SOURCE_DIR" ]; then
    echo "Error: Source directory '$SOURCE_DIR' does not exist!"
    exit 1
fi

# Start processing
echo "Processing directory structure..."
process_directory "$SOURCE_DIR" "$TARGET_DIR"

echo "Data processing completed successfully!"
echo "Results are in: $TARGET_DIR"