"""
EthOS AI Chat — Tool Execution (Tickets, etc.)
Extracts tool-related functionality from aichat.py.
"""

import json
import os
import sys

from flask import jsonify

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


TICKET_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_ticket",
            "description": "Creates a new ticket in the EthOS Kanban system. Use when the user asks to create a task, reports a problem, or suggests an improvement.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short ticket title, prefixed with a tag e.g. [FE], [BE], [DevOps]"},
                    "description": {"type": "string", "description": "Detailed description of the problem or task"},
                    "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"], "description": "Ticket priority"},
                    "column": {"type": "string", "enum": ["Backlog", "To Do"], "description": "Target column, defaults to Backlog"},
                },
                "required": ["title", "description", "priority"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_tickets",
            "description": "Retrieves a list of tickets from the Kanban board. Use to check project status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "column": {"type": "string", "description": "Filter by column (optional)"},
                },
            }
        }
    },
]


def _execute_tool(tool_name, args, username):
    """Execute an AI tool and return result string."""
    from blueprints.tickets import _load, _save, _gen_id, _now, _emit

    if tool_name == 'create_ticket':
        data = _load()
        # Find first copilot-enabled project or first project the user owns
        project = None
        for p in data.get('projects', []):
            if p.get('copilot_enabled') and username in p.get('members', []):
                project = p
                break
        if not project:
            for p in data.get('projects', []):
                if username in p.get('members', []):
                    project = p
                    break
        if not project:
            return "No available projects. Create a project in the ticket system."

        now = _now()
        ticket = {
            'id': _gen_id('t_'),
            'title': args.get('title', 'New ticket'),
            'description': args.get('description', ''),
            'project_id': project['id'],
            'column': args.get('column', 'Backlog'),
            'priority': args.get('priority', 'medium'),
            'assignee': '',
            'reporter': username,
            'labels': [],
            'comments': [],
            'order': 0,
            'created': now,
            'updated': now,
        }
        data['tickets'].append(ticket)
        _save(data)

        # Emit socket event if available
        _emit('ticket_created', project['id'], {'ticket': ticket})

        return f"Ticket created: [{ticket['priority'].upper()}] {ticket['title']} (id: {ticket['id']}, project: {project['name']}, column: {ticket['column']})"

    elif tool_name == 'list_tickets':
        data = _load()
        col_filter = args.get('column', '')
        results = []
        for p in data.get('projects', []):
            if username not in p.get('members', []):
                continue
            tickets = [t for t in data.get('tickets', []) if t['project_id'] == p['id']]
            if col_filter:
                tickets = [t for t in tickets if t['column'] == col_filter]
            for t in tickets:
                results.append(f"[{t['column']}] [{t['priority'].upper()}] {t['title']} (id: {t['id']})")
        if not results:
            return "No tickets" + (f" in column '{col_filter}'" if col_filter else "")
        return f"Found {len(results)} tickets:\n" + "\n".join(results)

    return f"Unknown tool: {tool_name}"


def register_tool_routes(blueprint):
    """Register tool routes on the given blueprint."""
    pass  # No dedicated routes for tools, they're called via /chat endpoint
