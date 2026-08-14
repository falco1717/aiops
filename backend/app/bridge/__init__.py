"""Helper processes that run alongside an agent rather than inside the app.

These are spawned by the agent CLI (not by AIOps), so they must stay
dependency-free and must not import the application package.
"""
