#!/usr/bin/env python3
"""
MCP Server Framework - Reusable HTTP-based MCP server implementation

This framework provides MCPHTTPServer class for building MCP-compliant HTTP servers.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import asyncio
import inspect
import json
import logging
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Callable, Dict, List, Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


class MCPHTTPServer:
    """Model Context Protocol Server Implementation with HTTP Transport"""
    
    def __init__(self, name: str = "MCP Server", version: str = "1.0.0", port: int = 8000, host: str = "localhost"):
        self.name = name
        self.version = version
        self.port = port
        self.host = host
        self.tools: Dict[str, Dict[str, Any]] = {}
        self.resources: Dict[str, Dict[str, Any]] = {}
        self.initialized = False
        
    def tool(self, func: Callable) -> Callable:
        """Decorator to register a tool function."""
        tool_info = {
            'function': func,
            'name': func.__name__,
            'description': self._extract_description(func),
            'parameters': self._extract_parameters(func),
            'is_async': asyncio.iscoroutinefunction(func)
        }
        self.tools[func.__name__] = tool_info
        logger.info(f"Registered tool: {func.__name__}")
        return func
    
    def resource(self, uri: str) -> Callable[[Callable], Callable]:
        """Decorator to register a resource function."""
        def decorator(func: Callable) -> Callable:
            resource_info = {
                'function': func,
                'uri': uri,
                'name': func.__name__,
                'description': self._extract_description(func),
                'parameters': self._extract_parameters(func),
                'is_async': asyncio.iscoroutinefunction(func)
            }
            self.resources[uri] = resource_info
            logger.info(f"Registered resource: {uri}")
            return func
        return decorator
    
    def _extract_description(self, func: Callable) -> str:
        """Extract description from function docstring."""
        if func.__doc__:
            return func.__doc__.strip().split('\n')[0]
        return f"Function: {func.__name__}"
    
    def _extract_parameters(self, func: Callable) -> Dict[str, Any]:
        """Extract parameter information from function signature."""
        sig = inspect.signature(func)
        parameters = {}
        
        for name, param in sig.parameters.items():
            param_info = {
                'name': name,
                'required': param.default == inspect.Parameter.empty
            }
            
            if param.annotation != inspect.Parameter.empty:
                param_info['type'] = self._python_type_to_json_schema(param.annotation)
            else:
                param_info['type'] = 'string'
            
            if param.default != inspect.Parameter.empty:
                param_info['default'] = param.default
            
            parameters[name] = param_info
        
        return parameters
    
    def _python_type_to_json_schema(self, python_type: Any) -> str:
        """Convert Python type to JSON schema type."""
        type_mapping = {
            str: 'string',
            int: 'integer',
            float: 'number',
            bool: 'boolean',
            list: 'array',
            dict: 'object',
            List: 'array',
            Dict: 'object',
            Optional: 'string'  # Simplified
        }
        
        if hasattr(python_type, '__origin__'):
            return type_mapping.get(python_type.__origin__, 'string')
        
        return type_mapping.get(python_type, 'string')
    
    async def process_message(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Process incoming MCP message."""
        try:
            method_raw = message.get('method')
            method = str(method_raw) if method_raw is not None else ''
            params_raw = message.get('params', {})
            params = params_raw if isinstance(params_raw, dict) else {}
            msg_id = message.get('id')
            
            logger.info(f"Processing message: {method} (id: {msg_id})")
            
            if msg_id is None:
                await self._handle_notification(method, params)
                return None
            
            if method == 'initialize':
                return await self._handle_initialize(msg_id, params)
            elif method == 'tools/list':
                return await self._handle_list_tools(msg_id)
            elif method == 'tools/call':
                return await self._handle_call_tool(msg_id, params)
            elif method == 'resources/list':
                return await self._handle_list_resources(msg_id)
            elif method == 'resources/read':
                return await self._handle_read_resource(msg_id, params)
            else:
                return self._error_response(msg_id, f"Unknown method: {method}")
                
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            logger.error(traceback.format_exc())
            return self._error_response(message.get('id'), str(e))
    
    async def _handle_notification(self, method: str, params: Dict[str, Any]) -> None:
        """Handle notification messages."""
        if method == 'initialized':
            self.initialized = True
            logger.info("Client initialized")
    
    async def _handle_initialize(self, msg_id: int, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle initialize request."""
        return {
            'jsonrpc': '2.0',
            'id': msg_id,
            'result': {
                'protocolVersion': '2024-11-05',
                'capabilities': {
                    'tools': {'listChanged': True},
                    'resources': {'listChanged': True, 'subscribe': True},
                },
                'serverInfo': {
                    'name': self.name,
                    'version': self.version
                }
            }
        }
    
    async def _handle_list_tools(self, msg_id: int) -> Dict[str, Any]:
        """Handle list tools request."""
        tools_list = []
        
        for tool_name, tool_info in self.tools.items():
            properties = {}
            required = []
            
            for param_name, param_info in tool_info['parameters'].items():
                properties[param_name] = {
                    'type': param_info['type'],
                    'description': f"Parameter: {param_name}"
                }
                
                if 'default' in param_info:
                    properties[param_name]['default'] = param_info['default']
                
                if param_info.get('required', False):
                    required.append(param_name)
            
            tool_schema = {
                'name': tool_name,
                'description': tool_info['description'],
                'inputSchema': {
                    'type': 'object',
                    'properties': properties,
                    'required': required
                }
            }
            
            tools_list.append(tool_schema)
        
        return {
            'jsonrpc': '2.0',
            'id': msg_id,
            'result': {'tools': tools_list}
        }
    
    async def _handle_call_tool(self, msg_id: int, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle call tool request."""
        tool_name = params.get('name')
        arguments = params.get('arguments', {})
        
        if tool_name not in self.tools:
            return self._error_response(msg_id, f"Unknown tool: {tool_name}")
        
        try:
            tool_info = self.tools[tool_name]
            tool_func = tool_info['function']
            
            if tool_info['is_async']:
                result = await tool_func(**arguments)
            else:
                result = tool_func(**arguments)
            
            content = self._format_tool_result(result)
            
            return {
                'jsonrpc': '2.0',
                'id': msg_id,
                'result': {
                    'content': content
                }
            }
            
        except Exception as e:
            logger.error(f"Tool execution error: {e}")
            logger.error(traceback.format_exc())
            return self._error_response(msg_id, f"Tool execution error: {str(e)}")
    
    def _format_tool_result(self, result: Any) -> List[Dict[str, Any]]:
        """Format tool result as MCP content."""
        if isinstance(result, str):
            return [{'type': 'text', 'text': result}]
        elif isinstance(result, (dict, list)):
            return [{'type': 'text', 'text': json.dumps(result, indent=2, ensure_ascii=False)}]
        else:
            return [{'type': 'text', 'text': str(result)}]
    
    async def _handle_list_resources(self, msg_id: int) -> Dict[str, Any]:
        """Handle list resources request."""
        resources_list = []
        
        for uri, resource_info in self.resources.items():
            resource_schema = {
                'uri': uri,
                'name': resource_info['name'],
                'description': resource_info['description'],
                'mimeType': 'application/json'
            }
            resources_list.append(resource_schema)
        
        return {
            'jsonrpc': '2.0',
            'id': msg_id,
            'result': {'resources': resources_list}
        }
    
    async def _handle_read_resource(self, msg_id: int, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle read resource request."""
        uri = params.get('uri')
        
        if not uri:
            return self._error_response(msg_id, "Missing URI parameter")
        
        resource_info = self.resources.get(uri)
        
        if not resource_info:
            return self._error_response(msg_id, f"Resource not found: {uri}")
        
        try:
            resource_func = resource_info['function']
            
            if resource_info['is_async']:
                result = await resource_func()
            else:
                result = resource_func()
            
            content = self._format_tool_result(result)
            
            return {
                'jsonrpc': '2.0',
                'id': msg_id,
                'result': {
                    'contents': content
                }
            }
            
        except Exception as e:
            logger.error(f"Resource read error: {e}")
            return self._error_response(msg_id, f"Resource read error: {str(e)}")
    
    def _error_response(self, msg_id: Any, error_msg: str) -> Dict[str, Any]:
        """Create error response."""
        return {
            'jsonrpc': '2.0',
            'id': msg_id,
            'error': {
                'code': -32603,
                'message': error_msg
            }
        }
    
    def run(self):
        """Run the HTTP server."""
        def handler_factory(mcp_server):
            def handler(*args, **kwargs):
                return MCPHTTPRequestHandler(mcp_server, *args, **kwargs)
            return handler
        
        handler = handler_factory(self)
        httpd = HTTPServer((self.host, self.port), handler)
        
        logger.info("=" * 80)
        logger.info(f"Starting {self.name} v{self.version}")
        logger.info(f"HTTP Server: http://{self.host}:{self.port}")
        logger.info(f"Registered: {len(self.tools)} tools")
        logger.info("=" * 80)
        
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            logger.info("Server stopped by user")
        except Exception as e:
            logger.error(f"Server error: {e}")
            logger.error(traceback.format_exc())


class MCPHTTPRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for MCP server."""
    
    def __init__(self, mcp_server, *args, **kwargs):
        self.mcp_server = mcp_server
        super().__init__(*args, **kwargs)
    
    def do_POST(self):
        """Handle POST requests."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            
            try:
                message = json.loads(body)
            except json.JSONDecodeError as e:
                self._send_error_response(400, f"Invalid JSON: {str(e)}")
                return
            
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            try:
                response = loop.run_until_complete(self.mcp_server.process_message(message))
                
                if response:
                    self._send_json_response(response)
                else:
                    self._send_empty_response()
                    
            finally:
                loop.close()
                
        except Exception as e:
            logger.error(f"HTTP request error: {e}")
            logger.error(traceback.format_exc())
            self._send_error_response(500, str(e))
    
    def do_OPTIONS(self):
        """Handle OPTIONS requests for CORS."""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
    
    def _send_json_response(self, data: Dict[str, Any]) -> None:
        """Send JSON response."""
        response_json = json.dumps(data, ensure_ascii=False)
        
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        
        self.wfile.write(response_json.encode('utf-8'))
    
    def _send_empty_response(self) -> None:
        """Send empty response for notifications."""
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
    
    def _send_error_response(self, status_code: int, error_msg: str) -> None:
        """Send error response."""
        error_data = {
            'jsonrpc': '2.0',
            'id': None,
            'error': {
                'code': status_code,
                'message': error_msg
            }
        }
        
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        
        self.wfile.write(json.dumps(error_data).encode('utf-8'))
    
    def log_message(self, format, *args):
        """Override to use our logger."""
        logger.info(f"HTTP: {format % args}")
