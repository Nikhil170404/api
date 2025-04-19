import os
from paste import app

# Use PORT environment variable from Render
port = int(os.environ.get("PORT", 8000))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("paste:app", host="0.0.0.0", port=port)
