from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator


class CameraOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:str; provider_id:str; name:str; type:str; model:str; online:bool; status:str="online"
    battery_level:int|None=None; capabilities:dict[str,str]={}


class EventOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:str; camera_id:str; type:str; priority:str; source:str; start_time:datetime; description:str
    event_metadata:dict={}


class MockEventIn(BaseModel):
    camera_id:str="mock-front-door"
    type:str="person"


class CameraStatusIn(BaseModel):
    status:str = Field(pattern="^(online|offline|degraded|unknown)$")


class CameraBatteryIn(BaseModel):
    battery_level:int = Field(ge=0, le=100)


class ProviderOutageIn(BaseModel):
    unavailable:bool = True


class RegisterIn(BaseModel):
    email:str
    password:str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def email_must_look_valid(cls, value: str) -> str:
        if "@" not in value or "." not in value.split("@")[-1]:
            raise ValueError("email must be a valid address")
        return value.lower().strip()


class LoginIn(BaseModel):
    email:str
    password:str


class UserOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:str; email:str; created_at:datetime


class TokenOut(BaseModel):
    access_token:str
    token_type:str = "bearer"
    expires_at:datetime
    user:UserOut

