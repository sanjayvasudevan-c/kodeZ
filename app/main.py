from fastapi import FastAPI

from app.api import auth, devices, groups, invitations, messages, repositories, users

app = FastAPI(title="KodeZ")

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(repositories.router)
app.include_router(groups.router)
app.include_router(invitations.router)
app.include_router(messages.router)
app.include_router(devices.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
