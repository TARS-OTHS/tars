"""Claude Code CLI as LLM provider.

Spawns Claude Code CLI sessions per agent in their project directory.
Uses Max subscription — no API key needed. Claude Code reads CLAUDE.md
automatically and is sandboxed to the project dir.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from src.core.base import LLMProvider, LLMResponse, Message, MessageRole, ToolDef

logger = logging.getLogger(__name__)

MODEL_MAP = {
    "opus": "opus",
    "sonnet": "sonnet",
    "haiku": "haiku",
    "claude-opus-4-6": "opus",
    "claude-sonnet-4-6": "sonnet",
    "claude-haiku-4-5": "haiku",
}


class ClaudeCodeProvider(LLMProvider):
    """LLM provider that uses Claude Code CLI.

    Each call spawns `claude --print` in the agent's project directory.
    Claude Code reads CLAUDE.md automatically for identity/instructions.
    """
    name = "claude_code"

    def __init__(self, config: dict):
        super().__init__(config)
        self._claude_bin = config.get("claude_bin", "claude")
        self._default_model = config.get("model", "sonnet")
        self._timeout = config.get("timeout", 3600)

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
        stream: bool = False,
        **kwargs,
    ) -> LLMResponse:
        """Send messages to Claude Code CLI and return the response.

        kwargs:
            project_dir: str — working directory for the CLI session
            model: str — model override for this call
            session_id: str — resume a previous session
            allowed_tools: list[str] — Claude Code tools to allow
        """
        project_dir = kwargs.get("project_dir", ".")
        model = kwargs.get("model", self._default_model)
        session_id = kwargs.get("session_id")
        allowed_tools = kwargs.get("allowed_tools")
        disallowed_tools = kwargs.get("disallowed_tools")

        # Build the prompt from messages
        resuming = session_id is not None
        prompt = self._build_prompt(messages, resuming=resuming)

        # Build CLI args
        args = [self._claude_bin, "--print", "--output-format", "json"]

        # Model
        resolved_model = MODEL_MAP.get(model, model)
        args.extend(["--model", resolved_model])

        # Session management
        if session_id:
            args.extend(["--resume", session_id])
            # Don't inject system prompt on resume — session already has it
        else:
            # System prompt — only inject if there's an explicit system message
            # (Claude Code reads CLAUDE.md automatically from project_dir)
            system_msgs = [m for m in messages if m.role == MessageRole.SYSTEM and m.content]
            if system_msgs:
                args.extend(["--system-prompt", system_msgs[0].content])

        # MCP config — use explicit path if set, otherwise auto-discover from cwd
        mcp_config = kwargs.get("mcp_config")
        if mcp_config:
            args.extend(["--mcp-config", str(mcp_config)])

        # Allowed tools
        if allowed_tools:
            args.extend(["--allowedTools"] + allowed_tools)

        # Disallowed tools — hide and block these entirely
        if disallowed_tools:
            args.extend(["--disallowedTools"] + disallowed_tools)

        # Run in the agent's project directory
        cwd = Path(project_dir).resolve()
        if not cwd.exists():
            cwd.mkdir(parents=True, exist_ok=True)

        logger.debug(f"Claude Code: cwd={cwd} model={resolved_model} prompt_len={len(prompt)}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=self._build_env(),
            )

            # Notify caller of running process (for /stop support)
            proc_callback = kwargs.get("proc_callback")
            if proc_callback:
                proc_callback(proc)

            timeout = kwargs.get("timeout", self._timeout)
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode()),
                timeout=timeout,
            )

            if proc.returncode != 0:
                stderr_text = stderr.decode().strip() if stderr else ""
                stdout_text = stdout.decode().strip() if stdout else ""

                # Try to extract error from JSON stdout (Claude Code sometimes returns errors there)
                error_detail = ""
                if stdout_text:
                    try:
                        data = json.loads(stdout_text)
                        if data.get("is_error"):
                            error_detail = data.get("result", "")
                    except json.JSONDecodeError:
                        error_detail = stdout_text[:500]

                error_msg = stderr_text or error_detail or f"Exit code {proc.returncode}"
                combined = f"{stderr_text} {error_detail}".lower()
                if "401" in combined or "authentication" in combined or "not logged in" in combined:
                    logger.critical(
                        "Claude auth failed — token may be expired. "
                        "Fix: run 'claude setup-token' as the tars user, then restart."
                    )
                    return LLMResponse(
                        content="Authentication failed — the Claude token needs to be refreshed. An admin needs to run `claude setup-token`.",
                        stop_reason="error",
                    )
                logger.error(
                    f"Claude Code failed (exit={proc.returncode}): {error_msg}"
                    + (f" | stdout: {stdout_text[:300]}" if stdout_text and not error_detail else "")
                )
                return LLMResponse(
                    content=f"Error from Claude Code: {error_msg}",
                    stop_reason="error",
                )

            return self._parse_response(stdout.decode())

        except asyncio.TimeoutError:
            logger.error(f"Claude Code timed out after {self._timeout}s")
            if proc:
                proc.kill()
            return LLMResponse(
                content="Claude Code timed out. Try a simpler request.",
                stop_reason="timeout",
            )
        except FileNotFoundError:
            logger.error(f"Claude Code CLI not found at '{self._claude_bin}'")
            return LLMResponse(
                content="Claude Code CLI not found. Is it installed?",
                stop_reason="error",
            )

    def _build_prompt(self, messages: list[Message], resuming: bool = False) -> str:
        """Build a prompt string from messages.

        For Claude Code CLI, we send the user messages as the prompt.
        System messages are handled via --system-prompt flag.

        When resuming a session, only send the latest user message —
        Claude Code already has the conversation history internally.
        """
        if resuming:
            # Only the latest user message — CLI has the rest
            for msg in reversed(messages):
                if msg.role == MessageRole.USER:
                    return msg.content
            return ""

        parts = []
        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                continue  # handled via --system-prompt
            elif msg.role == MessageRole.USER:
                parts.append(msg.content)
            elif msg.role == MessageRole.ASSISTANT:
                parts.append(f"[Previous response]: {msg.content}")
            elif msg.role == MessageRole.TOOL:
                parts.append(f"[Tool result ({msg.name})]: {msg.content}")
        return "\n\n".join(parts)

    def _parse_response(self, output: str) -> LLMResponse:
        """Parse Claude Code JSON output into LLMResponse."""
        output = output.strip()
        if not output:
            return LLMResponse(content="(empty response)", stop_reason="error")

        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            # Not JSON — treat as plain text (shouldn't happen with --output-format json)
            return LLMResponse(content=output, stop_reason="end_turn")

        if data.get("is_error"):
            return LLMResponse(
                content=data.get("result", "Unknown error"),
                stop_reason="error",
            )

        # Extract usage info
        usage = data.get("usage", {})
        total_tokens = (
            usage.get("input_tokens", 0) +
            usage.get("output_tokens", 0) +
            usage.get("cache_read_input_tokens", 0)
        )

        return LLMResponse(
            content=data.get("result", ""),
            tokens_used=total_tokens,
            model=data.get("model"),
            stop_reason=data.get("stop_reason", "end_turn"),
            session_id=data.get("session_id"),
        )

    # Env vars safe to pass to Claude Code subprocess.
    # Everything else is stripped to prevent secret leakage.
    _ENV_ALLOWLIST = {
        "PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
        "TERM", "COLORTERM", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
        "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "NODE_PATH", "EDITOR",
        "SSH_AUTH_SOCK", "CLAUDE_CONFIG_DIR",
    }

    def _build_env(self) -> dict[str, str]:
        """Build environment for the CLI subprocess.

        Only passes allowlisted env vars — prevents vault secrets,
        API keys, or other sensitive values from leaking into the subprocess.
        """
        return {k: v for k, v in os.environ.items() if k in self._ENV_ALLOWLIST}
