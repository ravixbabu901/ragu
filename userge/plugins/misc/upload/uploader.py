# Updated uploader.py

# This import is now handled inside the function.

def audio_upload(...):
    try:
        import stagger
    except ImportError:
        stagger = None
    # Rest of the function continues...

# Code to skip album art if stagger is missing
if stagger:
    # Proceed with album art
else:
    # Skip album art
