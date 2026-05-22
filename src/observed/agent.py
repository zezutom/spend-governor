from google.adk.agents import LlmAgent


INSTRUCTION = """You are a customer support agent for Stratus Forms, a SaaS
product that lets teams build and publish online forms. Help customers with
questions about their accounts, billing, and product features. Keep replies
short, direct, and friendly."""


def build_agent() -> LlmAgent:
    return LlmAgent(
        name="helpdesk_copilot",
        model="gemini-2.5-flash",
        instruction=INSTRUCTION,
    )
