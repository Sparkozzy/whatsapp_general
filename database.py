import os
from dotenv import load_dotenv
from supabase import create_client, Client
from typing import Dict

# Load environment variables
load_dotenv()

MASTER_SUPABASE_URL = os.getenv("MASTER_SUPABASE_URL")
MASTER_SUPABASE_SERVICE_KEY = os.getenv("MASTER_SUPABASE_SERVICE_KEY")

if not MASTER_SUPABASE_URL or not MASTER_SUPABASE_SERVICE_KEY:
    raise ValueError("MASTER_SUPABASE_URL and MASTER_SUPABASE_SERVICE_KEY must be set in .env")

# Singleton Master Supabase Client
master_supabase: Client = create_client(MASTER_SUPABASE_URL, MASTER_SUPABASE_SERVICE_KEY)

class ClientDatabaseManager:
    """
    Manages tenant Supabase client connections dynamically and caches them
    to avoid multiple instances initialization for the same client.
    """
    _clients: Dict[str, Client] = {}

    @classmethod
    def get_client(cls, client_id: str) -> Client:
        if client_id in cls._clients:
            return cls._clients[client_id]

        # Fetch client configuration from Supabase Master
        config_res = master_supabase.table("client_configurations")\
            .select("supabase_url, supabase_service_key")\
            .eq("client_id", client_id)\
            .single()\
            .execute()

        if not config_res.data:
            raise ValueError(f"Client configuration not found for client_id: {client_id}")

        supabase_url = config_res.data.get("supabase_url")
        supabase_service_key = config_res.data.get("supabase_service_key")

        if not supabase_url or not supabase_service_key:
            raise ValueError(f"Incomplete Supabase credentials for client_id: {client_id}")

        # Initialize tenant client and cache it
        client = create_client(supabase_url, supabase_service_key)
        cls._clients[client_id] = client
        return client

    @classmethod
    def get_client_config(cls, client_id: str) -> dict:
        """
        Helper to fetch all configurations of a client from Master DB.
        """
        res = master_supabase.table("client_configurations")\
            .select("*")\
            .eq("client_id", client_id)\
            .single()\
            .execute()
        
        if not res.data:
            raise ValueError(f"Client configuration not found for client_id: {client_id}")
        
        return res.data
