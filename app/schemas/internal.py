from typing import Literal

from pydantic import BaseModel


class AcceptedResponse(BaseModel):
    status: Literal["accepted"] = "accepted"
