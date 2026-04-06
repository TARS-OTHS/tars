"""Built-in tools — always available to agents."""

import asyncio
import logging

from src.core.base import IncomingMessage, ToolContext
from src.core.tools import tool

logger = logging.getLogger(__name__)

# Maximum inter-agent call depth to prevent infinite loops (A -> B -> A -> ...)
MAX_INTER_AGENT_DEPTH = 3


def _build_inter_agent_message(
    ctx: ToolContext, target_agent: str, content: str,
) -> IncomingMessage:
    """Build an IncomingMessage for inter-agent communication."""
    msg = IncomingMessage(
        connector="internal",
        channel_id=f"internal:{ctx.agent_id}:{target_agent}",
        user_id=ctx.agent_id,
        user_name=f"agent:{ctx.agent_id}",
        content=content,
    )
    # Carry depth through the message so the target agent's ToolContext inherits it
    msg._inter_agent_depth = ctx.inter_agent_depth + 1  # type: ignore[attr-defined]
    return msg


@tool(name="send_message", description="Send a message to a Discord channel or user")
async def send_message(ctx: ToolContext, channel_id: str, content: str, bot: str = "") -> str:
    """Send a message to a specific channel.

    Args:
        channel_id: The Discord channel ID to send to.
        content: The message content.
        bot: Which bot account to send as (e.g. 'main', 'assistant'). Leave empty for default.
    """
    if ctx.connector_send and not bot:
        await ctx.connector_send(channel_id, content)
        return f"Message sent to {channel_id}"

    # Fallback: send via Discord API directly (MCP server context)
    if ctx.vault:
        token = None
        if bot:
            token = ctx.vault.get(f"discord-token-{bot}")
            if not token:
                return f"Error: no Discord token for bot '{bot}'."
        else:
            token = ctx.vault.get("active-discord-token") or ctx.vault.get("discord-token")
        if token:
            import aiohttp
            headers = {
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (https://github.com/tars, 1.0)",
            }
            # Split long messages (Discord 2000 char limit)
            chunks = [content[i:i+2000] for i in range(0, len(content), 2000)]
            async with aiohttp.ClientSession() as session:
                for chunk in chunks:
                    async with session.post(
                        f"https://discord.com/api/v10/channels/{channel_id}/messages",
                        headers=headers,
                        json={"content": chunk},
                    ) as resp:
                        if resp.status != 200:
                            err = await resp.text()
                            return f"Discord API error ({resp.status}): {err[:200]}"
            return f"Message sent to {channel_id}"

    return "No connector available to send messages"


@tool(name="ask_agent", description="Ask another agent a question and wait for their response")
async def ask_agent(ctx: ToolContext, agent_name: str, question: str) -> str:
    """Ask another agent a question. Returns their response.

    This is a synchronous call — waits for the other agent to respond.
    Use send_to_agent for fire-and-forget.
    """
    if not ctx.agent_manager:
        return "No agent manager available — inter-agent communication not supported."

    # Depth check to prevent infinite loops
    if ctx.inter_agent_depth >= MAX_INTER_AGENT_DEPTH:
        return (
            f"Inter-agent call depth limit reached ({MAX_INTER_AGENT_DEPTH}). "
            f"Cannot ask {agent_name} — this prevents infinite agent loops."
        )

    # Verify target agent exists
    if agent_name not in ctx.agent_manager.agent_configs:
        available = list(ctx.agent_manager.agent_configs.keys())
        return f"Unknown agent '{agent_name}'. Available agents: {available}"

    # Prevent self-calls
    if agent_name == ctx.agent_id:
        return "Cannot ask yourself — use a different agent."

    logger.info(
        f"Inter-agent ask: {ctx.agent_id} -> {agent_name} "
        f"(depth {ctx.inter_agent_depth + 1}/{MAX_INTER_AGENT_DEPTH})"
    )

    msg = _build_inter_agent_message(ctx, agent_name, question)

    try:
        response = await ctx.agent_manager.handle_internal_message(agent_name, msg)
        if response:
            return response
        return f"Agent {agent_name} returned no response."
    except Exception as e:
        logger.error(f"Inter-agent ask failed ({ctx.agent_id} -> {agent_name}): {e}", exc_info=True)
        return f"Error communicating with {agent_name}: {e}"


@tool(name="send_to_agent", description="Send a message to another agent (fire and forget)")
async def send_to_agent(ctx: ToolContext, agent_name: str, message: str) -> str:
    """Send a message to another agent without waiting for a response.

    Fire-and-forget — the message is dispatched in the background.
    """
    if not ctx.agent_manager:
        return "No agent manager available — inter-agent communication not supported."

    # Depth check
    if ctx.inter_agent_depth >= MAX_INTER_AGENT_DEPTH:
        return (
            f"Inter-agent call depth limit reached ({MAX_INTER_AGENT_DEPTH}). "
            f"Cannot send to {agent_name} — this prevents infinite agent loops."
        )

    # Verify target agent exists
    if agent_name not in ctx.agent_manager.agent_configs:
        available = list(ctx.agent_manager.agent_configs.keys())
        return f"Unknown agent '{agent_name}'. Available agents: {available}"

    # Prevent self-sends
    if agent_name == ctx.agent_id:
        return "Cannot send to yourself — use a different agent."

    logger.info(
        f"Inter-agent send: {ctx.agent_id} -> {agent_name} "
        f"(depth {ctx.inter_agent_depth + 1}/{MAX_INTER_AGENT_DEPTH})"
    )

    msg = _build_inter_agent_message(ctx, agent_name, message)

    # Fire and forget — dispatch in background task
    asyncio.create_task(
        ctx.agent_manager.handle_internal_message(agent_name, msg),
        name=f"inter-agent:{ctx.agent_id}->{agent_name}",
    )

    return f"Message sent to {agent_name} (fire-and-forget)."
