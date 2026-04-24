import asyncio
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part
from agent import sre_agent

SESSION_ID = "sre-session-1"
USER_ID = "sre-user"
APP_NAME = "sre-agent"


async def run():
    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=SESSION_ID,
    )

    runner = Runner(
        agent=sre_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    print("AI SRE Agent ready. Type 'exit' to quit.\n")

    while True:
        user_input = input("You: ").strip()
        if not user_input or user_input.lower() == "exit":
            break

        message = Content(role="user", parts=[Part(text=user_input)])

        async for event in runner.run_async(
            user_id=USER_ID,
            session_id=SESSION_ID,
            new_message=message,
        ):
            if event.is_final_response() and event.content:
                for part in event.content.parts:
                    if part.text:
                        print(f"\nAgent: {part.text}\n")


if __name__ == "__main__":
    asyncio.run(run())
