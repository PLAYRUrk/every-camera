#!/usr/bin/env python3
"""
Every Camera — Self-updater.

Downloads the latest version from the git remote and updates script files,
preserving local configuration. Works even on old versions where this
utility did not exist yet.

Usage:
    python update.py              # update to latest
    python update.py --dry-run    # show what would change, don't apply
    python update.py --force      # overwrite even if local files are modified
"""
import argparse
import os
import subprocess
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Files that should NEVER be overwritten by the updater
CONFIG_FILES = {
    "config.json",
    ".gitignore",
    ".gitmodules",
    ".gitattributes",
}

# Patterns for config/user-data files that are generated locally
CONFIG_PATTERNS = (
    "camcfg_",       # per-camera config (camcfg_Canon_EOS_6D.ini, ...)
    "schedule",      # schedule.txt, dSchedule.txt, ndSchedule.txt, ...
)

# Only update files with these extensions
UPDATEABLE_EXTENSIONS = {
    ".py",
    ".md",    # README.md
}

# Specific non-.py files that should be updated
UPDATEABLE_FILES = {
    "requirements.txt",
}

# Directories to sync (relative to repo root)
UPDATEABLE_DIRS = {
    "firmware",
}


def is_config_file(filename):
    """Check if a file is a config file that should be preserved."""
    if filename in CONFIG_FILES:
        return True
    name_lower = filename.lower()
    for pattern in CONFIG_PATTERNS:
        if pattern in name_lower:
            return True
    return False


def is_updateable(filepath):
    """Check if a file should be updated."""
    filename = os.path.basename(filepath)
    if is_config_file(filename):
        return False

    # Files in updateable directories (e.g. firmware/)
    parts = filepath.replace("\\", "/").split("/")
    if len(parts) > 1 and parts[0] in UPDATEABLE_DIRS:
        return True

    # Specific named files
    if filename in UPDATEABLE_FILES:
        return True

    # Root-level files with updateable extensions
    if len(parts) == 1:
        _, ext = os.path.splitext(filename)
        return ext in UPDATEABLE_EXTENSIONS

    return False


def run_git(*args, capture=True):
    """Run a git command and return stdout."""
    cmd = ["git", "-C", APP_DIR] + list(args)
    result = subprocess.run(
        cmd, capture_output=capture, text=True,
        timeout=60,
    )
    if result.returncode != 0:
        error = result.stderr.strip() if capture else ""
        raise RuntimeError(f"git {' '.join(args)} failed: {error}")
    return result.stdout.strip() if capture else ""


def get_remote_branch():
    """Detect the remote tracking branch."""
    try:
        ref = run_git("symbolic-ref", "--short", "HEAD")
    except RuntimeError:
        ref = "main"
    remote = "origin"
    return remote, ref


def fetch_latest(remote):
    """Fetch latest from remote."""
    print(f"[INFO] Fetching from {remote}...")
    run_git("fetch", remote, capture=False)


def get_changed_files(remote, branch):
    """Get list of files changed between local HEAD and remote."""
    remote_ref = f"{remote}/{branch}"

    # Files that differ between local and remote
    diff_output = run_git("diff", "--name-status", "HEAD", remote_ref)
    if not diff_output:
        return []

    changes = []
    for line in diff_output.splitlines():
        parts = line.split("\t", 1)
        if len(parts) < 2:
            continue
        status, filepath = parts[0], parts[1]
        changes.append((status, filepath))
    return changes


def get_local_modifications():
    """Get set of locally modified files (uncommitted changes)."""
    try:
        output = run_git("status", "--porcelain", "--ignore-submodules=all")
    except RuntimeError:
        # Broken submodule references can cause git status to fail;
        # treat as no local modifications so the update can proceed.
        print("[WARN] git status failed (broken submodule?), assuming no local changes.")
        return set()
    modified = set()
    for line in output.splitlines():
        if len(line) < 4:
            continue
        # status is first 2 chars, then space, then filename
        filepath = line[3:].strip()
        if filepath:
            modified.add(filepath)
    return modified


def checkout_file(remote, branch, filepath):
    """Checkout a single file from remote."""
    remote_ref = f"{remote}/{branch}"
    run_git("checkout", remote_ref, "--", filepath)


def remove_file(filepath):
    """Remove a file from working tree and git index."""
    full_path = os.path.join(APP_DIR, filepath)
    if os.path.exists(full_path):
        os.remove(full_path)
        print(f"  Deleted: {filepath}")


def update(dry_run=False, force=False):
    """Main update logic."""
    # Verify this is a git repo
    if not os.path.isdir(os.path.join(APP_DIR, ".git")):
        print("[ERROR] Not a git repository. Cannot update.")
        return False

    remote, branch = get_remote_branch()

    try:
        fetch_latest(remote)
    except RuntimeError as e:
        print(f"[ERROR] Failed to fetch: {e}")
        return False

    # Get changes
    try:
        changes = get_changed_files(remote, branch)
    except RuntimeError as e:
        print(f"[ERROR] Failed to diff: {e}")
        return False

    if not changes:
        print("[INFO] Already up to date.")
        return True

    local_modified = get_local_modifications() if not force else set()

    # Filter to updateable files
    to_update = []
    to_add = []
    to_delete = []
    skipped_config = []
    skipped_modified = []

    for status, filepath in changes:
        if not is_updateable(filepath):
            if not is_config_file(os.path.basename(filepath)):
                # Not updateable and not config — just skip silently
                pass
            else:
                skipped_config.append(filepath)
            continue

        if status.startswith("D"):
            to_delete.append(filepath)
        elif status.startswith("A"):
            to_add.append(filepath)
        else:
            # Modified or renamed
            if filepath in local_modified and not force:
                skipped_modified.append(filepath)
            else:
                to_update.append(filepath)

    # Report
    total = len(to_update) + len(to_add) + len(to_delete)
    if total == 0 and not skipped_modified:
        print("[INFO] No updateable files changed.")
        if skipped_config:
            print(f"[INFO] Skipped config files: {', '.join(skipped_config)}")
        return True

    print(f"\n{'=' * 50}")
    print(f"  Available updates: {total} file(s)")
    print(f"{'=' * 50}")

    if to_add:
        print(f"\n  New files ({len(to_add)}):")
        for f in to_add:
            print(f"    + {f}")

    if to_update:
        print(f"\n  Updated files ({len(to_update)}):")
        for f in to_update:
            print(f"    ~ {f}")

    if to_delete:
        print(f"\n  Removed files ({len(to_delete)}):")
        for f in to_delete:
            print(f"    - {f}")

    if skipped_config:
        print(f"\n  Preserved config ({len(skipped_config)}):")
        for f in skipped_config:
            print(f"    * {f}")

    if skipped_modified:
        print(f"\n  Skipped (local changes, use --force) ({len(skipped_modified)}):")
        for f in skipped_modified:
            print(f"    ! {f}")

    print()

    if dry_run:
        print("[DRY RUN] No changes applied.")
        return True

    # Apply updates
    errors = 0
    for filepath in to_add + to_update:
        try:
            # Ensure parent directory exists
            full_path = os.path.join(APP_DIR, filepath)
            parent = os.path.dirname(full_path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)

            checkout_file(remote, branch, filepath)
            print(f"  Updated: {filepath}")
        except RuntimeError as e:
            print(f"  [ERROR] {filepath}: {e}")
            errors += 1

    for filepath in to_delete:
        try:
            remove_file(filepath)
        except Exception as e:
            print(f"  [ERROR] {filepath}: {e}")
            errors += 1

    # Update HEAD to match remote for the files we updated
    # (don't do a full merge/rebase — just advance HEAD)
    if errors == 0 and not skipped_modified:
        try:
            remote_ref = f"{remote}/{branch}"
            run_git("reset", "--soft", remote_ref)
            print(f"\n[OK] Updated to latest version.")
        except RuntimeError:
            print(f"\n[OK] Files updated. Run 'git pull' to sync HEAD.")
    else:
        print(f"\n[OK] {total - errors}/{total} files updated.")
        if errors:
            print(f"[WARN] {errors} error(s) occurred.")
        if skipped_modified:
            print("[INFO] Some files skipped due to local changes. Use --force to overwrite.")

    return errors == 0


def main():
    parser = argparse.ArgumentParser(
        description="Every Camera — Self-updater",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be updated without applying changes",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite locally modified files",
    )
    args = parser.parse_args()

    print("Every Camera — Update Utility")
    print()

    ok = update(dry_run=args.dry_run, force=args.force)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
