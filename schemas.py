LIST_EVENTS_SCHEMA = {
    "name": "ha_list_events",
    "description": "List Home Assistant event types available on the event bus.",
    "parameters": {"type": "object", "properties": {}},
}

RECENT_EVENTS_SCHEMA = {
    "name": "ha_recent_events",
    "description": "Return recent HA events observed by this plugin process.",
    "parameters": {
        "type": "object",
        "properties": {
            "event_type": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
    },
}
