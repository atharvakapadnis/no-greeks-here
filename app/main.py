from fastapi import FastAPI

app = FastAPI(title="no-greeks-here")

@app.get("/health")
def health():
    return {"status": "ok"}