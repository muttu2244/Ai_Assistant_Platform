"""Backward-compatible import shim.

Use src.agents.ai_engine.foundry_client instead.
"""

from src.agents.ai_engine.foundry_client import FoundryClient

# Keep legacy symbol for existing imports.
Gpt4oClient = FoundryClient
