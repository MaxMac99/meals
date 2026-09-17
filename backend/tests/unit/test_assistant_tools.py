"""The assistant's tool table: parity with the MCP server, argument
validation against the schemas it ships, and the destructive-action flags.

These tests import the MCP server directly — it is a path dependency of the
backend, so the two halves of the agent surface are linted against each
other in the same suite that runs either of them.
"""

from app.assistant import tools


class TestParityWithMcp:
    async def test_every_assistant_tool_exists_in_mcp(self):
        """A handler whose name the MCP server doesn't publish would build a
        wire definition from nowhere and never reach the model."""
        from meals_mcp import server as mcp_server

        mcp_names = {tool.name for tool in await mcp_server.mcp.list_tools()}
        orphans = set(tools.TOOLS) - mcp_names
        assert orphans == set(), f"assistant tools with no MCP counterpart: {sorted(orphans)}"

    async def test_definitions_carry_the_mcp_schemas(self):
        from meals_mcp import server as mcp_server

        tools.forget_definitions()
        definitions = await tools.tool_definitions()
        mcp = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}
        names = {definition["function"]["name"] for definition in definitions}
        assert names == set(tools.TOOLS)
        for definition in definitions:
            name = definition["function"]["name"]
            tool = mcp[name]
            assert definition["function"]["parameters"] == tool.input_schema
            assert definition["function"]["description"] == (tool.description or name)
        tools.forget_definitions()

    async def test_definitions_are_cached(self):
        tools.forget_definitions()
        first = await tools.tool_definitions()
        assert await tools.tool_definitions() is first


class TestValidateArguments:
    schema = {
        "type": "object",
        "required": ["url"],
        "properties": {
            "url": {"type": "string"},
            "portions": {"type": "integer"},
            "include_staples": {"type": "boolean"},
            "tier": {"enum": ["premium", "budget", "any"]},
            "scale": {"type": "number"},
        },
    }

    def test_valid_arguments_pass(self):
        assert tools.validate_arguments(self.schema, {"url": "https://x", "portions": 2}) is None

    def test_not_an_object_at_all(self):
        assert tools.validate_arguments(self.schema, "url=x") is not None

    def test_missing_required_argument(self):
        assert "missing required argument 'url'" in tools.validate_arguments(self.schema, {})

    def test_wrong_type_is_named(self):
        assert "'url' must be a string" in tools.validate_arguments(self.schema, {"url": 3})
        assert "'portions' must be an integer" in tools.validate_arguments(self.schema, {"url": "x", "portions": 1.5})
        # bool is an int in Python — it must not pass as an integer or a number
        assert "'portions' must be an integer" in tools.validate_arguments(self.schema, {"url": "x", "portions": True})

    def test_enum_value_outside_the_vocabulary(self):
        assert "must be one of" in tools.validate_arguments(self.schema, {"url": "x", "tier": "posh"})

    def test_unknown_keys_are_ignored(self):
        # The model's stray keys harm nobody; handlers read by name.
        assert tools.validate_arguments(self.schema, {"url": "x", "weather": "nice"}) is None


class TestConfirmationFlags:
    def test_flatly_destructive_tools(self):
        for name in ("delete_recipe", "delete_meal", "delete_ingredient", "merge_ingredients", "finish_shop"):
            assert tools.TOOLS[name].requires_confirmation({})

    def test_reparse_only_when_forcing(self):
        assert tools.TOOLS["reparse_recipe"].requires_confirmation({"force": True})
        assert not tools.TOOLS["reparse_recipe"].requires_confirmation({"force": False})

    def test_freezer_take_only_when_binning(self):
        assert tools.TOOLS["take_from_freezer"].requires_confirmation({"all_of_it": True})
        assert not tools.TOOLS["take_from_freezer"].requires_confirmation({"portions": 2})

    def test_every_destructive_tool_has_a_sentence(self):
        for name, spec in tools.TOOLS.items():
            if spec.destructive or spec.destructive_for is not None:
                summary = tools.confirmation_summary(name, {})
                assert summary, name


class TestSummaries:
    def test_templates_read_the_arguments(self):
        summary = tools.confirmation_summary("delete_recipe", {"title": "Cottage Pie"})
        assert "Cottage Pie" in summary

    def test_unfillable_template_falls_back(self):
        summary = tools.confirmation_summary("delete_meal", {"unexpected": "shape"})
        assert "delete_meal" in summary
