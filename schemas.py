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

FETCH_MEDIA_SCHEMA = {
    "name": "ha_fetch_media",
    "description": (
        "Fetch Home Assistant-hosted media with HA authentication and save it as a local file. "
        "Use this when a Home Assistant image or camera entity exposes media."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "entity_id": {
                "type": "string",
                "description": "HA entity with attributes.entity_picture, e.g. image.front_door_person",
            },
            "path": {
                "type": "string",
                "description": "Optional HA media path, e.g. /api/image_proxy/image.foo?token=...",
            },
            "url": {
                "type": "string",
                "description": "Optional full HA media URL on the configured HA host.",
            },
        },
        "additionalProperties": False,
    },
}
