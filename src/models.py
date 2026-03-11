from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Text, Boolean, JSON
from sqlalchemy.orm import relationship
from datetime import datetime
from pgvector.sqlalchemy import Vector
from src.database import Base

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    email = Column(String, unique=True, index=True, nullable=True)
    hashed_password = Column(String, nullable=True)
    agents = relationship("Agent", back_populates="owner")
    messages = relationship("Message", back_populates="owner")

class Agent(Base):
    __tablename__ = "agents"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    system_prompt = Column(String)
    owner_id = Column(Integer, ForeignKey("users.id"))
    parent_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=True) # For agent factories
    settings = Column(JSON, default=dict)  # Webhook URLs and other config

    owner = relationship("User", back_populates="agents")
    cron_jobs = relationship("CronJob", back_populates="agent")

    # Self-referential relationship for spawned agents
    spawned_agents = relationship("Agent", backref="parent_agent", remote_side=[id])

class CronJob(Base):
    __tablename__ = "cron_jobs"
    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id"))
    schedule = Column(String) # Cron string e.g. "0 9 * * *"
    task_description = Column(String)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    agent = relationship("Agent", back_populates="cron_jobs")

class Message(Base):
    """
    All conversation turns for a user's workspace.
    source: 'user' | 'agent' | 'cron'
    from_agent_id: agent that produced the message (None for user turns)
    chat_agent_id: the agent chat window this belongs to (None = broadcast to all)
    """
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    from_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=True)
    chat_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=True)
    source = Column(String, default="agent")   # 'user' | 'agent' | 'cron'
    content = Column(Text, nullable=False)
    embedding = Column(Vector(768), nullable=True) # gemini-2.5-flash uses 768 length embeddings
    created_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", back_populates="messages")
