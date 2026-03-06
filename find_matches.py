#!/usr/bin/env python3
"""
Find Matches - Find perceptually similar images to a given photo.
Searches an existing image_hashes.db database.
"""

import os
import sys
import argparse
import sqlite3
from pathlib import Path

try:
    from PIL import Image
    import imagehash
except ImportError:
    print("Error: Required packages not installed.")
    print("Run: pip install pillow imagehash")
    sys.exit(1)

# File picker
def pick_file():
    """Open a file picker dialog and return selected path."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        
        root = tk.Tk()
        root.withdraw()  # Hide the main window
        root.attributes('-topmost', True)  # Bring dialog to front
        
        file_path = filedialog.askopenfilename(
            title="Select an image to find matches for",
            filetypes=[
                ("Image files", "*.jpg *.jpeg *.png *.gif *.webp *.bmp *.tiff *.tif"),
                ("JPEG", "*.jpg *.jpeg"),
                ("PNG", "*.png"),
                ("All files", "*.*")
            ]
        )
        
        root.destroy()
        return file_path if file_path else None
    except Exception as e:
        print(f"Error opening file picker: {e}")
        return None

def find_matches(image_path, db_path, threshold=7, limit=50):
    """Find images in database that match the given image within threshold."""
    
    image_path = Path(image_path)
    if not image_path.exists():
        print(f"Error: Image not found: {image_path}")
        return []
    
    db_path = Path(db_path)
    if not db_path.exists():
        print(f"Error: Database not found: {db_path}")
        return []
    
    # Compute hash of input image
    try:
        img = Image.open(image_path)
        input_hash = imagehash.phash(img)
        print(f"Input image: {image_path.name}")
        print(f"Input hash:  {input_hash}")
        print(f"Threshold:   {threshold}")
        print()
    except Exception as e:
        print(f"Error reading image: {e}")
        return []
    
    # Search database
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('SELECT id, filepath, filename, phash, width, height, filesize FROM images WHERE phash IS NOT NULL')
    rows = cursor.fetchall()
    conn.close()
    
    print(f"Searching {len(rows)} images in database...")
    
    matches = []
    for row in rows:
        try:
            db_hash = imagehash.hex_to_hash(row['phash'])
            distance = input_hash - db_hash
            
            if distance <= threshold:
                matches.append({
                    'id': row['id'],
                    'filepath': row['filepath'],
                    'filename': row['filename'],
                    'distance': distance,
                    'width': row['width'],
                    'height': row['height'],
                    'filesize': row['filesize']
                })
        except Exception:
            continue
    
    # Sort by distance (closest first)
    matches.sort(key=lambda x: x['distance'])
    
    return matches[:limit]


def format_size(size):
    """Format file size in human-readable form."""
    if size is None:
        return "?"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def main():
    parser = argparse.ArgumentParser(
        description='Find perceptually similar images in a database.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  python find_matches.py                     # Opens file picker
  python find_matches.py photo.jpg
  python find_matches.py photo.jpg --threshold 5
  python find_matches.py photo.jpg --db /path/to/image_hashes.db
  python find_matches.py photo.jpg --threshold 10 --limit 100
        '''
    )
    
    parser.add_argument('image', nargs='?', default=None,
                        help='Path to the image (opens file picker if not provided)')
    parser.add_argument('--db', default='image_hashes.db',
                        help='Path to database (default: image_hashes.db in current directory)')
    parser.add_argument('--threshold', '-t', type=int, default=7,
                        help='Similarity threshold 0-20 (default: 7, lower=stricter)')
    parser.add_argument('--limit', '-l', type=int, default=50,
                        help='Maximum matches to show (default: 50)')
    parser.add_argument('--exists', '-e', action='store_true',
                        help='Only show matches where file still exists')
    
    args = parser.parse_args()
    
    # Get image path - use file picker if not provided
    image_path = args.image
    if not image_path:
        print("No image specified. Opening file picker...")
        image_path = pick_file()
        if not image_path:
            print("No file selected. Exiting.")
            sys.exit(0)
    
    # Find database
    db_path = Path(args.db)
    if not db_path.exists():
        # Try looking in script directory
        script_dir = Path(__file__).parent
        db_path = script_dir / 'image_hashes.db'
    
    matches = find_matches(image_path, db_path, args.threshold, args.limit)
    
    if not matches:
        print("No matches found.")
        return
    
    print(f"\nFound {len(matches)} match(es):\n")
    print("-" * 80)
    
    for i, m in enumerate(matches, 1):
        exists = os.path.exists(m['filepath'])
        
        if args.exists and not exists:
            continue
        
        status = "✓" if exists else "✗ MISSING"
        dims = f"{m['width']}x{m['height']}" if m['width'] and m['height'] else "?"
        size = format_size(m['filesize'])
        
        print(f"{i:3}. [d={m['distance']}] {status}")
        print(f"     {m['filename']}")
        print(f"     {dims} | {size}")
        print(f"     {m['filepath']}")
        print()


if __name__ == '__main__':
    main()