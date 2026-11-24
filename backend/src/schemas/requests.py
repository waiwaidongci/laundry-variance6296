from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=100)


class Import(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    csv: str = Field(min_length=1, max_length=1_000_000)


class Run(BaseModel):
    dataset_id: int = Field(gt=0)
    options: dict = Field(default_factory=dict)


class Review(BaseModel):
    decision: Literal['approved', 'returned']
    note: str = Field(min_length=2, max_length=1000)


class Delivery(BaseModel):
    recipient: str = Field(min_length=2, max_length=100)
    cadence: Literal['once', 'daily']
    scheduled_at: datetime | None = None


class MeterReadingIn(BaseModel):
    reading_id: str = Field(min_length=1, max_length=60)
    shop: str = Field(min_length=1, max_length=80)
    read_at: datetime
    liters: float = Field(ge=0, le=100000)


class MeterSubmission(BaseModel):
    readings: list[MeterReadingIn] = Field(min_length=1, max_length=5000)


class WashWindowIn(BaseModel):
    shop: str = Field(min_length=1, max_length=80)
    batch_id: str = Field(min_length=1, max_length=60)
    started_at: datetime
    finished_at: datetime


class WindowSync(BaseModel):
    dataset_id: int = Field(gt=0)
    windows: list[WashWindowIn] = Field(min_length=1, max_length=5000)
