#!/usr/bin/env python3
"""Test direct connection to Anthropic Foundry endpoint."""

import asyncio
import json
from azure.identity import DefaultAzureCredential
from anthropic import AnthropicFoundry
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

async def test_foundry():
    endpoint = "https://streamline-foundry-nonprod.services.ai.azure.com/anthropic/"
    deployment = "claude-opus-4-6"
    
    print(f"Testing endpoint: {endpoint}")
    print(f"Deployment: {deployment}")
    print()
    
    # Get token
    try:
        credential = DefaultAzureCredential()
        token = credential.get_token("https://cognitiveservices.azure.com/.default")
        print(f"✓ Token acquired: {token.token[:30]}...")
    except Exception as exc:
        print(f"✗ Token acquisition failed: {exc}")
        return
    
    # Create client
    try:
        client = AnthropicFoundry(api_key=token.token, base_url=endpoint)
        print(f"✓ AnthropicFoundry client created")
    except Exception as exc:
        print(f"✗ Client creation failed: {exc}")
        return
    
    # Test message
    try:
        print(f"\nSending test message...")
        response = await asyncio.to_thread(
            client.messages.create,
            model=deployment,
            system="You are a helpful assistant.",
            messages=[{"role": "user", "content": "What is 2+2?"}],
            max_tokens=100,
            temperature=0.2,
        )
        print(f"✓ Success! Response:")
        print(f"  {response.content[0].text if response.content else 'No content'}")
    except Exception as exc:
        print(f"✗ API call failed:")
        print(f"  Error type: {type(exc).__name__}")
        print(f"  Status code: {getattr(exc, 'status_code', 'N/A')}")
        print(f"  Message: {str(exc)[:500]}")

if __name__ == "__main__":
    asyncio.run(test_foundry())
