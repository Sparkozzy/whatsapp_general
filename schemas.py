from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, Dict, Any

# Z-API Webhook Schema
class ZApiFile(BaseModel):
    publicUrl: Optional[str] = None

class ZApiDetails(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    
    sender_from: str = Field(alias="from")
    file: Optional[ZApiFile] = None

class ZApiContent(BaseModel):
    type: str
    text: Optional[str] = None
    details: ZApiDetails

class ZApiWebhookPayload(BaseModel):
    instanceId: Optional[str] = None
    eventType: str
    content: ZApiContent

# CRM Webhook Schema
class CrmFile(BaseModel):
    url: Optional[str] = None
    publicUrl: Optional[str] = None

class CrmDetails(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    
    to: str
    sender_from: str = Field(alias="from")
    file: Optional[CrmFile] = None

class CrmWebhookPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    
    type: str  # TEXT, AUDIO, etc.
    text: Optional[str] = None
    direction: str  # FROM_HUB, TO_HUB
    details: CrmDetails

# Internal Normalized Schema
class NormalizedMessage(BaseModel):
    client_id: str
    phone: str
    text: str
    type: str  # texto, audio, etc.
    audio_url: Optional[str] = None
    raw_payload: Dict[str, Any]
