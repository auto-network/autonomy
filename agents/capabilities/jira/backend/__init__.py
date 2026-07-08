"""Host-side Jira backend for the broker: ADF conversion + REST client.

Runs only in trusted host processes (the dashboard). Agent containers never
import this — they call the dashboard's /api/jira/* routes and the approval
rendezvous instead, and never hold the token.
"""
