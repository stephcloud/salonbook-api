import uuid
from datetime import date, datetime, time
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.availability_rule import RuleKind

Weekday = Annotated[int, Field(ge=0, le=6)]


class AvailabilityRuleInput(BaseModel):
    """One rule. working/break need weekday + times; day_off needs only a date."""

    model_config = ConfigDict(extra="forbid")

    kind: RuleKind
    weekday: Weekday | None = None
    start_time: time | None = None
    end_time: time | None = None
    off_date: date | None = None

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> Self:
        if self.kind == RuleKind.DAY_OFF:
            if self.off_date is None:
                raise ValueError("off_date is required when kind is day_off")
            if (
                self.weekday is not None
                or self.start_time is not None
                or self.end_time is not None
            ):
                raise ValueError("day_off takes only off_date")
            return self
        if self.off_date is not None:
            raise ValueError("off_date is only allowed when kind is day_off")
        if self.weekday is None or self.start_time is None or self.end_time is None:
            raise ValueError("weekday, start_time and end_time are required")
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class AvailabilityUpdate(BaseModel):
    """Full replacement of a stylist's rules (PUT)."""

    model_config = ConfigDict(extra="forbid")

    rules: list[AvailabilityRuleInput] = Field(max_length=200)


class AvailabilityRuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: RuleKind
    weekday: int | None
    start_time: time | None
    end_time: time | None
    off_date: date | None


class AvailabilityResponse(BaseModel):
    stylist_id: uuid.UUID
    rules: list[AvailabilityRuleResponse]


class SlotsResponse(BaseModel):
    """Bookable start times (salon-local, with UTC offset), earliest first."""

    stylist_id: uuid.UUID
    service_id: uuid.UUID
    slots: list[datetime]
