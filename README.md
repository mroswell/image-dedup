# Image Deduplicator

A web-based tool for finding and managing duplicate images using perceptual hashing. Built with Python/Flask, it displays clusters of similar images and lets you mark duplicates, manage Finder tags, and track your progress.

## Features

- **Perceptual hash matching** — Finds visually similar images even if resized, recompressed, or slightly edited
- **Cluster view** — Groups similar images together for easy comparison
- **Lightbox navigation** — Click any thumbnail to view full-size with keyboard navigation
- **DUP\_ marking** — Rename duplicates with a prefix (files stay in place, excluded from future scans)
- **Finder tag support** — View, add, delete, and merge macOS tags (requires APFS/HFS+ filesystem)
- **Progress tracking** — Mark clusters as complete; auto-completes when only one unmarked file remains
- **Export** — Download CSV or TXT reports of all duplicate groups

## Requirements

- Python 3.8+
- macOS (for Finder tag support) or Linux/Windows (tags disabled)
- APFS or HFS+ filesystem recommended for tag features

## Installation

```bash
# Clone or unzip the files
cd image-dedup

# Create virtual environment (recommended)
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install flask pillow imagehash

# Optional: Install for Finder tag support (macOS only)
pip install osxmetadata
```

## Usage

### Step 1: Scan your images

```bash
python scanner.py "/path/to/your/images" --threshold 7
```

**Options:**
| Flag | Description |
|------|-------------|
| `--threshold N` | Similarity threshold (default: 5). Lower = stricter matching |
| `--rescan` | Force rescan all files, even unchanged ones |
| `--find-only` | Find duplicates without rescanning (uses existing hashes) |

**Threshold guide:**
| Value | Matches |
|-------|---------|
| 0 | Exact duplicates only |
| 1–5 | Very similar (resizes, recompression) |
| 6–10 | Somewhat similar (minor edits, crops) |
| 10+ | Loose matching (may include false positives) |

### Step 2: Start the web interface

```bash
python app.py
```

Open http://127.0.0.1:5000 in your browser.

**Options:**
| Flag | Description |
|------|-------------|
| `--port N` | Use a different port (default: 5000) |
| `--enable-move` | Enable the move-to-review feature (disabled by default) |

## Interface Guide

### Main View

- **Cluster status bar** — Row of small rectangles at top of page, one per cluster:
  - 🟠 **Orange** — Incomplete cluster
  - 🟢 **Green** — Complete cluster
  - Click any rectangle to scroll to that cluster
- **Clusters** are displayed as groups of thumbnail images
- **Badge colors** indicate cluster status:
  - 🟠 **Orange** — Incomplete, multiple unmarked files
  - 🟢 **Green with ✓** — Manually marked as done (2+ files remain unmarked)
  - 🟢 **Green** — Auto-complete (only 1 unmarked file remains)
- **Image status badges** show individual file status:
  - 🔴 **DUP** — File marked as duplicate
  - ⚫ **SKIP** — File marked as skipped
  - 🟢 **✓** — Keeper file (unmarked in a completed cluster)
- **Tags** appear as colored badges below each thumbnail
- **Merge Tags** button appears when files in a cluster have different tags

### Lightbox View

Click any thumbnail to open the lightbox:

- Large preview of the current image
- **Image status bar** — Row of small rectangles showing status of each image in cluster:
  - ⚫ **Grey** — Pending (not yet handled)
  - 🔴 **Red** — Marked as duplicate
  - ⚫ **Dark Grey** — Marked as skipped
  - 🟢 **Green** — Keeper (last remaining or cluster marked done)
  - Click any rectangle to jump to that image
- File details (name, dimensions, size, path)
- Tags with delete option (click × on any tag)
- Navigation between images in the cluster

## Keyboard Shortcuts

*In lightbox view:*

| Key | Action |
|-----|--------|
| `←` `→` | Navigate to previous/next image in cluster |
| `↑` `↓` | Navigate to previous/next cluster |
| `D` | Mark current image as duplicate (adds DUP\_ prefix) |
| `S` | Mark current image as skipped (adds SKIP\_ prefix) |
| `U` | Unmark file (removes DUP\_ or SKIP\_ prefix) |
| `M` | Merge tags from all images in cluster |
| `C` | Toggle cluster as complete/done |
| `T` | Open "Add tag" popup |
| `Enter` | Submit tag (when popup is open) |
| `Esc` | Close popup or lightbox |

## File Naming

When you mark a file as duplicate:

- `photo.jpg` becomes `DUP_photo.jpg`
- The file stays in its original location
- Future scans automatically skip files starting with `DUP_`

When you mark a file as skipped:

- `photo.jpg` becomes `SKIP_photo.jpg`
- Use this for images you want to keep but exclude from the duplicate group
- Future scans automatically skip files starting with `SKIP_`

Other prefixes also skipped by scanner:
- `ERR_` — For files with errors (you can manually rename problem files)

**Directories automatically excluded:**
- `venv`, `.venv`, `env`, `.env` — Python virtual environments
- `__pycache__`, `.pytest_cache` — Python cache
- `node_modules` — Node.js dependencies
- `site-packages`, `dist-packages` — Python packages
- `.git`, `.svn`, `.hg` — Version control
- `thumbnails` — Scanner's own thumbnails

## Tag Management

*Requires macOS with osxmetadata installed*

| Action | How |
|--------|-----|
| **View tags** | Shown as badges on thumbnails and in lightbox |
| **Add tag** | Press `T` to open popup, type tag name, press Enter |
| **Delete tag** | Hover over tag in lightbox, click × |
| **Merge tags** | Click "Merge Tags" button — combines all tags from cluster and applies to every file |

Tags are cached in the database during scanning for fast display. Changes made in the app update both the file and the cache.

## Export Options

Click the export buttons in the header:

- **Progress** — Hover to see stats, click to download timestamped report
- **CSV** — Spreadsheet with: Group ID, Hash, File Path, Filename, Size, Dimensions, Distance, Tags
- **TXT** — Human-readable report with file details

### Progress Report

Hover over the **📈 Progress** button to see:
- Total Images and Groups
- Threshold used for matching
- Processed Images count and percentage
- Processed Groups count and percentage

Click the button to download a timestamped `progress-report-YYYY-MM-DD.txt` file.

Exports reflect current filenames (including any DUP\_ renames you've made).

## Files Created

| File/Folder | Purpose |
|-------------|---------|
| `image_hashes.db` | SQLite database with image hashes, metadata, tags, and duplicate groups |
| `thumbnails/` | Generated thumbnail images for the web interface |
| `review_candidates/` | Destination for moved files (if `--enable-move` is used) |

## Rescanning

You can rescan anytime:

- **Incremental** (default): Only processes new or modified files
- **Full rescan**: Use `--rescan` to reprocess everything

```bash
# Quick update — only new/changed files
python scanner.py "/path/to/images" --threshold 7

# Full rescan — reprocess everything
python scanner.py "/path/to/images" --threshold 7 --rescan

# Just rebuild groups with a different threshold
python scanner.py "/path/to/images" --threshold 5 --find-only
```

## Tips

1. **Start with threshold 5–7** for most photo collections
2. **Use `--find-only`** to quickly test different thresholds without rescanning
3. **Work through clusters systematically** — the "Done" feature helps track progress
4. **Merge tags first** before marking duplicates, so the keeper has all tags
5. **Export CSV periodically** as a backup of your decisions

## Troubleshooting

**"Tags not supported" message**
```bash
pip install osxmetadata
```
Then restart the app.

**Tags not showing**
Run a rescan to populate the tags column:
```bash
python scanner.py "/path/to/images" --threshold 7 --rescan
```

**Slow scanning**
- First scan is slowest (computing hashes + generating thumbnails)
- Subsequent scans skip unchanged files
- Tag reading adds minimal overhead

**Database locked**
Make sure only one instance of the app is running.

**Files showing as "missing"**
The file was moved or deleted outside the app. Rescan to update the database.

## License

MIT License — Use freely, modify as needed.
