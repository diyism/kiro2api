# -*- coding: utf-8 -*-

# Kiro OpenAI Gateway
# https://github.com/jwadow/kiro-openai-gateway
# Copyright (C) 2025 Jwadow
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

import json
import os
import sys
import uuid
from pathlib import Path

import requests
from dotenv import load_dotenv
from loguru import logger

# --- Load environment variables ---
load_dotenv()

# --- Configuration ---
KIRO_API_HOST = "https://q.us-east-1.amazonaws.com"
TOKEN_URL = "https://prod.us-east-1.auth.desktop.kiro.dev/refreshToken"
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN")
PROFILE_ARN = os.getenv("PROFILE_ARN", "arn:aws:codewhisperer:us-east-1:699475941385:profile/EHGA3GRVQMUK")
KIRO_CREDS_FILE = os.getenv("KIRO_CREDS_FILE", "")

# --- Load credentials from file if REFRESH_TOKEN not in env ---
if not REFRESH_TOKEN and KIRO_CREDS_FILE:
    try:
        creds_path = Path(KIRO_CREDS_FILE).expanduser()
        if creds_path.exists():
            with open(creds_path, 'r', encoding='utf-8') as f:
                creds_data = json.load(f)
            REFRESH_TOKEN = creds_data.get("refreshToken", "")
            if creds_data.get("profileArn"):
                PROFILE_ARN = creds_data["profileArn"]
            logger.info(f"Credentials loaded from {KIRO_CREDS_FILE}")
        else:
            logger.warning(f"Credentials file not found: {KIRO_CREDS_FILE}")
    except Exception as e:
        logger.error(f"Error loading credentials from file: {e}")

# --- Validate required credentials ---
if not REFRESH_TOKEN:
    logger.error("Neither REFRESH_TOKEN env variable nor KIRO_CREDS_FILE is configured. Exiting.")
    sys.exit(1)

# Global variables
AUTH_TOKEN = None
HEADERS = {
    "Authorization": None,
    "Content-Type": "application/json",
    "User-Agent": "aws-sdk-js/1.0.27 ua/2.1 os/win32#10.0.19044 lang/js md/nodejs#22.21.1 api/codewhispererstreaming#1.0.27 m/E KiroIDE-0.7.45-31c325a0ff0a9c8dec5d13048f4257462d751fe5b8af4cb1088f1fca45856c64",
    "x-amz-user-agent": "aws-sdk-js/1.0.27 KiroIDE-0.7.45-31c325a0ff0a9c8dec5d13048f4257462d751fe5b8af4cb1088f1fca45856c64",
    "x-amzn-codewhisperer-optout": "true",
    "x-amzn-kiro-agent-mode": "vibe",
}


def refresh_auth_token():
    """Refreshes AUTH_TOKEN via Kiro API."""
    global AUTH_TOKEN, HEADERS
    logger.info("Refreshing Kiro token...")
    
    payload = {"refreshToken": REFRESH_TOKEN}
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "KiroIDE-0.7.45-31c325a0ff0a9c8dec5d13048f4257462d751fe5b8af4cb1088f1fca45856c64",
    }
    
    try:
        response = requests.post(TOKEN_URL, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        
        new_token = data.get("accessToken")
        expires_in = data.get("expiresIn")
        
        if not new_token:
            logger.error("Failed to get accessToken from response")
            return False

        logger.success(f"Token successfully refreshed. Expires in: {expires_in}s")
        AUTH_TOKEN = new_token
        HEADERS['Authorization'] = f"Bearer {AUTH_TOKEN}"
        return True
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Error refreshing token: {e}")
        if hasattr(e, 'response') and e.response:
            logger.error(f"Server response: {e.response.status_code} {e.response.text}")
        return False


def test_get_models():
    """Tests the ListAvailableModels endpoint."""
    logger.info("Testing /ListAvailableModels...")
    url = f"{KIRO_API_HOST}/ListAvailableModels"
    params = {
        "origin": "AI_EDITOR",
        "profileArn": PROFILE_ARN
    }

    try:
        response = requests.get(url, headers=HEADERS, params=params)
        response.raise_for_status()

        logger.info(f"Response status: {response.status_code}")
        logger.debug(f"Response (JSON):\n{json.dumps(response.json(), indent=2, ensure_ascii=False)}")
        logger.success("ListAvailableModels test COMPLETED SUCCESSFULLY")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"ListAvailableModels test failed: {e}")
        return False


def test_generate_content():
    """Tests the generateAssistantResponse endpoint."""
    logger.info("Testing /generateAssistantResponse...")
    url = f"{KIRO_API_HOST}/generateAssistantResponse"
    
    payload = {
        "conversationState": {
            "agentContinuationId": str(uuid.uuid4()),
            "agentTaskType": "vibe",
            "chatTriggerType": "MANUAL",
            "conversationId": str(uuid.uuid4()),
            "currentMessage": {
                "userInputMessage": {
                    "content": "Hello! Say something short.",
                    "modelId": "claude-haiku-4.5",
                    "origin": "AI_EDITOR",
                    "userInputMessageContext": {
                        "tools": []
                    }
                }
            },
            "history": []
        },
        "profileArn": PROFILE_ARN
    }

    try:
        with requests.post(url, headers=HEADERS, json=payload, stream=True) as response:
            response.raise_for_status()
            logger.info(f"Response status: {response.status_code}")
            logger.info("Streaming response:")

            for chunk in response.iter_content(chunk_size=1024):
                if chunk:
                    # Try to decode and find JSON
                    chunk_str = chunk.decode('utf-8', errors='ignore')
                    logger.debug(f"Chunk: {chunk_str[:200]}...")

        logger.success("generateAssistantResponse test COMPLETED")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"generateAssistantResponse test failed: {e}")
        return False


if __name__ == "__main__":
    # Determine credential source for logging
    cred_source = "KIRO_CREDS_FILE" if KIRO_CREDS_FILE else "REFRESH_TOKEN"
    logger.info(f"Starting Kiro API tests (credentials from {cred_source})...")

    token_ok = refresh_auth_token()

    if token_ok:
        models_ok = test_get_models()
        generate_ok = test_generate_content()

        if models_ok and generate_ok:
            logger.success(f"All tests passed successfully! (credentials from {cred_source})")
        else:
            logger.warning(f"One or more tests failed. (credentials from {cred_source})")
    else:
        logger.error("Failed to refresh token. Tests not started.")
