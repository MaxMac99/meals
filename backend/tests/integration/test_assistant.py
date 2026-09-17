"""POST /assistant/chat, end to end with a scripted provider.

No test here reaches the network: the LLM is a ``FakeProvider`` whose
completions are queued per test, so every behaviour of the loop — tools,
confirmations, error recovery, the round ceiling — is proven against the
real routers, schemas and serializers, with only the model faked.

The tool layer's own unit tests live in tests/unit/test_assistant_tools.py;
the provider's in tests/unit/test_assistant_provider.py.
"""

import pytest

from app.assistant import provider as provider_module
from app.assistant.provider import CompletionResult, ProviderRateLimited, ProviderUnavailable, ToolCallRequest
from tests.conftest import create_meal, create_plan, create_recipe, get_list, item_by_name


class FakeProvider:
    """Pops one completion per call; records what the loop sent, so tests
    can assert on the tool traffic the model actually saw."""

    def __init__(self, script: list[CompletionResult | Exception]):
        self.script = list(script)
        self.turns: list[list[dict]] = []

    async def complete(self, messages, tools):
        self.turns.append(list(messages))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def tool_messages(self) -> list[str]:
        return [m["content"] for turn in self.turns for m in turn if m.get("role") == "tool"]


def answer(content: str) -> CompletionResult:
    return CompletionResult(content=content)


def call(name: str, arguments: dict, call_id: str = "call-1") -> CompletionResult:
    return CompletionResult(tool_calls=[ToolCallRequest(id=call_id, name=name, arguments=arguments)])


@pytest.fixture
def assistant_on(settings_override, monkeypatch):
    """The deployment configured with a provider. Returns a callable that
    installs a scripted FakeProvider and returns it."""
    settings_override(LLM_PROVIDER="openai", OPENAI_API_KEY="sk-test", OPENAI_MODEL="test-model")

    def install(script: list[CompletionResult | Exception]) -> FakeProvider:
        llm = FakeProvider(script)
        monkeypatch.setattr(provider_module, "get_provider", lambda: llm)
        return llm

    return install


async def chat(client, messages: list[dict], **extra):
    return await client.post("/assistant/chat", json={"messages": messages, **extra})


class TestDisabled:
    async def test_the_endpoint_does_not_exist_without_a_provider(self, client, monkeypatch):
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        response = await chat(client, [{"role": "user", "content": "hi"}])
        assert response.status_code == 404
        assert "LLM_PROVIDER" in response.json()["detail"]


class TestAuth:
    async def test_chat_needs_a_bearer_token(self, client, assistant_on):
        assistant_on([answer("hi")])
        response = await chat(client, [{"role": "user", "content": "hi"}])
        assert response.status_code == 401


class TestPlainChat:
    async def test_an_answer_without_tools(self, auth_client, assistant_on):
        assistant_on([answer("The spag bol is on the plan for this week.")])
        response = await chat(auth_client, [{"role": "user", "content": "what's for dinner?"}])
        assert response.status_code == 200
        body = response.json()
        assert body["message"] == {"role": "assistant", "content": "The spag bol is on the plan for this week."}
        assert body["references"] == []
        assert body["action"] is None

    async def test_the_conversation_is_trimmed_but_kept(self, auth_client, assistant_on):
        llm = assistant_on([answer("ok")])
        history = [{"role": "user", "content": f"message number {i} about dinner"} for i in range(30)]
        history[-1] = {"role": "user", "content": "and now the actual question"}
        response = await chat(auth_client, history)
        assert response.status_code == 200
        sent = llm.turns[0]
        # The system prompt, then at most MAX_HISTORY_MESSAGES of the client's
        # messages, newest last — and the newest is the one that asked.
        assert sent[0]["role"] == "system"
        assert len(sent) - 1 <= 24
        assert sent[-1]["content"] == "and now the actual question"


class TestSingleTool:
    async def test_get_plan_returns_references(self, auth_client, assistant_on):
        await create_plan(auth_client, label="w/c 20 July")
        llm = assistant_on([call("get_plan", {}), answer("You have one plan: w/c 20 July.")])
        response = await chat(auth_client, [{"role": "user", "content": "what's on the plan?"}])
        assert response.status_code == 200
        body = response.json()
        assert "w/c 20 July" in body["message"]["content"]
        refs = body["references"]
        assert refs and refs[0]["type"] == "plan" and refs[0]["title"] == "w/c 20 July"
        # The model saw the plan, not a URL or an HTTP error.
        tool_text = llm.tool_messages()[0]
        assert "w/c 20 July" in tool_text
        assert "API error" not in tool_text

    async def test_the_model_sees_the_skill_in_its_context(self, auth_client, assistant_on):
        llm = assistant_on([answer("ok")])
        await chat(auth_client, [{"role": "user", "content": "hi"}])
        system = llm.turns[0][0]["content"]
        assert "pools of options" in system  # SKILL.md's first golden rule, in its own words
        assert "meal-planning assistant" in system


class TestMultipleTools:
    async def test_plan_a_meal_across_two_rounds(self, auth_client, assistant_on):
        await create_plan(auth_client)
        recipe = await create_recipe(auth_client)
        await create_meal(auth_client, name="Spag bol", recipe_ids=[recipe["id"]])
        llm = assistant_on(
            [
                call("add_meal_to_plan", {"meal_id": "spag bol"}, call_id="c1"),
                call("get_shopping_list", {}, call_id="c2"),
                answer("Added; the list now has minced beef and the rest."),
            ]
        )
        response = await chat(auth_client, [{"role": "user", "content": "add spag bol to the plan"}])
        assert response.status_code == 200
        plan = (await auth_client.get("/plans/current")).json()
        assert [entry["meal"]["name"] for entry in plan["meals"]] == ["Spag bol"]
        shopping = await get_list(auth_client)
        assert item_by_name(shopping, "minced beef") is not None
        tool_texts = llm.tool_messages()
        # Each turn replays the earlier tool results, so the newest reply is
        # the last message of the last call: the list it filled.
        assert "Spag bol" in tool_texts[0]
        assert "minced beef" in tool_texts[-1]


class TestInvalidToolCalls:
    async def test_an_unknown_tool_is_feedback_not_a_crash(self, auth_client, assistant_on):
        llm = assistant_on(
            [call("make_coffee", {}, call_id="c1"), answer("I can't make coffee — I run your meal plan.")]
        )
        response = await chat(auth_client, [{"role": "user", "content": "make coffee"}])
        assert response.status_code == 200
        assert any("unknown tool 'make_coffee'" in text for text in llm.tool_messages())

    async def test_arguments_that_fails_the_schema_are_named(self, auth_client, assistant_on):
        llm = assistant_on([call("add_to_list", {}, call_id="c1"), answer("What should I add?")])
        response = await chat(auth_client, [{"role": "user", "content": "add milk"}])
        assert response.status_code == 200
        assert any("missing required argument 'name'" in text for text in llm.tool_messages())


class TestToolErrors:
    async def test_a_missing_meal_is_a_sentence_the_model_can_read(self, auth_client, assistant_on):
        llm = assistant_on(
            [call("add_meal_to_plan", {"meal_id": "toast"}, call_id="c1"), answer("There's no meal called toast.")]
        )
        response = await chat(auth_client, [{"role": "user", "content": "put toast on the plan"}])
        assert response.status_code == 200
        assert any("No meal matching 'toast'" in text for text in llm.tool_messages())

    async def test_a_router_refusal_reaches_the_model_verbatim(self, auth_client, assistant_on):
        """mark_cooked on a meal that is not on the plan is a 404 from the
        router — the tool layer's HTTPException path."""
        await create_meal(auth_client, name="Spag bol")
        llm = assistant_on(
            [call("mark_meal_cooked", {"meal_name": "Spag bol"}, call_id="c1"), answer("It isn't on the plan yet.")]
        )
        response = await chat(auth_client, [{"role": "user", "content": "we cooked spag bol"}])
        assert response.status_code == 200
        assert any("API error 404" in text for text in llm.tool_messages())


class TestRoundCeiling:
    async def test_the_loop_stops_after_max_rounds(self, auth_client, assistant_on):
        recipe = await create_recipe(auth_client)
        await create_meal(auth_client, name="Spag bol", recipe_ids=[recipe["id"]])
        await create_plan(auth_client)
        # Eight rounds of the same harmless add — idempotent? No: duplicate
        # adds are refused, which is the point. The ceiling ends the loop
        # before the ninth call, with an honest sentence.
        llm = assistant_on([call("add_meal_to_plan", {"meal_id": "spag bol"}, call_id=f"c{i}") for i in range(8)])
        response = await chat(auth_client, [{"role": "user", "content": "add it eight times"}])
        assert response.status_code == 200
        assert "stopped after 8 tool calls" in response.json()["message"]["content"]
        # Eight completions, and no ninth — the ceiling is what ended it.
        assert len(llm.turns) == 8


class TestMutations:
    async def test_an_ad_hoc_add_persists(self, auth_client, assistant_on):
        assistant_on(
            [
                call("add_to_list", {"name": "skyr", "quantity": 500, "unit": "g"}, call_id="c1"),
                answer("Skyr is on the list."),
            ]
        )
        response = await chat(auth_client, [{"role": "user", "content": "add 500g skyr to the list"}])
        assert response.status_code == 200
        shopping = await get_list(auth_client)
        item = item_by_name(shopping, "skyr")
        assert item is not None and item["quantity"] == 500
        refs = response.json()["references"]
        assert refs and refs[0]["type"] == "list_item"

    async def test_a_check_off_persists(self, auth_client, assistant_on):
        await auth_client.post("/shopping-list/items", json={"name": "milk", "quantity": 2, "unit": "l"})
        assistant_on([call("check_off", {"item_name": "milk"}, call_id="c1"), answer("Done.")])
        response = await chat(auth_client, [{"role": "user", "content": "tick off the milk"}])
        assert response.status_code == 200
        shopping = await get_list(auth_client, include_staples=True, include_excluded=True)
        assert item_by_name(shopping, "milk")["checked"] is True

    async def test_making_a_recipe_protein_richer_rewrites_its_lines(self, auth_client, assistant_on):
        """'Mach die Pfanne proteinreicher' — the recipe's own lines change,
        the meal on the plan follows, and the list re-syncs."""
        recipe = await create_recipe(
            auth_client,
            title="Hack-Reis-Pfanne",
            ingredients=[{"name": "minced beef", "quantity": 300, "unit": "g"}],
        )
        meal = await create_meal(auth_client, name="Pfanne", recipe_ids=[recipe["id"]])
        plan = await create_plan(auth_client)
        added = await auth_client.post(f"/plans/{plan['id']}/meals", json={"meal_id": meal["id"]})
        assert added.status_code == 201, added.text
        recipe_id = recipe["id"]
        assistant_on(
            [
                call(
                    "update_recipe",
                    {
                        "recipe": "Hack-Reis-Pfanne",
                        "ingredients": [
                            {"name": "minced beef", "quantity": 500, "unit": "g"},
                            {"name": "eggs", "quantity": 4, "unit": "item"},
                        ],
                    },
                    call_id="c1",
                ),
                answer("More protein: 500 g mince and four eggs."),
            ]
        )
        response = await chat(auth_client, [{"role": "user", "content": "mach die Pfanne proteinreicher"}])
        assert response.status_code == 200
        fresh = (await auth_client.get(f"/recipes/{recipe_id}")).json()
        assert fresh["edited"] is True
        names = {line["name"]: line for line in fresh["ingredients"]}
        assert names["minced beef"]["quantity"] == 500
        assert "egg" in names
        # The meal on the plan re-synced: the list asks for the new amount.
        shopping = await get_list(auth_client)
        assert item_by_name(shopping, "minced beef")["quantity"] == 500
        assert item_by_name(shopping, "egg") is not None
        refs = response.json()["references"]
        assert refs and refs[0]["type"] == "recipe" and refs[0]["id"] == recipe_id


class TestConfirmation:
    async def test_a_destructive_call_is_held_for_the_household(self, auth_client, assistant_on):
        await create_recipe(auth_client, title="Bad parse")
        llm = assistant_on([call("delete_recipe", {"title": "Bad parse"}, call_id="c1")])
        response = await chat(auth_client, [{"role": "user", "content": "delete the bad parse"}])
        assert response.status_code == 200
        body = response.json()
        assert body["action"]["tool"] == "delete_recipe"
        assert "Bad parse" in body["action"]["summary"]
        assert body["action"]["arguments"] == {"title": "Bad parse"}
        # Nothing was deleted: the model's ask-first text is the answer.
        assert body["message"]["role"] == "assistant"
        library = (await auth_client.get("/recipes")).json()
        assert [r["title"] for r in library] == ["Bad parse"]
        # The provider was called exactly once — the loop stopped at the
        # gate, before any second completion could happen.
        assert len(llm.turns) == 1

    async def test_the_confirmed_action_runs_and_the_loop_continues(self, auth_client, assistant_on):
        await create_recipe(auth_client, title="Bad parse")
        llm = assistant_on([answer("Deleted the recipe.")])
        response = await chat(
            auth_client,
            [{"role": "user", "content": "delete the bad parse"}],
            confirmed_action={"tool": "delete_recipe", "arguments": {"title": "Bad parse"}},
        )
        assert response.status_code == 200
        assert response.json()["message"]["content"] == "Deleted the recipe."
        library = (await auth_client.get("/recipes")).json()
        assert library == []
        # The model saw the confirmation as the tool result it followed.
        assert any("Bad parse" in text for text in llm.tool_messages())

    async def test_a_confirmed_non_destructive_tool_is_refused(self, auth_client, assistant_on):
        assistant_on([])
        response = await chat(
            auth_client,
            [{"role": "user", "content": "x"}],
            confirmed_action={"tool": "add_to_list", "arguments": {"name": "milk"}},
        )
        assert response.status_code == 422
        assert "not a confirmation-required action" in response.json()["detail"]

    async def test_tampered_confirmation_arguments_are_refused(self, auth_client, assistant_on):
        await create_recipe(auth_client, title="Keep me")
        assistant_on([])
        response = await chat(
            auth_client,
            [{"role": "user", "content": "x"}],
            confirmed_action={"tool": "delete_recipe", "arguments": {}},
        )
        assert response.status_code == 422

    async def test_freezer_take_only_needs_confirmation_when_binning(self, auth_client, assistant_on):
        """A normal freezer take runs straight through; all_of_it asks."""
        assistant_on(
            [call("add_to_freezer", {"name": "chilli", "portions": 2}, call_id="c1"), answer("In the freezer.")]
        )
        response = await chat(auth_client, [{"role": "user", "content": "two portions of chilli went in the freezer"}])
        assert response.status_code == 200
        assert response.json()["action"] is None  # an explicit take needs no dialog
        assistant_on([call("take_from_freezer", {"name": "chilli", "all_of_it": True}, call_id="c2")])
        response = await chat(auth_client, [{"role": "user", "content": "bin the chilli"}])
        assert response.status_code == 200
        assert response.json()["action"]["tool"] == "take_from_freezer"
        freezer = (await auth_client.get("/freezer")).json()
        assert freezer["items"]  # still there, waiting for the yes


class TestProviderErrors:
    async def test_an_unreachable_provider_is_a_502_with_a_sentence(self, auth_client, assistant_on):
        assistant_on([ProviderUnavailable("the LLM provider could not be reached")])
        response = await chat(auth_client, [{"role": "user", "content": "hi"}])
        assert response.status_code == 502
        assert "could not be reached" in response.json()["detail"]
        # The provider's own text must not leak into the response.
        assert "sk-test" not in response.text

    async def test_a_rate_limited_provider_is_a_429(self, auth_client, assistant_on):
        assistant_on([ProviderRateLimited("slow down")])
        response = await chat(auth_client, [{"role": "user", "content": "hi"}])
        assert response.status_code == 429
        assert "try again in a moment" in response.json()["detail"]


class TestHouseholdBoundary:
    async def test_tools_cannot_reach_another_household(self, client, assistant_on):
        """Two households on one server: the assistant runs with whoever is
        authenticated, and the routers' scoping decides what it sees."""
        first = await client.post(
            "/auth/register",
            json={"email": "a@example.com", "password": "a-strong-password", "display_name": "A"},
        )
        first_token = first.json()["token"]
        client.headers["Authorization"] = f"Bearer {first_token}"
        await create_recipe(client, title="A's secret lasagne")

        second = await client.post(
            "/auth/register",
            json={"email": "b@example.com", "password": "a-strong-password", "display_name": "B"},
        )
        client.headers["Authorization"] = f"Bearer {second.json()['token']}"

        llm = assistant_on([call("list_recipes", {}, call_id="c1"), answer("Your library is empty.")])
        response = await chat(client, [{"role": "user", "content": "show recipes"}])
        assert response.status_code == 200
        # B's tool result saw B's library, never A's recipe. An empty
        # library reaches the model as a bare JSON array.
        tool_text = llm.tool_messages()[0]
        assert "A's secret lasagne" not in tool_text
        assert tool_text.strip() == "[]"


class TestThrottle:
    async def test_more_than_the_rate_limit_is_refused_per_household(
        self, auth_client, assistant_on, settings_override
    ):
        settings_override(
            LLM_PROVIDER="openai",
            OPENAI_API_KEY="sk-test",
            OPENAI_MODEL="test-model",
            ASSISTANT_RATE_LIMIT_PER_MINUTE="2",
        )
        assistant_on([answer("ok"), answer("ok"), answer("ok")])
        assert (await chat(auth_client, [{"role": "user", "content": "1"}])).status_code == 200
        assert (await chat(auth_client, [{"role": "user", "content": "2"}])).status_code == 200
        throttled = await chat(auth_client, [{"role": "user", "content": "3"}])
        assert throttled.status_code == 429
        assert "wait a minute" in throttled.json()["detail"]
