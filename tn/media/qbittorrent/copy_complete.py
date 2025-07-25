import argparse
import os
import shutil
import glob

#!/usr/bin/env python3

def main():
    parser = argparse.ArgumentParser(description='Copy files based on category')
    parser.add_argument('category', help='Category name to match')
    parser.add_argument('root_dir', help='Root directory path')
    
    args = parser.parse_args()
    
    # Category to destination directory mapping
    category_mapping = {
        'abr-prowlarr': '/data/audiobooks'
    }
    
    if args.category not in category_mapping:
        print(f"Category '{args.category}' not found in mapping. Taking no action.")
        return
    
    destination = category_mapping[args.category]
    destination_path = os.path.join(args.root_dir, destination)
    
    print(f"Category: {args.category}")
    print(f"Destination: {destination_path}")
    
    # Check if root_dir is a file
    if os.path.isfile(args.root_dir):
        # Validate file extension
        file_extension = os.path.splitext(args.root_dir)[1].lower()
        if file_extension not in ['.m4b', '.mp3']:
            print(f"File '{args.root_dir}' does not have a valid audio extension (.m4b or .mp3). Taking no action.")
            return

        # Copy the file to the destination_path
        target_path = os.path.join(destination_path, os.path.basename(args.root_dir))
        print(f"Copying file: {args.root_dir} -> {target_path}")

        # Ensure destination directory exists
        os.makedirs(destination_path, exist_ok=True)

        # Copy the file
        shutil.copy2(args.root_dir, target_path)
        print("File copied successfully.")
        return

    # Escape bracket characters in the root directory path
    root_dir_escaped = glob.escape(args.root_dir)

    # Find all audio files in the root directory
    audio_files = glob.glob(os.path.join(root_dir_escaped, "*.m4b")) + glob.glob(os.path.join(root_dir_escaped, "*.mp3"))
    
    if len(audio_files) == 0:
        print("No audio files found in the root directory.")
        return
    
    if len(audio_files) == 1:
        # If there's only one audio file, copy just that file
        audio_file = audio_files[0]
        target_path = os.path.join(destination_path, os.path.basename(audio_file))
        print(f"Copying single audio file: {audio_file} -> {target_path}")
        
        # Ensure destination directory exists
        os.makedirs(destination_path, exist_ok=True)
        
        # Copy the file
        shutil.copy2(audio_file, target_path)
        print("File copied successfully.")
    else:
        # If there are multiple audio files, copy files with parent directory intact
        parent_dir = os.path.basename(args.root_dir)
        target_dir = os.path.join(destination_path, parent_dir)
        
        # Ensure destination directory exists
        os.makedirs(target_dir, exist_ok=True)
        
        print(f"Copying multiple audio files with parent directory: {parent_dir} -> {target_dir}")
        
        # Copy each audio file to the target directory
        for audio_file in audio_files:
            file_name = os.path.basename(audio_file)
            target_path = os.path.join(target_dir, file_name)
            shutil.copy2(audio_file, target_path)
            print(f"Copied: {file_name}")
        
        print(f"Successfully copied {len(audio_files)} audio files.")

if __name__ == '__main__':
    main()