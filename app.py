#!/usr/bin/env python3
"""
Image Deduplicator Web UI - Flask application for reviewing duplicate images.
Supports Finder tags, DUP_ renaming, and tag management on APFS drives.
"""

import os
import sys
import sqlite3
import shutil
import csv
import io
import json
import argparse
from pathlib import Path
from datetime import datetime

try:
    from flask import Flask, render_template, jsonify, request, send_file, abort, Response
    from flask.json.provider import DefaultJSONProvider
except ImportError:
    print("Error: Flask not installed.")
    print("Run: pip install flask")
    sys.exit(1)

# Optional: osxmetadata for tag management
try:
    import osxmetadata
    HAS_OSXMETADATA = True
except ImportError:
    HAS_OSXMETADATA = False


class SafeJSONProvider(DefaultJSONProvider):
    @staticmethod
    def default(o):
        if isinstance(o, bytes):
            return o.decode('utf-8', errors='replace')
        return DefaultJSONProvider.default(o)


app = Flask(__name__)
app.json = SafeJSONProvider(app)

SCRIPT_DIR = Path(__file__).parent
DB_PATH = SCRIPT_DIR / "image_hashes.db"
THUMBNAIL_DIR = SCRIPT_DIR / "thumbnails"
REVIEW_DIR = SCRIPT_DIR / "review_candidates"
MOVE_ENABLED = False


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.text_factory = str
    
    # Ensure completed_at column exists
    cursor = conn.cursor()
    try:
        cursor.execute('ALTER TABLE duplicate_groups ADD COLUMN completed_at TIMESTAMP')
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists
    
    return conn


def safe_str(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return str(value)


def parse_tags(tags_json):
    """Parse tags JSON from database."""
    if not tags_json:
        return []
    try:
        return json.loads(tags_json)
    except:
        return []


def format_size(size_bytes):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


@app.route('/')
def index():
    return render_template('index.html', move_enabled=MOVE_ENABLED)


@app.route('/api/config')
def get_config():
    return jsonify({
        'move_enabled': MOVE_ENABLED,
        'tags_supported': HAS_OSXMETADATA
    })


@app.route('/api/stats')
def get_stats():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM images')
    total_images = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM duplicate_groups')
    total_groups = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM group_members')
    total_in_groups = cursor.fetchone()[0]
    cursor.execute('SELECT threshold FROM duplicate_groups LIMIT 1')
    row = cursor.fetchone()
    threshold = row[0] if row else 5
    conn.close()
    return jsonify({
        'total_images': total_images,
        'total_groups': total_groups,
        'total_in_groups': total_in_groups,
        'threshold': threshold
    })


@app.route('/api/progress')
def get_progress():
    """Get progress statistics for duplicate processing."""
    conn = get_db()
    cursor = conn.cursor()
    
    # Get total images in duplicate groups
    cursor.execute('SELECT COUNT(DISTINCT image_id) FROM group_members')
    total_images = cursor.fetchone()[0] or 0
    
    # Get total groups
    cursor.execute('SELECT COUNT(*) FROM duplicate_groups')
    total_groups = cursor.fetchone()[0] or 0
    
    # Get threshold (from most recent group)
    cursor.execute('SELECT threshold FROM duplicate_groups ORDER BY id DESC LIMIT 1')
    row = cursor.fetchone()
    threshold = row['threshold'] if row else 0
    
    # Get processed groups (completed OR only 1 unmarked file remaining)
    processed_groups = 0
    images_in_processed_groups = 0
    cursor.execute('SELECT id, completed_at FROM duplicate_groups')
    groups = cursor.fetchall()
    
    for group in groups:
        # Check if only 1 unmarked file remains (auto-complete) or manually completed
        # Count files not starting with DUP_ or SKIP_
        cursor.execute('''
            SELECT COUNT(*) FROM group_members gm
            JOIN images i ON gm.image_id = i.id
            WHERE gm.group_id = ? AND i.filename NOT LIKE 'DUP_%' AND i.filename NOT LIKE 'SKIP_%'
        ''', (group['id'],))
        unmarked = cursor.fetchone()[0]
        
        if group['completed_at'] or unmarked <= 1:
            processed_groups += 1
            # Count images in this processed group
            cursor.execute('SELECT COUNT(*) FROM group_members WHERE group_id = ?', (group['id'],))
            images_in_processed_groups += cursor.fetchone()[0]
    
    conn.close()
    
    # Processed images = images in processed groups
    processed_images = images_in_processed_groups
    
    # Calculate percentages
    proc_images_pct = round(processed_images / total_images * 100, 1) if total_images > 0 else 0
    proc_groups_pct = round(processed_groups / total_groups * 100, 1) if total_groups > 0 else 0
    
    return jsonify({
        'total_images': total_images,
        'total_groups': total_groups,
        'threshold': threshold,
        'processed_images': processed_images,
        'processed_images_pct': proc_images_pct,
        'processed_groups': processed_groups,
        'processed_groups_pct': proc_groups_pct
    })


@app.route('/api/groups')
def get_groups():
    conn = get_db()
    cursor = conn.cursor()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 25, type=int)
    offset = (page - 1) * per_page
    cursor.execute('SELECT COUNT(*) FROM duplicate_groups')
    total_groups = cursor.fetchone()[0]
    cursor.execute('''
        SELECT id, group_hash, threshold, created_at, completed_at
        FROM duplicate_groups ORDER BY id LIMIT ? OFFSET ?
    ''', (per_page, offset))
    groups = cursor.fetchall()
    result = []
    for group in groups:
        cursor.execute('''
            SELECT i.id, i.filepath, i.filename, i.filesize, i.width, i.height,
                   i.thumbnail_path, i.tags, gm.distance
            FROM images i
            JOIN group_members gm ON i.id = gm.image_id
            WHERE gm.group_id = ?
            ORDER BY gm.distance, i.filesize DESC
        ''', (group['id'],))
        images = cursor.fetchall()
        
        # Count unmarked files (not DUP_ or SKIP_)
        unmarked_count = 0
        image_list = []
        for img in images:
            filepath = safe_str(img['filepath'])
            filename = safe_str(img['filename'])
            exists = os.path.exists(filepath)
            tags = parse_tags(img['tags'])
            is_marked = filename.startswith('DUP_') or filename.startswith('SKIP_')
            if not is_marked:
                unmarked_count += 1
            image_list.append({
                'id': img['id'],
                'filepath': filepath,
                'filename': filename,
                'filesize': img['filesize'],
                'filesize_formatted': format_size(img['filesize']) if img['filesize'] else 'Unknown',
                'width': img['width'],
                'height': img['height'],
                'dimensions': f"{img['width']}x{img['height']}" if img['width'] and img['height'] else 'Unknown',
                'distance': img['distance'],
                'exists': exists,
                'tags': tags
            })
        
        # Auto-complete if only 1 unmarked remaining
        is_complete = group['completed_at'] is not None or unmarked_count <= 1
        
        group_data = {
            'id': group['id'],
            'hash': safe_str(group['group_hash']),
            'count': len(images),
            'unmarked_count': unmarked_count,
            'completed': is_complete,
            'completed_at': group['completed_at'],
            'images': image_list
        }
        result.append(group_data)
    conn.close()
    return jsonify({
        'groups': result,
        'page': page,
        'per_page': per_page,
        'total_groups': total_groups,
        'total_pages': (total_groups + per_page - 1) // per_page
    })


@app.route('/api/thumbnail/<int:image_id>')
def get_thumbnail(image_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT thumbnail_path FROM images WHERE id = ?', (image_id,))
    row = cursor.fetchone()
    conn.close()
    if not row or not row['thumbnail_path']:
        abort(404)
    thumb_path = Path(safe_str(row['thumbnail_path']))
    if not thumb_path.exists():
        abort(404)
    return send_file(thumb_path, mimetype='image/jpeg')


@app.route('/api/image/<int:image_id>')
def get_full_image(image_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT filepath FROM images WHERE id = ?', (image_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        abort(404)
    filepath = Path(safe_str(row['filepath']))
    if not filepath.exists():
        abort(404)
    ext = filepath.suffix.lower()
    mimetypes = {
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
        '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
        '.tiff': 'image/tiff', '.tif': 'image/tiff'
    }
    mimetype = mimetypes.get(ext, 'application/octet-stream')
    return send_file(filepath, mimetype=mimetype)


@app.route('/api/rename-dup/<int:image_id>', methods=['POST'])
def rename_as_duplicate(image_id):
    """Rename a file to prepend DUP_ to its name."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT filepath, filename FROM images WHERE id = ?', (image_id,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        return jsonify({'error': 'Image not found'}), 404
    
    filepath = Path(safe_str(row['filepath']))
    filename = safe_str(row['filename'])
    
    if not filepath.exists():
        conn.close()
        return jsonify({'error': 'File not found on disk'}), 404
    
    # Check if already renamed
    if filename.startswith('DUP_') or filename.startswith('SKIP_'):
        conn.close()
        return jsonify({'error': 'File is already marked'}), 400
    
    # Create new path with DUP_ prefix
    new_filename = f"DUP_{filename}"
    new_filepath = filepath.parent / new_filename
    
    # Check if target exists
    if new_filepath.exists():
        conn.close()
        return jsonify({'error': f'Target file already exists: {new_filename}'}), 400
    
    try:
        # Rename the file
        filepath.rename(new_filepath)
        
        # Update database
        cursor.execute('''
            UPDATE images SET filepath = ?, filename = ? WHERE id = ?
        ''', (str(new_filepath), new_filename, image_id))
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'old_name': filename,
            'new_name': new_filename,
            'new_path': str(new_filepath)
        })
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500


@app.route('/api/rename-skip/<int:image_id>', methods=['POST'])
def rename_as_skip(image_id):
    """Rename a file to prepend SKIP_ to its name."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT filepath, filename FROM images WHERE id = ?', (image_id,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        return jsonify({'error': 'Image not found'}), 404
    
    filepath = Path(safe_str(row['filepath']))
    filename = safe_str(row['filename'])
    
    if not filepath.exists():
        conn.close()
        return jsonify({'error': 'File not found on disk'}), 404
    
    # Check if already renamed
    if filename.startswith('DUP_') or filename.startswith('SKIP_'):
        conn.close()
        return jsonify({'error': 'File is already marked'}), 400
    
    # Create new path with SKIP_ prefix
    new_filename = f"SKIP_{filename}"
    new_filepath = filepath.parent / new_filename
    
    # Check if target exists
    if new_filepath.exists():
        conn.close()
        return jsonify({'error': f'Target file already exists: {new_filename}'}), 400
    
    try:
        # Rename the file
        filepath.rename(new_filepath)
        
        # Update database
        cursor.execute('''
            UPDATE images SET filepath = ?, filename = ? WHERE id = ?
        ''', (str(new_filepath), new_filename, image_id))
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'old_name': filename,
            'new_name': new_filename,
            'new_path': str(new_filepath)
        })
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500


@app.route('/api/unmark/<int:image_id>', methods=['POST'])
def unmark_file(image_id):
    """Remove DUP_ or SKIP_ prefix from a file's name."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT filepath, filename FROM images WHERE id = ?', (image_id,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        return jsonify({'error': 'Image not found'}), 404
    
    filepath = Path(safe_str(row['filepath']))
    filename = safe_str(row['filename'])
    
    if not filepath.exists():
        conn.close()
        return jsonify({'error': 'File not found on disk'}), 404
    
    # Check which prefix it has
    if filename.startswith('DUP_'):
        new_filename = filename[4:]  # Remove 'DUP_'
    elif filename.startswith('SKIP_'):
        new_filename = filename[5:]  # Remove 'SKIP_'
    else:
        conn.close()
        return jsonify({'error': 'File is not marked'}), 400
    
    new_filepath = filepath.parent / new_filename
    
    # Check if target exists
    if new_filepath.exists():
        conn.close()
        return jsonify({'error': f'Target file already exists: {new_filename}'}), 400
    
    try:
        # Rename the file
        filepath.rename(new_filepath)
        
        # Update database
        cursor.execute('''
            UPDATE images SET filepath = ?, filename = ? WHERE id = ?
        ''', (str(new_filepath), new_filename, image_id))
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'old_name': filename,
            'new_name': new_filename,
            'new_path': str(new_filepath)
        })
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500


@app.route('/api/unmark-dup/<int:image_id>', methods=['POST'])
def unmark_duplicate(image_id):
    """Remove DUP_ prefix from a file's name. (Legacy endpoint, use /api/unmark instead)"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT filepath, filename FROM images WHERE id = ?', (image_id,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        return jsonify({'error': 'Image not found'}), 404
    
    filepath = Path(safe_str(row['filepath']))
    filename = safe_str(row['filename'])
    
    if not filepath.exists():
        conn.close()
        return jsonify({'error': 'File not found on disk'}), 404
    
    # Check if it has DUP_ prefix
    if not filename.startswith('DUP_'):
        conn.close()
        return jsonify({'error': 'File is not marked as duplicate'}), 400
    
    # Remove DUP_ prefix
    new_filename = filename[4:]  # Remove 'DUP_'
    new_filepath = filepath.parent / new_filename
    
    # Check if target exists
    if new_filepath.exists():
        conn.close()
        return jsonify({'error': f'Target file already exists: {new_filename}'}), 400
    
    try:
        # Rename the file
        filepath.rename(new_filepath)
        
        # Update database
        cursor.execute('''
            UPDATE images SET filepath = ?, filename = ? WHERE id = ?
        ''', (str(new_filepath), new_filename, image_id))
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'old_name': filename,
            'new_name': new_filename,
            'new_path': str(new_filepath)
        })
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500


@app.route('/api/tags/<int:image_id>')
def get_image_tags(image_id):
    """Get current tags for an image (reads from file, not cache)."""
    if not HAS_OSXMETADATA:
        return jsonify({'error': 'osxmetadata not installed'}), 501
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT filepath FROM images WHERE id = ?', (image_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return jsonify({'error': 'Image not found'}), 404
    
    filepath = safe_str(row['filepath'])
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404
    
    try:
        md = osxmetadata.OSXMetaData(filepath)
        tags = [str(tag.name) if hasattr(tag, 'name') else str(tag) for tag in md.tags]
        return jsonify({'tags': tags})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tags/<int:image_id>/delete', methods=['POST'])
def delete_tag(image_id):
    """Delete a specific tag from an image."""
    if not HAS_OSXMETADATA:
        return jsonify({'error': 'osxmetadata not installed'}), 501
    
    data = request.get_json()
    tag_to_delete = data.get('tag')
    
    if not tag_to_delete:
        return jsonify({'error': 'No tag specified'}), 400
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT filepath FROM images WHERE id = ?', (image_id,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        return jsonify({'error': 'Image not found'}), 404
    
    filepath = safe_str(row['filepath'])
    if not os.path.exists(filepath):
        conn.close()
        return jsonify({'error': 'File not found'}), 404
    
    try:
        md = osxmetadata.OSXMetaData(filepath)
        current_tags = list(md.tags)
        
        # Find and remove the tag
        new_tags = [t for t in current_tags if (t.name if hasattr(t, 'name') else str(t)) != tag_to_delete]
        
        if len(new_tags) == len(current_tags):
            conn.close()
            return jsonify({'error': 'Tag not found on file'}), 404
        
        # Clear and set new tags
        md.tags = new_tags
        
        # Update database cache
        tag_names = [str(t.name) if hasattr(t, 'name') else str(t) for t in new_tags]
        cursor.execute('UPDATE images SET tags = ? WHERE id = ?', (json.dumps(tag_names), image_id))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'remaining_tags': tag_names})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500


@app.route('/api/tags/merge', methods=['POST'])
def merge_tags():
    """Merge tags from multiple images and apply to all of them."""
    if not HAS_OSXMETADATA:
        return jsonify({'error': 'osxmetadata not installed'}), 501
    
    data = request.get_json()
    image_ids = data.get('image_ids', [])
    
    if len(image_ids) < 2:
        return jsonify({'error': 'Need at least 2 images to merge tags'}), 400
    
    conn = get_db()
    cursor = conn.cursor()
    
    # Collect all unique tags from all images (preserving color info)
    all_tags = {}  # name -> Tag object
    filepaths = []
    
    for img_id in image_ids:
        cursor.execute('SELECT filepath FROM images WHERE id = ?', (img_id,))
        row = cursor.fetchone()
        if not row:
            continue
        filepath = safe_str(row['filepath'])
        if not os.path.exists(filepath):
            continue
        filepaths.append((img_id, filepath))
        
        try:
            md = osxmetadata.OSXMetaData(filepath)
            for tag in md.tags:
                tag_name = tag.name if hasattr(tag, 'name') else str(tag)
                if tag_name not in all_tags:
                    all_tags[tag_name] = tag  # Keep the original Tag object with color
        except:
            pass
    
    if not filepaths:
        conn.close()
        return jsonify({'error': 'No valid files found'}), 404
    
    # Apply merged tags to all files
    merged_tag_names = sorted(list(all_tags.keys()))
    merged_tag_objects = [all_tags[name] for name in merged_tag_names]
    errors = []
    
    for img_id, filepath in filepaths:
        try:
            md = osxmetadata.OSXMetaData(filepath)
            md.tags = merged_tag_objects
            
            # Update database cache
            cursor.execute('UPDATE images SET tags = ? WHERE id = ?', (json.dumps(merged_tag_names), img_id))
        except Exception as e:
            errors.append({'id': img_id, 'error': str(e)})
    
    conn.commit()
    conn.close()
    
    return jsonify({
        'success': True,
        'merged_tags': merged_tag_names,
        'files_updated': len(filepaths) - len(errors),
        'errors': errors
    })


@app.route('/api/group/<int:group_id>/toggle-complete', methods=['POST'])
def toggle_complete(group_id):
    """Toggle the completed status of a duplicate group."""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('SELECT completed_at FROM duplicate_groups WHERE id = ?', (group_id,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        return jsonify({'error': 'Group not found'}), 404
    
    if row['completed_at']:
        # Un-complete it
        cursor.execute('UPDATE duplicate_groups SET completed_at = NULL WHERE id = ?', (group_id,))
        completed = False
    else:
        # Mark complete
        cursor.execute('UPDATE duplicate_groups SET completed_at = CURRENT_TIMESTAMP WHERE id = ?', (group_id,))
        completed = True
    
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'completed': completed})


@app.route('/api/tags/<int:image_id>/add', methods=['POST'])
def add_tag(image_id):
    """Add a new tag to an image."""
    if not HAS_OSXMETADATA:
        return jsonify({'error': 'osxmetadata not installed'}), 501
    
    data = request.get_json()
    new_tag = data.get('tag', '').strip()
    
    if not new_tag:
        return jsonify({'error': 'No tag specified'}), 400
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT filepath FROM images WHERE id = ?', (image_id,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        return jsonify({'error': 'Image not found'}), 404
    
    filepath = safe_str(row['filepath'])
    if not os.path.exists(filepath):
        conn.close()
        return jsonify({'error': 'File not found'}), 404
    
    try:
        md = osxmetadata.OSXMetaData(filepath)
        current_tags = list(md.tags)
        
        # Check if tag already exists
        existing_names = [t.name if hasattr(t, 'name') else str(t) for t in current_tags]
        if new_tag in existing_names:
            conn.close()
            return jsonify({'error': 'Tag already exists on file'}), 400
        
        # Add the new tag (color 0 = no color)
        from osxmetadata import Tag
        current_tags.append(Tag(new_tag, 0))
        md.tags = current_tags
        
        # Update database cache
        tag_names = [t.name if hasattr(t, 'name') else str(t) for t in current_tags]
        cursor.execute('UPDATE images SET tags = ? WHERE id = ?', (json.dumps(tag_names), image_id))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'tags': tag_names})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500


@app.route('/api/export/csv')
def export_csv():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT dg.id as group_id, dg.group_hash, i.filepath, i.filename,
               i.filesize, i.width, i.height, i.tags, gm.distance
        FROM duplicate_groups dg
        JOIN group_members gm ON dg.id = gm.group_id
        JOIN images i ON gm.image_id = i.id
        ORDER BY dg.id, gm.distance, i.filesize DESC
    ''')
    rows = cursor.fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Group ID', 'Group Hash', 'File Path', 'Filename',
                     'File Size (bytes)', 'Width', 'Height', 'Hash Distance', 'Tags'])
    for row in rows:
        tags = parse_tags(row['tags'])
        writer.writerow([
            row['group_id'], safe_str(row['group_hash']), safe_str(row['filepath']),
            safe_str(row['filename']), row['filesize'], row['width'],
            row['height'], row['distance'], '; '.join(tags)
        ])
    output.seek(0)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"duplicate_report_{timestamp}.csv"
    return Response(output.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename={filename}'})


@app.route('/api/export/txt')
def export_txt():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM duplicate_groups')
    total_groups = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM group_members')
    total_in_groups = cursor.fetchone()[0]
    cursor.execute('SELECT threshold FROM duplicate_groups LIMIT 1')
    row = cursor.fetchone()
    threshold = row[0] if row else 'N/A'
    lines = ["=" * 70, "IMAGE DUPLICATE REPORT",
             f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "=" * 70, "",
             f"Total duplicate groups: {total_groups}",
             f"Total images in groups: {total_in_groups}",
             f"Similarity threshold used: {threshold}", "", "-" * 70]
    cursor.execute('SELECT id, group_hash FROM duplicate_groups ORDER BY id')
    groups = cursor.fetchall()
    for group in groups:
        lines.append("")
        lines.append(f"GROUP {group['id']} (Hash: {safe_str(group['group_hash'])})")
        lines.append("-" * 40)
        cursor.execute('''
            SELECT i.filepath, i.filename, i.filesize, i.width, i.height, i.tags, gm.distance
            FROM images i JOIN group_members gm ON i.id = gm.image_id
            WHERE gm.group_id = ? ORDER BY gm.distance, i.filesize DESC
        ''', (group['id'],))
        images = cursor.fetchall()
        for i, img in enumerate(images, 1):
            size_str = format_size(img['filesize']) if img['filesize'] else 'Unknown'
            dims = f"{img['width']}x{img['height']}" if img['width'] else 'Unknown'
            dist_str = "EXACT" if img['distance'] == 0 else f"distance={img['distance']}"
            tags = parse_tags(img['tags'])
            tags_str = f" [{', '.join(tags)}]" if tags else ""
            lines.append(f"  {i}. [{dist_str}] {size_str}, {dims}{tags_str}")
            lines.append(f"     {safe_str(img['filepath'])}")
        lines.append("")
    conn.close()
    output = "\n".join(lines)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"duplicate_report_{timestamp}.txt"
    return Response(output, mimetype='text/plain',
                    headers={'Content-Disposition': f'attachment; filename={filename}'})


@app.route('/api/move-enabled')
def check_move_enabled():
    return jsonify({'enabled': MOVE_ENABLED})


@app.route('/api/move', methods=['POST'])
def move_to_review():
    if not MOVE_ENABLED:
        return jsonify({'error': 'Move functionality is disabled.'}), 403
    data = request.get_json()
    image_ids = data.get('image_ids', [])
    confirmation_code = data.get('confirmation_code', '')
    if confirmation_code != 'I-UNDERSTAND-THIS-MOVES-FILES':
        return jsonify({'error': 'Invalid confirmation code'}), 400
    if not image_ids:
        return jsonify({'error': 'No images specified'}), 400
    conn = get_db()
    cursor = conn.cursor()
    REVIEW_DIR.mkdir(exist_ok=True)
    moved, errors = [], []
    for img_id in image_ids:
        cursor.execute('SELECT filepath, filename FROM images WHERE id = ?', (img_id,))
        row = cursor.fetchone()
        if not row:
            errors.append({'id': img_id, 'error': 'Not found'})
            continue
        filepath = Path(safe_str(row['filepath']))
        filename = safe_str(row['filename'])
        if not filepath.exists():
            errors.append({'id': img_id, 'error': 'File missing'})
            continue
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            review_name = f"{timestamp}_{filename}"
            review_path = REVIEW_DIR / review_name
            record_path = REVIEW_DIR / f"{timestamp}_{filename}.source.txt"
            with open(record_path, 'w') as f:
                f.write(f"Original: {filepath}\nMoved: {datetime.now().isoformat()}\nID: {img_id}\n")
            shutil.move(str(filepath), str(review_path))
            moved.append({'id': img_id, 'original': str(filepath), 'moved_to': str(review_path)})
        except Exception as e:
            errors.append({'id': img_id, 'error': str(e)})
    conn.close()
    return jsonify({'moved': moved, 'errors': errors, 'review_folder': str(REVIEW_DIR)})


@app.route('/api/open-folder/<int:image_id>')
def get_folder_path(image_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT filepath FROM images WHERE id = ?', (image_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    filepath = safe_str(row['filepath'])
    return jsonify({'folder': str(Path(filepath).parent), 'filepath': filepath})


def main():
    global MOVE_ENABLED
    parser = argparse.ArgumentParser(description='Image Deduplicator Web UI')
    parser.add_argument('--enable-move', action='store_true')
    parser.add_argument('--port', '-p', type=int, default=5000)
    args = parser.parse_args()
    MOVE_ENABLED = args.enable_move
    if not DB_PATH.exists():
        print("Error: No database. Run scanner.py first.")
        sys.exit(1)
    print("=" * 60)
    print("  Image Deduplicator - Cluster Viewer")
    print("=" * 60)
    print(f"\n  Open: http://localhost:{args.port}\n")
    print("  Features: View clusters, Export CSV/TXT, Tag management")
    if HAS_OSXMETADATA:
        print("  Tags: Supported ✓")
    else:
        print("  Tags: Not available (pip install osxmetadata)")
    if MOVE_ENABLED:
        print("  ⚠️  Move: ENABLED")
    else:
        print("  Move: disabled (--enable-move)")
    print("\n  Ctrl+C to stop\n" + "=" * 60)
    app.run(debug=False, host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
