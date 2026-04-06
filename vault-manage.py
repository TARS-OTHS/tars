#!/usr/bin/env python3
"""Manage the T.A.R.S vault — add, view, delete secrets."""
import sys
from pathlib import Path
from src.vault.fernet import FernetVault

KEY_FILE = Path.home() / ".config/tars-vault-key"

# Common secrets for Core tools — deployments can add their own via "Custom key"
ALL_KEYS = [
    # Discord (one per bot account)
    "discord-token",
    # AI / LLM
    "secrets/gemini-api-key",
    "secrets/groq-api-key",
    # Google Workspace (OAuth2)
    "secrets/google-api-credentials.json",
    # Web search
    "secrets/tavily-api-key",
    # Integrations
    "secrets/cloudflare-api-token",
    "secrets/notion-api-key",
    "secrets/trello-credentials.json",
    # GitHub
    "github-token",
]


def get_vault():
    v = FernetVault("config/secrets.enc")
    v.unlock(KEY_FILE.read_text().strip())
    return v


def cmd_list(v):
    keys = sorted(v.list_keys())
    print(f"\n  Vault: {len(keys)} secrets\n")
    for k in keys:
        print(f"    {k}")
    print()


def cmd_add(v):
    print("\n  Keys:")
    for i, key in enumerate(ALL_KEYS, 1):
        exists = "  *" if v.get(key) else ""
        print(f"    {i:>2}) {key}{exists}")
    custom_num = len(ALL_KEYS) + 1
    print(f"    {custom_num:>2}) Custom key")
    print("\n  (* = already set)")

    choice = input(f"\n  Pick [1-{custom_num}] or type key name: ").strip()

    try:
        idx = int(choice)
        if idx == custom_num:
            key = input("  Key name: ").strip()
        elif 1 <= idx <= len(ALL_KEYS):
            key = ALL_KEYS[idx - 1]
        else:
            print("  Invalid number.")
            return
    except ValueError:
        key = choice  # typed a key name directly

    if not key:
        print("  No key provided, aborting.")
        return

    existing = v.get(key)
    if existing:
        confirm = input(f"  '{key}' already exists. Overwrite? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            return

    value = input(f"  Paste value for '{key}': ").strip()
    if not value:
        print("  Empty value, aborting.")
        return

    v.set(key, value)
    print(f"  Saved '{key}'. Total secrets: {len(v.list_keys())}")


def cmd_delete(v):
    key = input("  Key to delete: ").strip()
    if not key:
        return
    if not v.get(key):
        print(f"  '{key}' not found.")
        return
    confirm = input(f"  Delete '{key}'? [y/N]: ").strip().lower()
    if confirm == "y":
        v.delete(key)
        print(f"  Deleted. Total secrets: {len(v.list_keys())}")


def cmd_check(v):
    key = input("  Key to check: ").strip()
    if v.get(key):
        print(f"  '{key}' = exists ({len(v.get(key))} chars)")
    else:
        print(f"  '{key}' = NOT FOUND")


def cmd_migrate_salt(v):
    from pathlib import Path
    salt_path = Path("config/secrets.salt")
    if salt_path.exists():
        print("  Per-instance salt already exists — already migrated.")
        return
    print("\n  This will migrate from the hardcoded vault salt to a random per-instance salt.")
    print("  The vault will be re-encrypted. Your passphrase is needed to re-derive the key.")
    confirm = input("\n  Proceed? [y/N]: ").strip().lower()
    if confirm != "y":
        print("  Aborted.")
        return
    passphrase = KEY_FILE.read_text().strip()
    v.migrate_salt(passphrase)
    print(f"  Salt migrated. New salt saved to {salt_path}")
    print(f"  Vault re-encrypted with new key. Total secrets: {len(v.list_keys())}")


def main():
    v = get_vault()

    while True:
        print("\n  T.A.R.S Vault Manager")
        print("  ─────────────────────")
        print("  1) List secrets")
        print("  2) Add/update secret")
        print("  3) Check if secret exists")
        print("  4) Delete secret")
        print("  5) Migrate vault salt")
        print("  q) Quit")

        choice = input("\n  > ").strip().lower()

        if choice in ("1", "list"):
            cmd_list(v)
        elif choice in ("2", "add"):
            cmd_add(v)
        elif choice in ("3", "check"):
            cmd_check(v)
        elif choice in ("4", "delete"):
            cmd_delete(v)
        elif choice in ("5", "migrate"):
            cmd_migrate_salt(v)
        elif choice in ("q", "quit", "exit"):
            print("  Done.")
            break
        else:
            print("  Invalid choice.")


if __name__ == "__main__":
    main()
