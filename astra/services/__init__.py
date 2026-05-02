"""
Service manager for Astra's agent fleet.

Controls the lifecycle of all agent backend services:
- Start/stop individual agents or the entire fleet
- Track PIDs and health
- Auto-start the A2A bridge server
- Port management to avoid conflicts
"""
