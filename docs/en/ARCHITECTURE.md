# Architectural Overview: Kiro OpenAI Gateway

## 1. System Purpose and Goals

The project is a high-level proxy gateway implementing the **"Adapter"** structural design pattern.

The main goal of the system is to provide transparent compatibility between two heterogeneous interfaces:
1.  **Target Interface (Client):** Standard OpenAI API protocol (endpoints `/v1/models`, `/v1/chat/completions`).
2.  **Adaptee (Provider):** Internal Kiro IDE API (AWS CodeWhisperer), discovered in the Amazon Kiro ecosystem.

The system acts as a "translator", allowing the use of any tools, libraries, and IDE plugins developed for the OpenAI ecosystem with Claude models through the Kiro API.

## 2. Project Structure

The project is organized as a modular Python package `kiro_gateway/`:

```
kiro-openai-gateway/
├── main.py                    # Entry point, FastAPI application creation
├── config.py                  # Legacy config (for backward compatibility)
├── debug_logger.py            # Debug logging of requests
├── requirements.txt           # Python dependencies
│
├── kiro_gateway/              # Main package
│   ├── __init__.py            # Package exports
│   ├── config.py              # Configuration and constants
│   ├── models.py              # Pydantic models for OpenAI API
│   ├── auth.py                # KiroAuthManager - token management
│   ├── cache.py               # ModelInfoCache - model cache
│   ├── utils.py               # Helper utilities
│   ├── converters.py          # OpenAI <-> Kiro conversion
│   ├── parsers.py             # AWS SSE stream parsers
│   ├── streaming.py           # Response streaming logic
│   ├── http_client.py         # HTTP client with retry logic
│   └── routes.py              # FastAPI routes
│
├── tests/                     # Tests
│   ├── unit/                  # Unit tests
│   └── integration/           # Integration tests
│
└── debug_logs/                # Debug logs (generated)
```

## 3. Architectural Topology and Components

The system is built on the asynchronous `FastAPI` framework and uses an event-driven lifecycle management model (`Lifespan Events`).

### 3.1. Configuration Module (`kiro_gateway/config.py`)

Centralized storage of all settings:

| Parameter | Description | Default Value |
|-----------|-------------|---------------|
| `PROXY_API_KEY` | API key for proxy access | `changeme_proxy_secret` |
| `REFRESH_TOKEN` | Kiro refresh token | from `.env` |
| `REGION` | AWS region | `us-east-1` |
| `TOKEN_REFRESH_THRESHOLD` | Time before token refresh | 600 sec (10 min) |
| `MAX_RETRIES` | Max retry attempts | 3 |
| `MODEL_CACHE_TTL` | Model cache TTL | 3600 sec (1 hour) |

### 3.2. State Management Layer

#### KiroAuthManager (`kiro_gateway/auth.py`)

**Role:** Stateful singleton encapsulating Kiro token management logic.

**Capabilities:**
- Loading credentials from `.env` or JSON file
- Support for `expiresAt` to check token expiration time
- Automatic token refresh 10 minutes before expiration
- Saving updated tokens back to JSON file
- Support for different AWS regions
- Unique fingerprint generation for User-Agent

**Concurrency Control:** Uses `asyncio.Lock` to protect against race conditions.

```python
# Usage example
auth_manager = KiroAuthManager(
    refresh_token="your_token",
    region="us-east-1"
)
token = await auth_manager.get_access_token()
```

#### ModelInfoCache (`kiro_gateway/cache.py`)

**Role:** Thread-safe storage for model configurations.

**Population Strategy:** 
- Lazy Loading via `/ListAvailableModels`
- Cache TTL: 1 hour
- Fallback to static model list

### 3.3. Conversion Layer (`kiro_gateway/converters.py`)

#### Message Conversion

OpenAI messages are transformed into Kiro conversationState:

1. **System prompt** — added to the first user message
2. **Message history** — fully passed in `history` array
3. **Adjacent message merging** — messages with the same role are merged
4. **Tool calls** — OpenAI tools format support
5. **Tool results** — correct transmission of tool call results

#### Model Mapping

External model names are converted to internal Kiro IDs:

| External Name | Internal Kiro ID |
|---------------|------------------|
| `claude-opus-4-5` | `claude-opus-4.5` |
| `claude-opus-4-5-20251101` | `claude-opus-4.5` |
| `claude-haiku-4-5` | `claude-haiku-4.5` |
| `claude-sonnet-4-5` | `CLAUDE_SONNET_4_5_20250929_V1_0` |
| `claude-sonnet-4-5-20250929` | `CLAUDE_SONNET_4_5_20250929_V1_0` |
| `claude-sonnet-4` | `CLAUDE_SONNET_4_20250514_V1_0` |
| `claude-sonnet-4-20250514` | `CLAUDE_SONNET_4_20250514_V1_0` |
| `claude-3-7-sonnet-20250219` | `CLAUDE_3_7_SONNET_20250219_V1_0` |

### 3.4. Parsing Layer (`kiro_gateway/parsers.py`)

#### AwsEventStreamParser

Advanced AWS SSE format parser with support for:

- **Bracket counting** — correct parsing of nested JSON objects
- **Content deduplication** — filtering of duplicate events
- **Tool calls** — parsing of structured and bracket-style tool calls
- **Escape sequences** — decoding of `\n` and others

#### Event Types

| Event | Description |
|-------|-------------|
| `content` | Text content of the response |
| `tool_start` | Start of tool call (name, toolUseId) |
| `tool_input` | Continuation of input for tool call |
| `tool_stop` | End of tool call |
| `usage` | Credit consumption information |
| `context_usage` | Context usage percentage |

### 3.5. HTTP Client (`kiro_gateway/http_client.py`)

#### KiroHttpClient

Automatic error handling with exponential backoff:

| Error Code | Action |
|------------|--------|
| `403` | Token refresh + retry |
| `429` | Exponential backoff (1s, 2s, 4s) |
| `5xx` | Exponential backoff (up to 3 attempts) |
| Timeout | Exponential backoff |

### 3.6. Kiro API Endpoints

All URLs are dynamically formed based on the region:

*   **Token Refresh:** `POST https://prod.{region}.auth.desktop.kiro.dev/refreshToken`
*   **List Models:** `GET https://q.{region}.amazonaws.com/ListAvailableModels`
*   **Generate Response:** `POST https://codewhisperer.{region}.amazonaws.com/generateAssistantResponse`

## 4. Detailed Data Flow

```
┌─────────────────┐
│  OpenAI Client  │
└────────┬────────┘
         │ POST /v1/chat/completions
         ▼
┌─────────────────┐
│  Security Gate  │ ◄── Proxy Bearer token verification
│  (routes.py)    │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ KiroAuthManager │ ◄── Get/refresh accessToken
│   (auth.py)     │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Payload Builder │ ◄── Convert OpenAI → Kiro format
│ (converters.py) │     (history, system prompt, tools)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ KiroHttpClient  │ ◄── Retry logic (403, 429, 5xx)
│ (http_client.py)│
└────────┬────────┘
         │ POST /generateAssistantResponse
         ▼
┌─────────────────┐
│   Kiro API      │
└────────┬────────┘
         │ AWS SSE Stream
         ▼
┌─────────────────┐
│ SSE Parser      │ ◄── Event parsing, tool calls
│  (parsers.py)   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ OpenAI Format   │ ◄── Convert to OpenAI SSE
│ (streaming.py)  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  OpenAI Client  │
└─────────────────┘
```

## 5. Available Models

| Model | Description | Credits |
|-------|-------------|---------|
| `claude-opus-4-5` | Top-tier model | ~2.2 |
| `claude-opus-4-5-20251101` | Top-tier model (version) | ~2.2 |
| `claude-sonnet-4-5` | Enhanced model | ~1.3 |
| `claude-sonnet-4-5-20250929` | Enhanced model (version) | ~1.3 |
| `claude-sonnet-4` | Balanced model | ~1.3 |
| `claude-sonnet-4-20250514` | Balanced (version) | ~1.3 |
| `claude-haiku-4-5` | Fast model | ~0.4 |
| `claude-3-7-sonnet-20250219` | Legacy model | ~1.0 |

## 6. Configuration

### Environment Variables (.env)

```env
# Required
REFRESH_TOKEN="your_kiro_refresh_token"
PROXY_API_KEY="your_proxy_secret"

# Optional
PROFILE_ARN="arn:aws:codewhisperer:..."
KIRO_REGION="us-east-1"
KIRO_CREDS_FILE="~/.aws/sso/cache/kiro-auth-token.json"
```

### JSON Credentials File (optional)

```json
{
  "accessToken": "eyJ...",
  "refreshToken": "eyJ...",
  "expiresAt": "2025-01-12T23:00:00.000Z",
  "profileArn": "arn:aws:codewhisperer:us-east-1:...",
  "region": "us-east-1"
}
```

## 7. API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Health check |
| `/health` | GET | Detailed health check |
| `/v1/models` | GET | List of available models |
| `/v1/chat/completions` | POST | Chat completions (streaming/non-streaming) |

## 8. Implementation Features

### Tool Calling

Support for OpenAI-compatible tools format:

```json
{
  "tools": [{
    "type": "function",
    "function": {
      "name": "get_weather",
      "description": "Get weather for a location",
      "parameters": {
        "type": "object",
        "properties": {
          "location": {"type": "string"}
        }
      }
    }
  }]
}
```

### Streaming

Full SSE streaming support with correct OpenAI format:

```
data: {"id":"chatcmpl-...","object":"chat.completion.chunk",...}

data: [DONE]
```

### Debugging

All requests and responses are logged in `debug_logs/`:
- `request_body.json` — incoming request
- `google_request_body.json` — request to Kiro API
- `response_stream_raw.txt` — raw stream from Kiro
- `response_stream_modified.txt` — transformed stream

## 9. Extensibility

### Adding a New Provider

The modular architecture allows easy addition of support for other providers:

1. Create a new module `kiro_gateway/providers/new_provider.py`
2. Implement classes:
   - `NewProviderAuthManager` — token management
   - `NewProviderConverter` — format conversion
   - `NewProviderParser` — response parsing
3. Add routes to `routes.py` or create a separate router

### Example Structure for a New Provider

```python
# kiro_gateway/providers/gemini.py

class GeminiAuthManager:
    """Gemini API key management."""
    pass

class GeminiConverter:
    """OpenAI -> Gemini format conversion."""
    pass

class GeminiParser:
    """Gemini SSE stream parsing."""
    pass