# backend.py
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

class RepoLink(BaseModel):
    url: str

@app.post("/gen-microcases/")
async def gen_microcases(link: RepoLink):
    # тут будет логика генерации, пока просто возвращаем echo
    return {"status": "ok", "received_url": link.url}
