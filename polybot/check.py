"""Pre-flight check before going live: python -m polybot.check [--approve]
Verifies location, wallet, balance and trading approvals without placing any order.
With --approve it also sets up the one-time trading approvals, creating the Builder API key
those gasless transactions need and saving it to .env (never printed)."""

import sys
from pathlib import Path

from polymarket import SecureClient

from .config import Settings
from .main import check_geoblock

ENV_FILE = Path(".env")


def save_to_env(values: dict[str, str]) -> None:
    """Set KEY=value lines in .env, replacing empty or old lines, keeping everything else."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    remaining = dict(values)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    if remaining:
        lines += ["", "# Created by polybot.check --approve. Keep private."]
        lines += [f"{k}={v}" for k, v in remaining.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def connect(s: Settings, api_key=None) -> SecureClient:
    return SecureClient.create(private_key=s.poly_private_key,
                               wallet=s.poly_wallet_address or None, api_key=api_key)


def main() -> None:
    s = Settings()
    geo = check_geoblock()
    print(f"Location check: {geo}")
    if geo.get("blocked"):
        print("✗ Polymarket blocks this server's location. Use a VPS in an allowed country.")
        sys.exit(1)
    if not s.poly_private_key:
        print("✗ POLY_PRIVATE_KEY is empty in .env")
        sys.exit(1)
    client = connect(s, s.builder_api_key)
    print(f"✓ Signer {client.signer}, wallet {client.wallet} ({client.wallet_type})")
    bal = client.get_balance_allowance(asset_type="COLLATERAL")
    print(f"✓ Cash balance: ${bal.balance / 1e6:.2f}")
    if client.get_closed_only_mode():
        print("✗ Account is in close-only mode (can't open new positions).")
    print(f"✓ Builder API key: {'set' if s.builder_api_key else 'not set (needed for approvals and merges)'}")

    # The trading server's view decides whether orders are accepted. The indexer view below
    # can lag behind the chain for a while after an approval.
    allowances = bal.allowances or {}
    approved = [spender for spender, amount in allowances.items() if amount > 0]
    print(f"✓ Exchange allowances (trading server): {len(approved)} of {len(allowances)} approved"
          if approved else f"✗ Exchange allowances (trading server): none approved {allowances}")

    state = client.get_trading_approvals_state()
    if state.is_fully_approved:
        print("✓ Trading approvals (indexer): all set")
    else:
        print(f"• Trading approvals (indexer) still lists as missing: {state.missing}")
        print("  This view can lag the blockchain. If the line above says approved, you can go live.")
        if "--approve" not in sys.argv:
            print("  If not, run again with --approve.")

    if "--approve" in sys.argv and not state.is_fully_approved:
        api_key = s.builder_api_key
        if api_key is None:
            print("Creating a Builder API key (needed for gasless approvals)...")
            api_key = client.create_builder_api_key()
            save_to_env({"POLY_BUILDER_KEY": api_key.key, "POLY_BUILDER_SECRET": api_key.secret,
                         "POLY_BUILDER_PASSPHRASE": api_key.passphrase})
            print("✓ Builder API key created and saved to .env")
            client.close()
            client = connect(s, api_key)
        print("Setting up trading approvals (gasless, can take a minute)...")
        outcome = client.setup_trading_approvals().wait()
        print(f"  Result: {outcome}")
        state = client.get_trading_approvals_state()
        bal = client.get_balance_allowance(asset_type="COLLATERAL")
        approved = [sp for sp, amount in (bal.allowances or {}).items() if amount > 0]
        print(f"  Trading server allowances now: {len(approved)} of {len(bal.allowances or {})} approved")

    positions = list(client.list_positions().iter_items())
    print(f"✓ {len(positions)} open positions (manual bets are fine; the bot ignores them)")
    if approved and len(approved) == len(allowances):
        print("\nReady: delete polybot.db, set MODE=live in .env, then run start.bat.")


if __name__ == "__main__":
    main()
