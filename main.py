import sys
import os


def main():
    """Entry point for the Polymarket AI Agent."""
    # Validate required environment variable before importing the agent
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: Falta ANTHROPIC_API_KEY en el .env")
        print("Configura tu API key: export ANTHROPIC_API_KEY='tu-clave'")
        sys.exit(1)

    from polymarket_agent import PolymarketAgent
    agent = PolymarketAgent()
    agent.run()


if __name__ == "__main__":
    main()
