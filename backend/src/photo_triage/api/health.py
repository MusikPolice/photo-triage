from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class Health(BaseModel):
    status: Literal["ok"] = "ok"


@router.get("/health")
def health() -> Health:
    return Health()
