from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from datetime import datetime

class CronJobBase(BaseModel):
    schedule: str
    task_description: str

class CronJobCreate(CronJobBase):
    pass

class CronJobResponse(CronJobBase):
    id: int
    agent_id: int
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True

class AgentBase(BaseModel):
    name: str
    system_prompt: str

class AgentCreate(AgentBase):
    owner_id: int
    parent_agent_id: Optional[int] = None

class AgentSettingsUpdate(BaseModel):
    settings: Dict[str, Any]

class AgentResponse(AgentBase):
    id: int
    owner_id: int
    parent_agent_id: Optional[int] = None
    settings: Optional[Dict[str, Any]] = None

    class Config:
        from_attributes = True

class UserBase(BaseModel):
    username: str

class UserCreate(BaseModel):
    username: str
    email: Optional[str] = None
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    username: Optional[str] = None

class UserResponse(UserBase):
    id: int
    agents: Optional[List[AgentResponse]] = None

class ChatRequest(BaseModel):
    message: str

class ChatResponse(BaseModel):
    response: str
    tool_calls: Optional[List[dict]] = None
