from src.greetings import greeting


def test_greeting_trims_and_capitalizes_each_name() -> None:
    assert greeting("  sam rivera  ") == "Hello, Sam Rivera!"
