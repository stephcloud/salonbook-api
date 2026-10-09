from typing import Annotated

from pydantic import StringConstraints

# https only (browsers block http images on an https site), no whitespace, max 500 chars.
ImageUrl = Annotated[
    str, StringConstraints(max_length=500, pattern=r"^https://[^\s]+$")
]
