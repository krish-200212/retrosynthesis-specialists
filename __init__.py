"""Agent Scientist standalone plugin entry point; imports no models at discovery."""
def register(ctx):
    from .retro_specialists.plugin import register as register_tools
    register_tools(ctx)
