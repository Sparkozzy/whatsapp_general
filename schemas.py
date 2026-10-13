from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, Dict, Any

# Z-API Webhook Schema
class ZApiFile(BaseModel):
    url: Optional[str] = None
    publicUrl: Optional[str] = None
    fileName: Optional[str] = None
    name: Optional[str] = None

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
    fileName: Optional[str] = None
    name: Optional[str] = None

class CrmDetails(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    
    to: str
    sender_from: str = Field(alias="from")
    file: Optional[CrmFile] = None

class CrmContent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    
    type: str = "TEXT"  # TEXT, AUDIO, IMAGE, DOCUMENT, PDF, etc.
    text: Optional[str] = None
    direction: str  # FROM_HUB, TO_HUB
    details: CrmDetails

class CrmWebhookPayload(BaseModel):
    eventType: str
    content: CrmContent


# Internal Normalized Schema
class NormalizedMessage(BaseModel):
    client_id: str
    phone: str
    text: str
    type: str  # texto, audio, imagem, documento, etc.
    audio_url: Optional[str] = None
    image_url: Optional[str] = None
    file_url: Optional[str] = None
    raw_payload: Dict[str, Any]

# FUP (Follow-up) Schemas
from typing import Literal

FupActionType = Literal["agendar_mensagem", "audio", "figurinha", "ligawhats", "ligacao"]


class FupRequestPayload(BaseModel):
    client_id: str
    phone: str
    lead_id: Optional[str] = None
    custom_context: Optional[str] = None
    from_workflow: str = "webhook_fup"
    execution_id: Optional[str] = None

class FupAgentDecision(BaseModel):
    acao: FupActionType
    quando_executar: str  # ISO 8601 com timezone offset obrigatório (ex: 2026-09-25T15:30:00-03:00)
    conteudo: Optional[str] = None
    justificativa: Optional[str] = None



