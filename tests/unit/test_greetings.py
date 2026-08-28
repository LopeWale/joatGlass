"""Deterministic responses for salutations that do not need an LLM."""

from joat.services.greetings import greeting_response


class TestGreetingResponse:
    def test_good_morning_is_answered_without_personal_grounding(self) -> None:
        assert greeting_response("Good morning.") == "Good morning. How can I help?"

    def test_compound_stt_spelling_and_assistant_suffix_are_tolerated(self) -> None:
        assert greeting_response("Goodmorning, JOATs!") == "Good morning. How can I help?"

    def test_other_simple_salutations_are_natural(self) -> None:
        assert greeting_response("hello") == "Hello. How can I help?"
        assert greeting_response("good afternoon") == "Good afternoon. How can I help?"
        assert greeting_response("good evening") == "Good evening. How can I help?"

    def test_substantive_request_is_not_swallowed(self) -> None:
        assert greeting_response("Good morning, what is on my calendar?") is None
        assert greeting_response("Hello, reply saying good morning") is None

