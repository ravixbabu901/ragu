# uplaoder.py

# Lazy import for stagger
optional_imports = ['stagger']
try:
    from userge.plugins.misc.upload import stagger
except ImportError:
    stagger = None

# Thumbnail fallback

def get_thumb():
    # your existing implementation
    pass

# Rest of the uploader.py code goes here...