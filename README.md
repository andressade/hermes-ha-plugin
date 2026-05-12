# Hermes Home Assistant Plugin

Hermes Agent platform plugin for Home Assistant.

It listens to Home Assistant WebSocket events, matches configured triggers, turns
the matching event into a configured prompt, and lets Hermes Agent act on it.
Agent output is configurable: ignore it, write it back to Home Assistant, call a
safe Home Assistant service, or POST it to a webhook such as a Slack incoming
webhook.

## Install

Copy this directory to:

```bash
~/.hermes/plugins/hermes-ha-plugin
```

Enable it in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - hermes-ha-plugin
```

Configure the platform:

```yaml
gateway:
  platforms:
    homeassistant:
      enabled: true
      extra:
        url: "http://homeassistant.local:8123"
        listen_events:
          - state_changed
        response:
          type: none
        triggers:
          - name: front-door-opened
            event_type: state_changed
            cooldown_seconds: 120
            match:
              data.entity_id: binary_sensor.front_door
              data.new_state.state: "on"
            prompt: |
              Front door opened.
              Entity: {event.data.entity_id}
              Full HA event JSON: {json}

              Check whether this looks suspicious. If needed, use HA tools.
              Send a short summary to Slack.
            response:
              type: webhook
              url_env: SLACK_HOME_WEBHOOK_URL
              payload:
                text: "{response}"
```

By default each matched HA event gets its own Hermes session key so bursts do
not overwrite pending events. Set `shared_session: true` on a trigger only when
you explicitly want all events for that trigger to share conversation state.

Environment:

```bash
export HASS_TOKEN="your-home-assistant-long-lived-token"
export HASS_URL="http://homeassistant.local:8123"
export SLACK_HOME_WEBHOOK_URL="https://hooks.slack.com/services/..."
```

Restart the gateway after changing plugin/config:

```bash
hermes gateway restart
```

## Trigger Matching

Each trigger supports:

```yaml
triggers:
  - name: motion
    enabled: true
    event_type: state_changed
    cooldown_seconds: 60
    match:
      data.entity_id: "binary_sensor.*_motion"
      data.new_state.state:
        in: ["on", "detected"]
      data.old_state.state:
        not_equals: "on"
    prompt: "Motion event: {json}"
```

Match operators:

- scalar value: exact match
- string with `*`, `?`, or `[]`: glob match
- list: actual value must be in the list
- `equals`, `not_equals`, `in`, `exists`, `regex`, `glob`

Template fields:

- `{json}`: full event JSON
- `{event.event_type}`
- `{event.data.entity_id}`
- `{event.data.new_state.state}`
- `{trigger.name}`

## Response Sinks

Ignore the final agent response:

```yaml
response:
  type: none
```

Write to HA persistent notification:

```yaml
response:
  type: persistent_notification
  title: Hermes Agent
```

Call any HA service:

```yaml
response:
  type: service
  domain: notify
  service: slack
  message_key: message
  service_data:
    title: Hermes
    message: "{response}"
```

POST to webhook:

```yaml
response:
  type: webhook
  url_env: SLACK_HOME_WEBHOOK_URL
  payload:
    text: "{response}"
```

Trigger-level `response` overrides platform-level `response` for that dispatched
event.

## Tools

The plugin registers these Hermes tools:

The plugin adds:

- `ha_list_events`: list HA event bus event types
- `ha_recent_events`: inspect recent events seen by this plugin process

Hermes Agent's built-in Home Assistant tools remain responsible for state reads
and service calls, including their existing validation and service guardrails.

## Notes

This plugin intentionally registers the platform name `homeassistant`. When the
plugin is enabled, Hermes gateway resolves the plugin platform before the
built-in Home Assistant adapter, so this replaces the limited built-in event
adapter without patching Hermes core. It does not overwrite the built-in HA
state/service tools.
