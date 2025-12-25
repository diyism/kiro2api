# -*- coding: utf-8 -*-

# Kiro OpenAI Gateway
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

"""
Streaming logic for converting Kiro stream to Anthropic Messages API format.

Handles conversion of Kiro SSE stream to Anthropic's streaming event format
for Claude Code compatibility.
"""

import json
import time
from typing import AsyncGenerator, Optional, Dict, Any, List

import httpx
from loguru import logger

from kiro_gateway.parsers import AwsEventStreamParser, parse_bracket_tool_calls
from kiro_gateway.utils import generate_completion_id


async def stream_kiro_to_anthropic(
    client: httpx.AsyncClient,
    response: httpx.Response,
    model: str,
    request_id: Optional[str] = None,
    debug_logger = None
) -> AsyncGenerator[str, None]:
    """
    Convert Kiro SSE stream to Anthropic Messages API streaming format.

    Args:
        client: HTTP client
        response: HTTP response with SSE stream
        model: Model name
        request_id: Optional request ID (generated if not provided)
        debug_logger: Optional debug logger instance

    Yields:
        SSE formatted strings for Anthropic streaming events
    """
    if not request_id:
        request_id = f"msg_{generate_completion_id()}"

    parser = AwsEventStreamParser()
    accumulated_text = ""
    tool_uses: List[Dict[str, Any]] = []
    current_tool_index = 0
    input_tokens = 0
    output_tokens = 0
    stop_reason = None
    message_started = False

    try:
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue

            # Log raw chunk for debugging
            if debug_logger:
                debug_logger.log_raw_chunk(chunk)

            # Parse AWS event stream - feed returns a list of events
            events = parser.feed(chunk)

            # # Debug: log received events
            # if events:
            #     logger.debug(f"[Anthropic Streaming] Received {len(events)} events")
            #     for evt in events:
            #         logger.debug(f"[Anthropic Streaming] Event: {evt}")

            for event in events:
                event_type = event.get("type")

                # Handle content events (new Kiro API format)
                if event_type == "content":
                    content = event.get("data", "")

                    # Send message_start event if not sent yet
                    if not message_started:
                        message_start = {
                            "type": "message_start",
                            "message": {
                                "id": request_id,
                                "type": "message",
                                "role": "assistant",
                                "content": [],
                                "model": model,
                                "stop_reason": None,
                                "stop_sequence": None,
                                "usage": {
                                    "input_tokens": 0,
                                    "output_tokens": 0
                                }
                            }
                        }
                        chunk_text = f"event: message_start\ndata: {json.dumps(message_start)}\n\n"
                        if debug_logger:
                            debug_logger.log_modified_chunk(chunk_text.encode('utf-8'))
                        yield chunk_text
                        message_started = True

                    # Check for tool calls in content
                    tool_calls = parse_bracket_tool_calls(content)

                    if tool_calls:
                        # Handle tool use
                        for tc in tool_calls:
                            tool_use_id = f"toolu_{int(time.time() * 1000)}_{current_tool_index}"
                            tool_use = {
                                "type": "tool_use",
                                "id": tool_use_id,
                                "name": tc.get("name", ""),
                                "input": tc.get("arguments", {})
                            }
                            tool_uses.append(tool_use)

                            # Send content_block_start
                            block_start = {
                                "type": "content_block_start",
                                "index": current_tool_index,
                                "content_block": tool_use
                            }
                            yield f"event: content_block_start\ndata: {json.dumps(block_start)}\n\n"

                            # Send content_block_stop
                            block_stop = {
                                "type": "content_block_stop",
                                "index": current_tool_index
                            }
                            yield f"event: content_block_stop\ndata: {json.dumps(block_stop)}\n\n"

                            current_tool_index += 1
                    else:
                        # Regular text content
                        if content:
                            # Send content_block_start for first text chunk
                            if not accumulated_text:
                                block_start = {
                                    "type": "content_block_start",
                                    "index": 0,
                                    "content_block": {
                                        "type": "text",
                                        "text": ""
                                    }
                                }
                                chunk_text = f"event: content_block_start\ndata: {json.dumps(block_start)}\n\n"
                                if debug_logger:
                                    debug_logger.log_modified_chunk(chunk_text.encode('utf-8'))
                                yield chunk_text

                            # Send content_block_delta
                            delta = {
                                "type": "content_block_delta",
                                "index": 0,
                                "delta": {
                                    "type": "text_delta",
                                    "text": content
                                }
                            }
                            chunk_text = f"event: content_block_delta\ndata: {json.dumps(delta)}\n\n"
                            if debug_logger:
                                debug_logger.log_modified_chunk(chunk_text.encode('utf-8'))
                            yield chunk_text

                            accumulated_text += content

                # Handle metadata event (for usage info)
                elif event_type == "metadata":
                    metadata = event.get("metadata", {})
                    usage_info = metadata.get("usage", {})
                    input_tokens = usage_info.get("inputTokens", 0)
                    output_tokens = usage_info.get("outputTokens", 0)

        # Send content_block_stop for text if we had any
        if accumulated_text:
            block_stop = {
                "type": "content_block_stop",
                "index": 0
            }
            chunk_text = f"event: content_block_stop\ndata: {json.dumps(block_stop)}\n\n"
            if debug_logger:
                debug_logger.log_modified_chunk(chunk_text.encode('utf-8'))
            yield chunk_text

        # Get tool calls from parser (Kiro sends them as toolUseEvent)
        parser_tool_calls = parser.get_tool_calls()
        if parser_tool_calls:
            logger.debug(f"[Anthropic Streaming] Found {len(parser_tool_calls)} tool calls from parser")
            for tool_call in parser_tool_calls:
                # Convert from OpenAI format to Anthropic format
                try:
                    arguments = tool_call.get('function', {}).get('arguments', '{}')
                    if isinstance(arguments, str):
                        arguments = json.loads(arguments)
                except (json.JSONDecodeError, ValueError):
                    logger.warning(f"Failed to parse tool arguments: {arguments}")
                    arguments = {}

                tool_use_id = f"toolu_{int(time.time() * 1000)}_{current_tool_index}"
                tool_use = {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": tool_call.get('function', {}).get('name', ''),
                    "input": arguments
                }
                tool_uses.append(tool_use)

                # Send content_block_start
                block_start = {
                    "type": "content_block_start",
                    "index": current_tool_index + (1 if accumulated_text else 0),
                    "content_block": tool_use
                }
                chunk_text = f"event: content_block_start\ndata: {json.dumps(block_start)}\n\n"
                if debug_logger:
                    debug_logger.log_modified_chunk(chunk_text.encode('utf-8'))
                yield chunk_text

                # Send content_block_stop
                block_stop = {
                    "type": "content_block_stop",
                    "index": current_tool_index + (1 if accumulated_text else 0)
                }
                chunk_text = f"event: content_block_stop\ndata: {json.dumps(block_stop)}\n\n"
                if debug_logger:
                    debug_logger.log_modified_chunk(chunk_text.encode('utf-8'))
                yield chunk_text

                current_tool_index += 1

        # Determine stop reason
        if tool_uses:
            stop_reason = "tool_use"
        else:
            stop_reason = "end_turn"

        # Send message_delta
        message_delta = {
            "type": "message_delta",
            "delta": {
                "stop_reason": stop_reason,
                "stop_sequence": None
            },
            "usage": {
                "output_tokens": output_tokens
            }
        }
        chunk_text = f"event: message_delta\ndata: {json.dumps(message_delta)}\n\n"
        if debug_logger:
            debug_logger.log_modified_chunk(chunk_text.encode('utf-8'))
        yield chunk_text

        # Send message_stop
        message_stop = {
            "type": "message_stop"
        }
        chunk_text = f"event: message_stop\ndata: {json.dumps(message_stop)}\n\n"
        if debug_logger:
            debug_logger.log_modified_chunk(chunk_text.encode('utf-8'))
        yield chunk_text

    except Exception as e:
        logger.error(f"Error in Anthropic streaming: {e}", exc_info=True)
        # Send error event
        error_event = {
            "type": "error",
            "error": {
                "type": "api_error",
                "message": str(e)
            }
        }
        yield f"event: error\ndata: {json.dumps(error_event)}\n\n"
        raise


async def collect_anthropic_response(
    client: httpx.AsyncClient,
    response: httpx.Response,
    model: str,
    request_id: Optional[str] = None,
    debug_logger = None
) -> Dict[str, Any]:
    """
    Collect complete Anthropic response from Kiro stream.

    Args:
        client: HTTP client
        response: HTTP response with SSE stream
        model: Model name
        request_id: Optional request ID
        debug_logger: Optional debug logger instance

    Returns:
        Complete Anthropic Messages API response
    """
    if not request_id:
        request_id = f"msg_{generate_completion_id()}"

    parser = AwsEventStreamParser()
    accumulated_text = ""
    content_blocks: List[Dict[str, Any]] = []
    input_tokens = 0
    output_tokens = 0
    stop_reason = "end_turn"

    try:
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue

            # Log raw chunk for debugging
            if debug_logger:
                debug_logger.log_raw_chunk(chunk)

            # Parse AWS event stream - feed returns a list of events
            events = parser.feed(chunk)

            # # Debug: log received events
            # if events:
            #     logger.debug(f"[Anthropic Collect] Received {len(events)} events")
            #     for evt in events:
            #         logger.debug(f"[Anthropic Collect] Event: {evt}")

            for event in events:
                event_type = event.get("type")

                if event_type == "content":
                    content = event.get("data", "")

                    # Check for tool calls
                    tool_calls = parse_bracket_tool_calls(content)

                    if tool_calls:
                        for tc in tool_calls:
                            tool_use_id = f"toolu_{int(time.time() * 1000)}_{len(content_blocks)}"
                            content_blocks.append({
                                "type": "tool_use",
                                "id": tool_use_id,
                                "name": tc.get("name", ""),
                                "input": tc.get("arguments", {})
                            })
                        stop_reason = "tool_use"
                    else:
                        accumulated_text += content

                elif event_type == "metadata":
                    metadata = event.get("metadata", {})
                    usage_info = metadata.get("usage", {})
                    input_tokens = usage_info.get("inputTokens", 0)
                    output_tokens = usage_info.get("outputTokens", 0)

        # Add text content if any
        if accumulated_text:
            content_blocks.insert(0, {
                "type": "text",
                "text": accumulated_text
            })

        # Build response
        return {
            "id": request_id,
            "type": "message",
            "role": "assistant",
            "content": content_blocks,
            "model": model,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens
            }
        }

    except Exception as e:
        logger.error(f"Error collecting Anthropic response: {e}", exc_info=True)
        raise
