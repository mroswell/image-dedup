#!/usr/bin/env python3
"""
Image Scanner - Scans directories for images and computes perceptual hashes.
Stores results in SQLite for the web UI to display.
"""

import os
import sys
import argparse
import sqlite3
import hashlib
import json
from pathlib import Path
from datetime import datetime

try:
    from PIL import Image
    import imagehash
except ImportError:
    print("Error: Required packages not installed.")
    print("Run: pip install pillow imagehash")
    sys.exit(1)

# Optional: osxmetadata for reading Finder tags
try:
    import osxmetadata
    HAS_OSXMETADATA = True
except ImportError:
    HAS_OSXMETADATA = False
    print("Note: osxmetadata not installed. Tags will not be read.")
    print("Run: pip install osxmetadata")

# Supported image extensions
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff', '.tif'}

# Paths
SCRIPT_DIR = Path(__file__).parent
DB_PATH = SCRIPT_DIR / "image_hashes.db"
THUMBNAIL_DIR = SCRIPT_DIR / "thumbnails"
THUMBNAIL_SIZE = (200, 200)


def init_database():
    """Initialize SQLite database with required tables."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filepath TEXT UNIQUE NOT NULL,
            filename TEXT NOT NULL,
            filesize INTEGER,
            width INTEGER,
            height INTEGER,
            modified_time REAL,
            phash TEXT,
            dhash TEXT,
            thumbnail_path TEXT,
            tags TEXT,
            scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Add tags column if it doesn't exist (for existing databases)
    try:
        cursor.execute('ALTER TABLE images ADD COLUMN tags TEXT')
    except sqlite3.OperationalError:
        pass  # Column already exists
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS duplicate_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_hash TEXT NOT NULL,
            threshold INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Add completed_at column if it doesn't exist (for existing databases)
    try:
        cursor.execute('ALTER TABLE duplicate_groups ADD COLUMN completed_at TIMESTAMP')
    except sqlite3.OperationalError:
        pass  # Column already exists
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS group_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            image_id INTEGER NOT NULL,
            distance INTEGER DEFAULT 0,
            FOREIGN KEY (group_id) REFERENCES duplicate_groups(id),
            FOREIGN KEY (image_id) REFERENCES images(id)
        )
    ''')
    
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_phash ON images(phash)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_filepath ON images(filepath)')
    
    conn.commit()
    return conn


def get_file_info(filepath):
    """Get file metadata."""
    stat = os.stat(filepath)
    return {
        'filesize': stat.st_size,
        'modified_time': stat.st_mtime
    }


def get_file_tags(filepath):
    """Get Finder tags from a file."""
    if not HAS_OSXMETADATA:
        return []
    try:
        md = osxmetadata.OSXMetaData(filepath)
        tags = md.tags
        # Extract just the tag names (tags come as Tag objects)
        return [str(tag.name) if hasattr(tag, 'name') else str(tag) for tag in tags]
    except Exception as e:
        return []


def generate_thumbnail(image_path, thumb_path):
    """Generate a thumbnail for the image."""
    try:
        with Image.open(image_path) as img:
            img.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
            # Convert to RGB if necessary (for PNG with transparency, etc.)
            if img.mode in ('RGBA', 'LA', 'P'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            img.save(thumb_path, 'JPEG', quality=85)
        return True
    except Exception as e:
        print(f"  Warning: Could not generate thumbnail: {e}")
        return False


def compute_hashes(image_path):
    """Compute perceptual hashes for an image."""
    try:
        with Image.open(image_path) as img:
            # Get dimensions
            width, height = img.size
            
            # Compute hashes
            phash = str(imagehash.phash(img))
            dhash = str(imagehash.dhash(img))
            
            return {
                'width': width,
                'height': height,
                'phash': phash,
                'dhash': dhash
            }
    except Exception as e:
        print(f"  Error processing {image_path}: {e}")
        return None


def find_images(root_path):
    """Recursively find all image files."""
    images = []
    root = Path(root_path)
    
    # Directories to skip entirely
    SKIP_DIRS = {
        'venv', '.venv', 'env', '.env',  # Python virtual environments
        '__pycache__', '.pytest_cache',   # Python cache directories
        'node_modules',                    # Node.js dependencies
        'site-packages', 'dist-packages', # Python package directories
        '.git', '.svn', '.hg',            # Version control
        'thumbnails',                      # Our own thumbnails
        '.Trash', '.Spotlight-V100',      # macOS system directories
    }
    
    for ext in IMAGE_EXTENSIONS:
        images.extend(root.rglob(f'*{ext}'))
        images.extend(root.rglob(f'*{ext.upper()}'))
    
    # Prefixes to skip (user-defined markers for files to ignore)
    SKIP_PREFIXES = ('._', '.', 'ERR_', 'SKIP_', 'DUP_')
    
    # Remove duplicates (case-insensitive filesystems) and skip hidden/system files
    seen = set()
    unique_images = []
    skipped_hidden = 0
    skipped_prefixes = 0
    skipped_dirs = 0
    
    for img in images:
        filename = img.name
        
        # Skip macOS resource fork files and hidden files
        if filename.startswith('._') or filename.startswith('.'):
            skipped_hidden += 1
            continue
        
        # Skip files with user-defined skip prefixes (ERR_, SKIP_, DUP_)
        if filename.startswith(('ERR_', 'SKIP_', 'DUP_')):
            skipped_prefixes += 1
            continue
        
        # Skip files in hidden directories (like .Trash, .Spotlight-V100)
        if any(part.startswith('.') for part in img.parts):
            skipped_hidden += 1
            continue
        
        # Skip files in excluded directories (venv, __pycache__, etc.)
        if any(part in SKIP_DIRS for part in img.parts):
            skipped_dirs += 1
            continue
        
        key = str(img).lower()
        if key not in seen:
            seen.add(key)
            unique_images.append(img)
    
    if skipped_hidden > 0:
        print(f"Skipped {skipped_hidden} hidden/system files (._*, .DS_Store, etc.)")
    if skipped_prefixes > 0:
        print(f"Skipped {skipped_prefixes} flagged files (ERR_*, SKIP_*, DUP_*)")
    if skipped_dirs > 0:
        print(f"Skipped {skipped_dirs} files in excluded directories (venv, __pycache__, etc.)")
    
    return unique_images


def scan_images(root_path, force_rescan=False):
    """Scan all images in the given path."""
    conn = init_database()
    cursor = conn.cursor()
    
    # Ensure thumbnail directory exists
    THUMBNAIL_DIR.mkdir(exist_ok=True)
    
    print(f"Scanning for images in: {root_path}")
    images = find_images(root_path)
    total = len(images)
    print(f"Found {total} images")
    
    if total == 0:
        print("No images found. Check the path and try again.")
        return conn
    
    processed = 0
    skipped = 0
    errors = 0
    
    for i, image_path in enumerate(images, 1):
        filepath = str(image_path.absolute())
        filename = image_path.name
        
        # Progress indicator
        pct = (i / total) * 100
        print(f"\r[{pct:5.1f}%] Processing {i}/{total}: {filename[:40]:<40}", end='', flush=True)
        
        # Check if already scanned
        if not force_rescan:
            cursor.execute('SELECT modified_time FROM images WHERE filepath = ?', (filepath,))
            row = cursor.fetchone()
            if row:
                file_info = get_file_info(filepath)
                if abs(row[0] - file_info['modified_time']) < 1:  # Within 1 second
                    skipped += 1
                    continue
        
        # Get file info
        try:
            file_info = get_file_info(filepath)
        except OSError as e:
            print(f"\n  Error accessing {filepath}: {e}")
            errors += 1
            continue
        
        # Compute hashes
        hash_info = compute_hashes(filepath)
        if hash_info is None:
            errors += 1
            continue
        
        # Generate thumbnail
        thumb_filename = hashlib.md5(filepath.encode()).hexdigest() + '.jpg'
        thumb_path = THUMBNAIL_DIR / thumb_filename
        if not thumb_path.exists():
            generate_thumbnail(filepath, thumb_path)
        
        # Get Finder tags
        tags = get_file_tags(filepath)
        tags_json = json.dumps(tags) if tags else '[]'
        
        # Insert or update database
        cursor.execute('''
            INSERT OR REPLACE INTO images 
            (filepath, filename, filesize, width, height, modified_time, phash, dhash, thumbnail_path, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            filepath,
            filename,
            file_info['filesize'],
            hash_info['width'],
            hash_info['height'],
            file_info['modified_time'],
            hash_info['phash'],
            hash_info['dhash'],
            str(thumb_path),
            tags_json
        ))
        
        processed += 1
        
        # Commit periodically
        if i % 100 == 0:
            conn.commit()
    
    conn.commit()
    print(f"\n\nScan complete!")
    print(f"  Processed: {processed}")
    print(f"  Skipped (unchanged): {skipped}")
    print(f"  Errors: {errors}")
    
    return conn


def find_duplicates(conn, threshold=5):
    """Find duplicate groups based on perceptual hash similarity."""
    cursor = conn.cursor()
    
    print(f"\nFinding duplicates with threshold {threshold}...")
    
    # Save completed_at status before clearing (keyed by group_hash)
    cursor.execute('SELECT group_hash, completed_at FROM duplicate_groups WHERE completed_at IS NOT NULL')
    completed_groups = {row[0]: row[1] for row in cursor.fetchall()}
    if completed_groups:
        print(f"Preserving completion status for {len(completed_groups)} groups...")
    
    # Clear existing groups
    cursor.execute('DELETE FROM group_members')
    cursor.execute('DELETE FROM duplicate_groups')
    
    # Get all images with hashes
    cursor.execute('SELECT id, filepath, phash FROM images WHERE phash IS NOT NULL')
    images = cursor.fetchall()
    
    print(f"Comparing {len(images)} images...")
    
    # Build groups using Union-Find approach
    parent = {img[0]: img[0] for img in images}
    
    def find(x):
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]
    
    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py
    
    # Compare all pairs
    total_comparisons = len(images) * (len(images) - 1) // 2
    comparison_count = 0
    
    for i, (id1, path1, hash1) in enumerate(images):
        h1 = imagehash.hex_to_hash(hash1)
        
        for id2, path2, hash2 in images[i+1:]:
            comparison_count += 1
            if comparison_count % 100000 == 0:
                pct = (comparison_count / total_comparisons) * 100
                print(f"\r  Progress: {pct:.1f}%", end='', flush=True)
            
            h2 = imagehash.hex_to_hash(hash2)
            distance = h1 - h2
            
            if distance <= threshold:
                union(id1, id2)
    
    print(f"\r  Progress: 100.0%")
    
    # Build groups from union-find results
    groups = {}
    for img_id in parent:
        root = find(img_id)
        if root not in groups:
            groups[root] = []
        groups[root].append(img_id)
    
    # Filter to only groups with more than one image
    duplicate_groups = {k: v for k, v in groups.items() if len(v) > 1}
    
    print(f"Found {len(duplicate_groups)} groups of duplicates")
    
    # Store groups in database
    restored_count = 0
    for group_root, members in duplicate_groups.items():
        # Get the hash of the root image for the group
        cursor.execute('SELECT phash FROM images WHERE id = ?', (group_root,))
        group_hash = cursor.fetchone()[0]
        
        # Check if this group was previously completed
        completed_at = completed_groups.get(group_hash)
        
        cursor.execute('''
            INSERT INTO duplicate_groups (group_hash, threshold, completed_at)
            VALUES (?, ?, ?)
        ''', (group_hash, threshold, completed_at))
        group_id = cursor.lastrowid
        
        if completed_at:
            restored_count += 1
        
        # Add members
        for img_id in members:
            cursor.execute('SELECT phash FROM images WHERE id = ?', (img_id,))
            img_hash = cursor.fetchone()[0]
            
            # Calculate distance from group hash
            h1 = imagehash.hex_to_hash(group_hash)
            h2 = imagehash.hex_to_hash(img_hash)
            distance = h1 - h2
            
            cursor.execute('''
                INSERT INTO group_members (group_id, image_id, distance)
                VALUES (?, ?, ?)
            ''', (group_id, img_id, distance))
    
    conn.commit()
    
    if restored_count:
        print(f"Restored completion status for {restored_count} groups")
    
    # Summary
    total_dupes = sum(len(m) for m in duplicate_groups.values())
    print(f"Total images in duplicate groups: {total_dupes}")
    
    return len(duplicate_groups)


def main():
    parser = argparse.ArgumentParser(
        description='Scan images and find duplicates using perceptual hashing'
    )
    parser.add_argument(
        'path',
        help='Path to directory containing images'
    )
    parser.add_argument(
        '--threshold', '-t',
        type=int,
        default=5,
        help='Similarity threshold (0=exact, higher=more lenient). Default: 5'
    )
    parser.add_argument(
        '--rescan', '-r',
        action='store_true',
        help='Force rescan of all images (ignore cache)'
    )
    parser.add_argument(
        '--find-only', '-f',
        action='store_true',
        help='Skip scanning, only find duplicates from existing data'
    )
    
    args = parser.parse_args()
    
    # Validate path
    if not args.find_only:
        if not os.path.exists(args.path):
            print(f"Error: Path does not exist: {args.path}")
            sys.exit(1)
        if not os.path.isdir(args.path):
            print(f"Error: Path is not a directory: {args.path}")
            sys.exit(1)
    
    # Run scan
    if args.find_only:
        conn = init_database()
    else:
        conn = scan_images(args.path, force_rescan=args.rescan)
    
    # Find duplicates
    num_groups = find_duplicates(conn, threshold=args.threshold)
    
    conn.close()
    
    if num_groups > 0:
        print(f"\nRun 'python app.py' to review duplicates in the web interface.")
    else:
        print(f"\nNo duplicates found at threshold {args.threshold}.")
        print("Try increasing the threshold with --threshold N")


if __name__ == '__main__':
    main()
