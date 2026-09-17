"""The built-in AI assistant (POST /assistant/chat).

An optional, server-side agent over the same Meals domain the REST API and
the MCP server expose. Three parts:

- ``provider``  the LLM behind the loop. Off unless the deployment
                configures one; the API key never leaves this process.
- ``tools``     task-level tools with the MCP server's names, descriptions
                and schemas — one source of tool semantics — whose handlers
                call the existing router functions directly, in the calling
                user's session and household.
- ``runtime``   the tool-calling loop: bounded rounds, per-tool error
                isolation, and a stateless confirmation handshake for the
                destructive actions.

The assistant adds no business logic of its own: everything a tool can do
is something the REST API can do, and every query runs with the caller's
household scoping because it runs through the same code paths.
"""
